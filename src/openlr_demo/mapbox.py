"""Mapbox Map Matching as a further linestring baseline.

Mapbox receives exactly what the other linestring matchers receive - the route coordinates
from map A - and matches them on its own map (Mapbox data, or ZENRIN data with a custom
routing profile). Its answer is geometry plus road attributes per segment
(``mapbox_streets_v8.class`` such as ``motorway`` / ``trunk``, and a ``tunnel`` flag), but no
OSM IDs. To score it like the other methods, the Mapbox geometry is transferred onto OSM
lines *of the road class Mapbox reported* (see :func:`transfer_to_osm`). For stacked roads
the class is what separates the levels (Shuto 3 = motorway, Route 246 = trunk).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import LineString

from . import OUTPUT_DIR
from .evaluation import PathResult
from .geo import LonLat, densify
from .matchers import hmm_path
from .network import FRC_BY_HIGHWAY, Line, RoadNetwork
from .settings import setting

API_URL = "https://api.mapbox.com/matching/v5"
DEFAULT_PROFILE = "mapbox/driving"
DEFAULT_RADIUS_M = 20.0
MAX_COORDINATES = 100  # API limit per request
CACHE_DIR = OUTPUT_DIR / ".cache" / "mapbox"

# mapbox_streets_v8 road classes -> OpenLR FRC (same scheme as the OSM mapping in network.py)
_CLASS_FRC = {**{k: int(v) for k, v in FRC_BY_HIGHWAY.items()},
              "secondary_tertiary": 3, "street": 6, "street_limited": 6, "service": 7}


def class_frc(road_class: str | None) -> int | None:
    if not road_class:
        return None
    return _CLASS_FRC.get(road_class.removesuffix("_link"), 7)


class MapboxError(Exception):
    pass


@dataclass(frozen=True)
class MapboxConfig:
    token: str = field(repr=False)
    profile: str = DEFAULT_PROFILE
    radius_m: float = DEFAULT_RADIUS_M
    cache_dir: Path | None = CACHE_DIR

    @classmethod
    def from_env(cls) -> MapboxConfig | None:
        token = setting("MAPBOX_ACCESS_TOKEN")
        if not token:
            return None
        profile = setting("MAPBOX_PROFILE") or DEFAULT_PROFILE
        if "/" not in profile:  # "<account>.zenrin-premium" -> "<account>.zenrin-premium/driving"
            profile += "/driving"
        return cls(token, profile, float(setting("MAPBOX_RADIUS") or DEFAULT_RADIUS_M))


@dataclass
class MapboxSegment:
    coords: list[LonLat]
    road_class: str | None
    tunnel: bool
    name: str
    ref: str

    @property
    def frc(self) -> int | None:
        return class_frc(self.road_class)


@dataclass
class MapboxMatch:
    profile: str
    sent: int  # coordinates sent
    segments: list[MapboxSegment] = field(default_factory=list)
    confidence: list[float] = field(default_factory=list)
    matched: int = 0  # tracepoints Mapbox could match
    path: PathResult | None = None  # transferred to OSM
    error: str | None = None
    cached: bool = False

    def class_runs(self, net: RoadNetwork) -> list[tuple[str, float]]:
        """Consecutive road classes along the Mapbox result with their length in metres."""
        runs: list[list] = []
        for seg in self.segments:
            label = (seg.road_class or "unknown") + (" tunnel" if seg.tunnel else "")
            length = LineString(net.proj.coords_to_xy(seg.coords)).length
            if runs and runs[-1][0] == label:
                runs[-1][1] += length
            else:
                runs.append([label, length])
        return [(label, length) for label, length in runs if length >= 1]

    def describe(self, net: RoadNetwork) -> str:
        if self.error:
            return f"error: {self.error}"
        runs = " -> ".join(f"{label} {length:.0f} m" for label, length in self.class_runs(net))
        conf = ", ".join(f"{c:.2f}" for c in self.confidence)
        return f"{self.matched}/{self.sent} points matched, confidence {conf}; road classes: {runs}"


# ---------------------------------------------------------------------- API client
class MapboxClient:
    """Map Matching requests with an on-disk cache (responses only - never the token)."""

    min_interval_s = 0.25  # stay well below the API rate limit of 300 requests / minute

    def __init__(self, config: MapboxConfig):
        self.config = config
        self._lock = threading.Lock()
        self._last = 0.0

    def request(self, coords: list[LonLat]) -> tuple[dict, bool]:
        params = {
            "geometries": "geojson", "overview": "full", "steps": "true", "tidy": "false",
            "radiuses": ";".join(f"{self.config.radius_m:g}" for _ in coords),
        }
        path = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
        key = hashlib.sha256(json.dumps([self.config.profile, path, params]).encode()).hexdigest()[:32]
        cache = self.config.cache_dir / f"{key}.json" if self.config.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8")), True
        query = urllib.parse.urlencode({**params, "access_token": self.config.token})
        url = f"{API_URL}/{self.config.profile}/{path}.json?{query}"
        data = self._get(url)
        if cache and data.get("code") in ("Ok", "NoMatch"):
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        return data, False

    def _get(self, url: str) -> dict:
        for attempt in range(4):
            with self._lock:
                wait = self._last + self.min_interval_s - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:  # never include the URL: it contains the token
                body = exc.read().decode("utf-8", "replace")
                try:
                    message = json.loads(body).get("message", body)
                except ValueError:
                    message = body[:200]
                if exc.code == 429 and attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if exc.code == 422:  # e.g. NoSegment / InvalidInput: a valid "no match" answer
                    return {"code": "InvalidInput", "message": message}
                raise MapboxError(f"HTTP {exc.code}: {message}") from None
            except urllib.error.URLError as exc:
                raise MapboxError(f"network error: {exc.reason}") from None
        raise MapboxError("rate limited")


def limit_coordinates(coords: list[LonLat], net: RoadNetwork, limit: int = MAX_COORDINATES) -> list[LonLat]:
    """Generalise long linestrings to the API limit (Douglas-Peucker, growing tolerance)."""
    if len(coords) <= limit:
        return list(coords)
    line = LineString(net.proj.coords_to_xy(coords))
    tol = 0.5
    while True:
        xy = list(line.simplify(tol, preserve_topology=False).coords)
        if len(xy) <= limit:
            return net.proj.coords_to_lonlat(xy)
        tol *= 1.5


def parse_response(data: dict) -> tuple[list[MapboxSegment], list[float], int]:
    """Split the matched geometry at every intersection; each piece gets that intersection's class."""
    segments: list[MapboxSegment] = []
    confidence = []
    for matching in data.get("matchings", []):
        confidence.append(float(matching.get("confidence", 0.0)))
        road_class = None
        for leg in matching.get("legs", []):
            for step in leg.get("steps", []):
                coords = [tuple(c) for c in step.get("geometry", {}).get("coordinates", [])]
                if len(coords) < 2:
                    continue
                cuts = []
                start = 0
                for inter in step.get("intersections", []):
                    loc = tuple(inter["location"])
                    idx = min(range(start, len(coords)),
                              key=lambda i: (coords[i][0] - loc[0]) ** 2 + (coords[i][1] - loc[1]) ** 2)
                    road_class = inter.get("mapbox_streets_v8", {}).get("class", road_class)
                    cuts.append((idx, road_class, "tunnel" in inter.get("classes", [])))
                    start = idx
                if not cuts or cuts[0][0] != 0:
                    cuts.insert(0, (0, road_class, False))
                for (a, cls, tunnel), (b, _, _) in zip(cuts, [*cuts[1:], (len(coords) - 1, None, False)]):
                    if b > a:
                        segments.append(MapboxSegment(list(coords[a:b + 1]), cls, tunnel,
                                                      step.get("name", ""), step.get("ref", "")))
    matched = sum(1 for t in data.get("tracepoints", []) if t)
    return segments, confidence, matched


