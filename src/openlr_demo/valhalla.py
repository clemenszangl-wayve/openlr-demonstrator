"""Routes drawn on OSM with Valhalla, carried over onto the bundled OSM network for encoding.

Valhalla routes on its own (current) OSM graph. ``/trace_attributes`` then reports the OSM way
of every edge of the route, and the route is matched onto the local network with the HMM,
penalising lines of other ways - an exact transfer whenever the way IDs agree.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import OUTPUT_DIR
from .evaluation import PathResult
from .geo import LonLat, densify, polyline_length
from .matchers import hmm_path
from .network import Line, RoadNetwork
from .routing import RoutePoint
from .settings import setting

DEFAULT_URL = "https://valhalla1.openstreetmap.de"  # FOSSGIS public server, fair use only
CACHE_DIR = OUTPUT_DIR / ".cache" / "valhalla"
ROAD_CLASS = ("motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
              "service_other")  # Valhalla road classes in FRC order
EDGE_ATTRIBUTES = ["edge.way_id", "edge.road_class", "edge.names", "edge.length", "edge.use", "edge.tunnel",
                   "edge.bridge", "edge.begin_shape_index", "edge.end_shape_index"]
WAY_PENALTY_M = 25.0
HEADING_TOLERANCE_DEG = 45


class ValhallaError(Exception):
    pass


@dataclass
class ValhallaRoute:
    shape: list[LonLat]
    length_m: float
    edges: list[dict]  # way_id, road_class, names, begin/end_shape_index, ...
    cached: bool = False

    def way_at(self) -> list[int | None]:
        """OSM way ID for every shape segment (index i covers shape[i] -> shape[i + 1])."""
        ways: list[int | None] = [None] * max(0, len(self.shape) - 1)
        for e in self.edges:
            for i in range(e.get("begin_shape_index", 0), min(e.get("end_shape_index", 0), len(ways))):
                ways[i] = e.get("way_id")
        return ways


def decode_polyline(encoded: str, precision: int = 6) -> list[LonLat]:
    coords, index, lat, lon, factor = [], 0, 0, 0, 10 ** precision
    while index < len(encoded):
        for which in (0, 1):
            shift, result = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lon / factor, lat / factor))
    return coords


class ValhallaClient:
    min_interval_s = 1.0  # the public server asks for moderate use

    def __init__(self, url: str = DEFAULT_URL, cache_dir: Path | None = CACHE_DIR, offline: bool = False):
        self.url = url.rstrip("/")
        self.cache_dir = cache_dir
        self.offline = offline
        self.requests = 0
        self._lock = threading.Lock()
        self._last = 0.0

    @classmethod
    def from_env(cls) -> ValhallaClient | None:
        url = setting("VALHALLA_URL", DEFAULT_URL)
        return cls(url) if url else None

    def post(self, endpoint: str, body: dict) -> tuple[dict, bool]:
        payload = json.dumps(body, sort_keys=True)
        key = hashlib.sha256(f"{self.url}/{endpoint}:{payload}".encode()).hexdigest()[:32]
        cache = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8")), True
        if self.offline:
            raise ValhallaError("offline and not cached")
        with self._lock:
            wait = self._last + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        req = urllib.request.Request(f"{self.url}/{endpoint}", payload.encode(),
                                     {"Content-Type": "application/json", "User-Agent": "openlr-demonstrator"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
                self.requests += 1
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", "")
            except ValueError:
                message = ""
            raise ValhallaError(f"Valhalla HTTP {exc.code}: {message or exc.reason}") from None
        except urllib.error.URLError as exc:
            raise ValhallaError(f"Valhalla not reachable: {exc.reason}") from None
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        return data, False

    def route(self, points: list[RoutePoint]) -> ValhallaRoute:
        if len(points) < 2:
            raise ValhallaError("a route needs at least two points")
        locations = []
        for i, p in enumerate(points):
            loc: dict = {"lat": round(p.lat, 7), "lon": round(p.lon, 7),
                         "type": "break" if i in (0, len(points) - 1) else "through"}
            if p.frc is not None:
                cls = ROAD_CLASS[p.frc]
                loc["search_filter"] = {"min_road_class": cls, "max_road_class": cls}
            if p.heading is not None:
                loc["heading"], loc["heading_tolerance"] = round(p.heading) % 360, HEADING_TOLERANCE_DEG
            locations.append(loc)
        data, cached = self.post("route", {"locations": locations, "costing": "auto",
                                           "directions_options": {"units": "kilometers"}})
        if "trip" not in data:
            raise ValhallaError(data.get("error", "no route"))
        shape: list[LonLat] = []
        for leg in data["trip"]["legs"]:
            pts = decode_polyline(leg["shape"])
            shape.extend(pts[1:] if shape and pts and pts[0] == shape[-1] else pts)
        body = {"shape": [{"lat": lat, "lon": lon} for lon, lat in shape], "costing": "auto",
                "shape_match": "edge_walk", "filters": {"attributes": EDGE_ATTRIBUTES, "action": "include"}}
        try:
            attrs, cached2 = self.post("trace_attributes", body)
        except ValhallaError as exc:  # edge_walk needs an exact edge sequence; fall back to map matching
            if "edge_walk" not in str(exc):
                raise
            attrs, cached2 = self.post("trace_attributes", {**body, "shape_match": "walk_or_snap"})
        return ValhallaRoute(shape, data["trip"]["summary"]["length"] * 1000.0, attrs.get("edges", []),
                             cached and cached2)


def to_osm(route: ValhallaRoute, net: RoadNetwork, step: float = 10.0) -> PathResult:
    """Match the Valhalla route onto the local network, preferring lines of the same OSM way."""
    lon_min, lat_min, lon_max, lat_max = net.meta["bbox"]
    outside = [c for c in route.shape if not (lon_min <= c[0] <= lon_max and lat_min <= c[1] <= lat_max)]
    if outside:
        raise ValhallaError(f"the route leaves the bundled OSM extract ({lat_min}-{lat_max} N, "
                            f"{lon_min}-{lon_max} E); rebuild it with `openlr-demo fetch-data --bbox ...`")
    xy = net.proj.coords_to_xy(route.shape)
    ways = route.way_at()
    pts, way_of = [], []
    for i in range(len(xy) - 1):
        for p in densify([xy[i], xy[i + 1]], step)[:-1]:
            if pts and abs(pts[-1][0] - p[0]) + abs(pts[-1][1] - p[1]) < 0.5:
                continue
            pts.append(p)
            way_of.append(ways[i])
    pts.append(xy[-1])
    way_of.append(ways[-1] if ways else None)

    def penalty(i: int, line: Line) -> float:
        return 0.0 if way_of[i] is None or line.way_id == way_of[i] else WAY_PENALTY_M

    path = hmm_path(pts, net, radius=25.0, step=step, penalty=penalty)
    if path is None or polyline_length(xy) < 20:
        raise ValhallaError("could not place the route on the OSM network")
    return path


def way_agreement(route: ValhallaRoute, path: PathResult) -> float:
    """Share of the Valhalla route length on OSM ways that the transferred path also uses."""
    used = {ln.way_id for ln in path.lines}
    total = sum(e.get("length", 0.0) for e in route.edges)
    same = sum(e.get("length", 0.0) for e in route.edges if e.get("way_id") in used)
    return same / total if total else 0.0