# ---------------------------------------------------------------------- transfer to OSM
def transfer_to_osm(segments: list[MapboxSegment], net: RoadNetwork, step: float = 15.0, radius: float = 25.0,
                    class_penalty: float = 15.0, tunnel_penalty: float = 5.0) -> PathResult | None:
    """Express a Mapbox result as an OSM path.

    The Mapbox geometry is matched onto OSM with the HMM matcher (it is accurate geometry on a
    different map, so this is the easy case for an HMM). Mapbox's own answer about *which*
    road it matched enters as an emission penalty: an OSM line of a different road class (FRC)
    counts `class_penalty` metres further away, a tunnel mismatch `tunnel_penalty` metres."""
    pts: list = []
    attrs: list[tuple[int | None, bool]] = []
    for seg in segments:
        for p in densify(net.proj.coords_to_xy(seg.coords), step):
            if pts and abs(pts[-1][0] - p[0]) + abs(pts[-1][1] - p[1]) < 0.5:
                continue
            pts.append(p)
            attrs.append((seg.frc, seg.tunnel))
    if len(pts) < 2:
        return None

    def penalty(i: int, line: Line) -> float:
        want, tunnel = attrs[i]
        cost = 0.0
        if want is not None and int(line.frc) != want:
            cost += class_penalty if min(want, int(line.frc)) < 5 else class_penalty / 3
        if tunnel != (line.level < 0):
            cost += tunnel_penalty
        return cost

    return hmm_path(pts, net, radius=radius, step=step, penalty=penalty)


def match_mapbox(polyline: list[LonLat], net: RoadNetwork, client: MapboxClient) -> MapboxMatch:
    coords = limit_coordinates(polyline, net)
    result = MapboxMatch(client.config.profile, len(coords))
    try:
        data, result.cached = client.request(coords)
    except MapboxError as exc:
        result.error = str(exc)
        return result
    if data.get("code") != "Ok":
        result.error = f"{data.get('code')}: {data.get('message', 'no match')}"
        return result
    result.segments, result.confidence, result.matched = parse_response(data)
    result.path = transfer_to_osm(result.segments, net)
    if result.path is None:
        result.error = "no OSM road near the Mapbox geometry"
    return result
