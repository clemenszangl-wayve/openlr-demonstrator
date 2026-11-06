"""Browser front end (``openlr-demo serve``).

* ``/`` - split view: draw a route on OSM (Valhalla), encode it as OpenLR, decode it on the
  Mapbox map and compare the two routes.
* ``/compare`` - OpenLR vs. linestring map matching on a simulated second map.

A small standard-library HTTP server; the pages (``static/``) use MapLibre (and deck.gl).
The Mapbox token stays on the server - the browser only talks to this process, which also
proxies the Mapbox basemap tiles.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import re
import threading
import traceback
import uuid
import webbrowser
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import openlr
from shapely.geometry import Point

from . import viz
from .cli import DEFAULT_DIFFERENCE
from .crossmap import ROUTING_API_LRP_DISTANCE, RoundTrip, encode_path, round_trip
from .encoder import MAX_LRP_DISTANCE, EncodedLocation, EncodingError
from .evaluation import PathResult, level_profile
from .geo import bearing, point_along, polyline_length
from .mapbox import MapboxClient, MapboxError
from .mapbox_decoder import MapboxDecoder
from .network import RoadNetwork
from .pipeline import LOCAL_METHODS, METHODS, ScenarioRun, run_route
from .routing import RoutePoint, RoutingError, route_through_points
from .scenarios import SCENARIOS, Scenario
from .sender_map import MapDifference, build_sender_map
from .valhalla import ValhallaClient, ValhallaError, to_osm, way_agreement

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {"/": ("index.html", "text/html"), "/split.js": ("split.js", "text/javascript"),
                "/split.css": ("split.css", "text/css"), "/compare": ("compare.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"), "/app.css": ("app.css", "text/css")}
TILE_PATH = re.compile(r"^/tiles/mapbox/([a-z]+)/(\d+)/(\d+)/(\d+)\.png$")
OPENLR_PATTERN = re.compile(r"^[A-Za-z0-9+/=_-]{8,2000}$")
MIN_LRP_DISTANCE = 200.0
FRC_LABELS = {0: "motorway", 1: "trunk road", 2: "primary road", 3: "secondary road", 4: "tertiary road",
              5: "unclassified road", 6: "residential road", 7: "other road"}
MAX_POINTS = 60
MAX_BODY_BYTES = 200_000
CUSTOM = Scenario("custom", "Custom route", "Route entered in the browser", (), lambda line: True)


class BadRequest(ValueError):
    pass


def _r(value: float, digits: int = 7) -> float:
    return round(value, digits)


def _one_way(line) -> bool:
    tags = line.tags
    return (tags.get("oneway") in ("yes", "1", "-1") or tags.get("junction") == "roundabout"
            or tags.get("highway") in ("motorway", "motorway_link")) and tags.get("oneway") != "no"


def _segment_label(s) -> str:
    return f"{s.name or '(unnamed)'} [{s.road_class or '?'}{', tunnel' if s.tunnel else ''}]"


def _length(net: RoadNetwork, coords) -> float:
    return polyline_length(net.proj.coords_to_xy(coords))


def _runs(items: list[tuple[str, float]]) -> list[list]:
    """Consecutive equal labels merged: [[label, metres], ...]."""
    out: list[list] = []
    for label, length in items:
        if out and out[-1][0] == label:
            out[-1][1] += length
        else:
            out.append([label, length])
    return [[label, round(length)] for label, length in out if length >= 1]


class App:
    def __init__(self, net: RoadNetwork, mapbox: MapboxClient | None = None, valhalla: ValhallaClient | None = None):
        self.net = net
        self.mapbox = mapbox
        self.valhalla = valhalla
        self.decoder = MapboxDecoder(mapbox) if mapbox else None
        self.heights = viz.node_heights(net)
        self._senders: dict[MapDifference, RoadNetwork] = {}
        self._mapbox_clients: dict[float, MapboxClient] = {}
        self._routes: OrderedDict[str, PathResult] = OrderedDict()
        self._lock = threading.Lock()
        self.presets = self._presets()

    # ------------------------------------------------------------------ GET /api/config
    def config(self) -> dict:
        lon_min, lat_min, lon_max, lat_max = self.net.meta["bbox"]
        d = DEFAULT_DIFFERENCE
        return {
            "bbox": [lon_min, lat_min, lon_max, lat_max],
            "presets": self.presets,
            "frc_labels": FRC_LABELS,
            "methods": [{"key": k, "name": METHODS[k], "short": viz.SHORT[k], "color": viz.COLORS[k],
                         "available": k != "mapbox" or self.mapbox is not None} for k in METHODS],
            "colors": {k: viz.COLORS[k] for k in ("truth", "polyline")},
            "difference": {"shift": d.shift_m, "bearing": d.shift_bearing, "warp": d.warp_m, "simplify": d.simplify_m},
            "mapbox": None if self.mapbox is None else {"profile": self.mapbox.config.profile,
                                                         "radius": self.mapbox.config.radius_m},
            "level_height": viz.LEVEL_HEIGHT_M,
            "data": {k: self.net.meta.get(k) for k in ("source", "extract", "replication_timestamp")},
            "valhalla": None if self.valhalla is None else {"url": self.valhalla.url},
            "tile_styles": list(MapboxClient.TILE_STYLES) if self.mapbox else [],
            "max_lrp_distance": {"default": ROUTING_API_LRP_DISTANCE, "min": MIN_LRP_DISTANCE, "max": MAX_LRP_DISTANCE},
        }

    def _presets(self) -> list[dict]:
        out = []
        for sc in SCENARIOS.values():
            truth = sc.route(self.net)
            points = []
            for a in sc.anchors:
                p = Point(self.net.proj.to_xy(a.lon, a.lat))
                line = min(truth.lines, key=lambda ln: ln.geom_xy.distance(p))
                points.append({"lat": a.lat, "lon": a.lon, "frc": int(line.frc)})
            out.append({"key": sc.key, "title": sc.title, "description": sc.description, "points": points,
                        "route_points": self._route_points(truth)})
        return out

    def _route_points(self, truth: PathResult) -> list[dict]:
        """Start and end of a scenario route, exactly on its carriageway, for Valhalla.

        The scenario anchors are only approximate (the local router also filters by road name);
        Valhalla snaps by road class and heading alone, so through points between stacked roads
        would be ambiguous.
        """
        first, last = truth.lines[0], truth.lines[-1]
        out = []
        for line, d in ((first, truth.p_off), (last, last.length - truth.n_off)):
            d = min(max(d, 1.0), line.length - 1.0)
            a, b = point_along(line.xy, d - 1.0), point_along(line.xy, d + 1.0)
            lon, lat = self.net.proj.to_lonlat(*point_along(line.xy, d))
            out.append({"lat": round(lat, 6), "lon": round(lon, 6), "frc": int(line.frc),
                        "heading": round(bearing(a, b))})
        return out

    # ------------------------------------------------------------------ GET /api/nearby
    def nearby(self, lat: float, lon: float, radius: float = 40.0) -> dict:
        """Distinct roads near a clicked point - on stacked roads the user picks one."""
        roads: dict[tuple, dict] = {}
        xy = self.net.proj.to_xy(lon, lat)
        for line, d in self.net.lines_near(xy, radius):
            heading = None
            if _one_way(line):  # direction of travel matters: carriageways, ramps
                at = line.geom_xy.project(Point(xy))
                heading = round(bearing(point_along(line.xy, at - 2.0), point_along(line.xy, at + 2.0)))
            key = (int(line.frc), line.level, line.name, None if heading is None else round(heading / 30) % 12)
            if key not in roads:
                roads[key] = {"frc": int(line.frc), "level": line.level, "name": line.name, "heading": heading,
                              "highway": line.tags.get("highway", ""), "distance": round(d, 1)}
        return {"roads": sorted(roads.values(), key=lambda r: r["distance"])[:10]}

    # ------------------------------------------------------------------ POST /api/route (split view, left)
    def route(self, body: dict) -> dict:
        """Route through the points with Valhalla, place it on the OSM network, encode it."""
        if self.valhalla is None:
            raise BadRequest("Valhalla is not configured (VALHALLA_URL)")
        points = self._points(body.get("points"))
        spacing = self._number(body.get("max_lrp_distance"), ROUTING_API_LRP_DISTANCE, MIN_LRP_DISTANCE,
                               MAX_LRP_DISTANCE)
        vroute = self.valhalla.route(points)
        with self._lock:
            path = to_osm(vroute, self.net)
            encoded = encode_path(self.net, path, bool(body.get("disambiguate", True)), spacing)
            route_id = uuid.uuid4().hex[:12]
            self._routes[route_id] = path
            while len(self._routes) > 50:
                self._routes.popitem(last=False)
        return {
            "route_id": route_id,
            "valhalla": {"shape": [[_r(lon), _r(lat)] for lon, lat in vroute.shape], "length": round(vroute.length_m),
                         "cached": vroute.cached, "url": self.valhalla.url,
                         "classes": _runs([(e.get("road_class", "?"), 1000 * e.get("length", 0.0))
                                           for e in vroute.edges])},
            "osm": {"paths": self._osm_paths(path), "length": round(path.length),
                    "way_agreement": round(way_agreement(vroute, path), 4)},
            "openlr": self._encoded(encoded),
        }

    def _encoded(self, e: EncodedLocation) -> dict:
        return {"binary": e.binary, "bytes": len(base64.b64decode(e.binary)), "steps": e.steps,
                "p_off": e.p_off, "n_off": e.n_off,
                "lrps": [{"index": i + 1, "lon": _r(p.lon), "lat": _r(p.lat), "frc": p.frc.name, "fow": p.fow.name,
                          "bear": p.bear, "lfrcnp": None if p.is_last else p.lfrcnp.name,
                          "dnp": None if p.is_last else p.dnp, "valid": p.node_valid, "line": p.line.label}
                         for i, p in enumerate(e.lrps)]}

    def _osm_paths(self, path: PathResult | None) -> list[dict]:
        if path is None:
            return []
        return [{"path": [[_r(lon), _r(lat)] for lon, lat in self.net.proj.coords_to_lonlat(piece)],
                 "level": line.level, "frc": int(line.frc), "label": f"{line.label} - {line.frc.name}/{line.fow.name}"}
                for line, piece in path.line_pieces()]

    # ------------------------------------------------------------------ POST /api/decode (split view, right)
    def decode(self, body: dict) -> dict:
        """Decode an OpenLR code on OSM and on the Mapbox map and compare with the original route."""
        if self.decoder is None:
            raise BadRequest("Mapbox is not configured (MAPBOX_ACCESS_TOKEN in .env)")
        binary = str(body.get("binary", "")).strip()
        if not OPENLR_PATTERN.match(binary):
            raise BadRequest("expected a base64 OpenLR line location")
        try:
            base64.b64decode(binary, validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("the OpenLR code is not valid base64") from None
        try:
            reference = openlr.binary_decode(binary)
        except Exception as exc:  # the openlr package raises various types for malformed input
            raise BadRequest(f"cannot read this OpenLR code: {exc}") from None
        if not isinstance(reference, openlr.LineLocationReference):
            raise BadRequest(f"only line locations are supported, not {type(reference).__name__}")
        original = self._routes.get(str(body.get("route_id", "")))
        before = self.mapbox.requests
        with self._lock:
            rt = round_trip(self.net, binary, self.decoder, original)
        return self._roundtrip(rt, self.mapbox.requests - before)

    def _roundtrip(self, rt: RoundTrip, requests: int) -> dict:
        ref, mb = rt.local.reference, rt.mapbox
        chosen = {(leg.start.lrp, leg.start.lon, leg.start.lat) for leg in mb.legs}
        chosen |= {(leg.end.lrp, leg.end.lon, leg.end.lat) for leg in mb.legs}
        acc = rt.accuracy
        return {
            "binary": rt.binary,
            "reference": {"poffs": ref.poffs, "noffs": ref.noffs, "points": [
                {"index": i + 1, "lon": p.lon, "lat": p.lat, "frc": p.frc.name, "fow": p.fow.name, "bear": p.bear,
                 "lfrcnp": None if i == len(ref.points) - 1 else p.lfrcnp.name,
                 "dnp": None if i == len(ref.points) - 1 else p.dnp} for i, p in enumerate(ref.points)]},
            "original": {"paths": self._osm_paths(rt.original), "length": round(rt.original.length)}
            if rt.original else None,
            "osm_decoded": {"paths": self._osm_paths(rt.local.result), "error": rt.local.error,
                            "length": round(rt.local.result.length) if rt.local.result else None},
            "mapbox": {
                "profile": self.mapbox.config.profile, "checked": mb.checked, "error": mb.error,
                "p_off": round(mb.p_off, 1), "n_off": round(mb.n_off, 1), "requests": requests,
                "candidates": [[{"lon": c.lon, "lat": c.lat, "label": c.label, "class": c.road_class,
                                 "frc": c.frc, "distance": round(c.distance, 1), "score": round(c.score, 3),
                                 "chosen": (c.lrp, c.lon, c.lat) in chosen} for c in cands]
                               for cands in mb.candidates],
                "attempts": [{"from": a.index + 1, "to": a.index + 2, "start": a.start.label, "end": a.end.label,
                              "status": a.status, "length": round(a.length), "dnp": a.dnp,
                              "start_class": a.start_class, "bearing": a.bearing, "cached": a.cached}
                             for a in mb.attempts],
                "segments": [{"path": [[_r(lon), _r(lat)] for lon, lat in s.coords], "class": s.road_class,
                              "frc": s.frc, "tunnel": s.tunnel, "label": _segment_label(s)} for s in mb.segments],
                "classes": _runs([(s.road_class or "?", _length(self.net, s.coords)) for s in mb.segments]),
            },
            "transferred": self._osm_paths(rt.transferred),
            "differences": self._differences(rt.reference, rt.transferred),
            "accuracy": None if acc is None else {"precision": acc.precision, "recall": acc.recall,
                                                  "level_accuracy": acc.level_accuracy, "correct": acc.correct},
            "reference_kind": "original" if rt.original else "osm_decoded",
            "similarity": rt.similarity,
            "similar": rt.similar,
            "seconds": {k: round(v, 3) for k, v in rt.seconds.items()},
        }

    def _differences(self, reference: PathResult | None, decoded: PathResult | None) -> dict:
        """OSM lines only in the decoded route (``wrong``) or only in the reference (``missed``)."""
        if reference is None or decoded is None:
            return {"wrong": [], "missed": [], "wrong_roads": [], "missed_roads": []}
        ref_ids, dec_ids = {ln.id for ln in reference.lines}, {ln.id for ln in decoded.lines}

        def only(path: PathResult, other: set) -> tuple[list[dict], list[list]]:
            paths, roads = [], {}
            for line, piece in path.line_pieces():
                if line.id in other:
                    continue
                paths.append({"path": [[_r(lon), _r(lat)] for lon, lat in self.net.proj.coords_to_lonlat(piece)],
                              "level": line.level, "label": line.label})
                key = (line.name, line.level)
                roads[key] = roads.get(key, 0.0) + polyline_length(piece)
            return paths, [[name, level, round(m)] for (name, level), m in roads.items() if m >= 20]

        wrong, wrong_roads = only(decoded, ref_ids)
        missed, missed_roads = only(reference, dec_ids)
        return {"wrong": wrong, "missed": missed, "wrong_roads": wrong_roads, "missed_roads": missed_roads}

    # ------------------------------------------------------------------ GET /tiles/mapbox/...
    def tile(self, style: str, z: int, x: int, y: int) -> bytes:
        if self.mapbox is None:
            raise BadRequest("Mapbox is not configured")
        if style not in MapboxClient.TILE_STYLES or not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise BadRequest("invalid tile")
        return self.mapbox.tile(style, z, x, y)

    @staticmethod
    def _number(value, default: float, lo: float, hi: float) -> float:
        if value in (None, ""):
            return default
        try:
            return min(hi, max(lo, float(value)))
        except (TypeError, ValueError):
            raise BadRequest("expected a number") from None

    # ------------------------------------------------------------------ POST /api/run
    def run(self, body: dict) -> dict:
        points = self._points(body.get("points"))
        diff = self._difference(body.get("difference") or {})
        methods = [m for m in body.get("methods") or LOCAL_METHODS if m in METHODS]
        notes = []
        mapbox = None
        if "mapbox" in methods:
            mapbox = self._mapbox(body.get("mapbox_radius"))
            if mapbox is None:
                methods.remove("mapbox")
                notes.append("Mapbox is not configured (MAPBOX_ACCESS_TOKEN in .env)")
        with self._lock:
            route = route_through_points(self.net, points)
            sender = self._senders.get(diff)
            if sender is None:
                if len(self._senders) > 8:
                    self._senders.clear()
                sender = self._senders[diff] = build_sender_map(self.net, diff)
            run = run_route(CUSTOM, route.path, self.net, sender, diff, methods,
                            bool(body.get("disambiguate", True)), mapbox)
            return self._serialize(run, route, notes)

    def _points(self, raw) -> list[RoutePoint]:
        if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_POINTS:
            raise BadRequest(f"send between 2 and {MAX_POINTS} points")
        lon_min, lat_min, lon_max, lat_max = self.net.meta["bbox"]
        points = []
        for i, p in enumerate(raw, 1):
            try:
                lat, lon = float(p["lat"]), float(p["lon"])
                frc = p.get("frc")
                frc = None if frc in (None, "", "any") else int(frc)
                heading = p.get("heading")
                heading = None if heading in (None, "") else float(heading) % 360
            except (KeyError, TypeError, ValueError):
                raise BadRequest(f"point {i}: expected lat, lon and optionally frc 0-7 and heading") from None
            if frc is not None and not 0 <= frc <= 7:
                raise BadRequest(f"point {i}: FRC must be between 0 and 7")
            if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
                raise BadRequest(f"point {i} ({lat:.5f}, {lon:.5f}) is outside the OSM extract "
                                 f"({lat_min}-{lat_max} N, {lon_min}-{lon_max} E)")
            points.append(RoutePoint(lat, lon, frc, heading))
        return points

    @staticmethod
    def _difference(d: dict) -> MapDifference:
        def num(key, default, lo, hi):
            try:
                return min(hi, max(lo, float(d.get(key, default))))
            except (TypeError, ValueError):
                raise BadRequest(f"{key} must be a number") from None
        base = DEFAULT_DIFFERENCE
        return MapDifference(num("shift", base.shift_m, 0, 50), num("bearing", base.shift_bearing, 0, 360) % 360,
                             num("warp", base.warp_m, 0, 20), base.warp_wavelength_m,
                             num("simplify", base.simplify_m, 0, 20), int(num("seed", base.seed, 0, 10_000)))

    def _mapbox(self, radius) -> MapboxClient | None:
        if self.mapbox is None:
            return None
        try:
            radius = self.mapbox.config.radius_m if radius in (None, "") else min(50.0, max(1.0, float(radius)))
        except (TypeError, ValueError):
            raise BadRequest("Mapbox radius must be a number") from None
        if radius == self.mapbox.config.radius_m:
            return self.mapbox
        if radius not in self._mapbox_clients:
            self._mapbox_clients[radius] = MapboxClient(dataclasses.replace(self.mapbox.config, radius_m=radius))
        return self._mapbox_clients[radius]

    # ------------------------------------------------------------------ serialisation
    def _paths(self, result: PathResult, lift: float = 0.0) -> list[dict]:
        return [{"path": viz.path3d(self.net, line, piece, self.heights, lift), "level": line.level,
                 "label": f"{line.label} - {line.frc.name}/{line.fow.name}"}
                for line, piece in result.line_pieces()]

    def _serialize(self, run: ScenarioRun, route, notes: list[str]) -> dict:
        net, e = self.net, run.encoded
        lifts = {"openlr": 3.0, "hmm": 6.0, "nearest": 9.0, "mapbox": 12.0}
        methods = []
        for key, result in run.results.items():
            m = run.metrics[key]
            item = {"key": key, "name": METHODS[key], "short": viz.SHORT[key], "color": viz.COLORS[key],
                    "correct": m.correct, "precision": m.precision, "recall": m.recall,
                    "level_accuracy": m.level_accuracy, "ms": round(1000 * run.seconds.get(key, 0.0)),
                    "paths": self._paths(result, lifts[key]) if result else [],
                    "roads": viz.roads_used(result), "error": None}
            if key == "openlr" and run.decoded.error:
                item["error"] = run.decoded.error
            if key == "mapbox" and run.mapbox:
                mb = run.mapbox
                item["error"] = mb.error
                item["mapbox"] = {
                    "profile": mb.profile, "confidence": mb.confidence, "matched": mb.matched, "sent": mb.sent,
                    "cached": mb.cached, "classes": [[c, round(length)] for c, length in mb.class_runs(net)],
                    "raw": [{"path": [[_r(lon), _r(lat)] for lon, lat in s.coords], "label":
                             f"Mapbox: {s.road_class or '?'}{' tunnel' if s.tunnel else ''} - {s.name or '-'}"}
                            for s in mb.segments],
                }
            methods.append(item)

        profile = level_profile(run.truth, run.truth)
        lrps = []
        for i, (lrp, point) in enumerate(zip(e.lrps, run.decoded.reference.points)):
            lrps.append({
                "index": i + 1, "lon": point.lon, "lat": point.lat, "z": self.heights.get(lrp.node, 0.0),
                "frc": lrp.frc.name, "fow": lrp.fow.name, "bear": lrp.bear, "bear_sector": point.bear,
                "lfrcnp": None if lrp.is_last else lrp.lfrcnp.name, "dnp": None if lrp.is_last else lrp.dnp,
                "dnp_binary": None if lrp.is_last else point.dnp, "valid": lrp.node_valid, "line": lrp.line.label,
                "candidates": [{"score": round(c.score, 3), "label": c.line.label,
                                "attrs": f"{c.line.frc.name}/{c.line.fow.name}"}
                               for c in run.decoded.candidates.get(i, [])[:5]],
            })
        return {
            "difference": run.difference.describe(),
            "truth": {"paths": self._paths(run.truth), "length": round(run.truth.length),
                      "levels": sorted({ln.level for ln in run.truth.lines})},
            "route": {
                "points": [{"lat": s.point.lat, "lon": s.point.lon, "frc": s.point.frc, "road": s.line.name,
                            "road_frc": int(s.line.frc), "level": s.line.level, "distance": round(s.distance, 1)}
                           for s in route.snapped],
                "notes": [*route.notes, *notes],
            },
            "network": [{"path": viz.path3d(net, ln, ln.xy, self.heights), "level": ln.level,
                         "label": f"{ln.label} - {ln.frc.name}/{ln.fow.name}"}
                        for ln in viz.context_lines(run, net, 300.0)],
            "polyline": [[lon, lat] for lon, lat in run.polyline],
            "openlr": {"binary": e.binary, "bytes": len(base64.b64decode(e.binary)), "lrps": lrps,
                       "steps": e.steps, "p_off": e.p_off, "n_off": e.n_off},
            "methods": methods,
            "profile": {"distance": [round(d) for d, _, _ in profile], "truth": [t for _, t, _ in profile],
                        "methods": {key: [lv for _, _, lv in level_profile(run.truth, result)]
                                    for key, result in run.results.items()}},
        }


# ---------------------------------------------------------------------- HTTP
def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "openlr-demo"

        def log_message(self, fmt, *args):  # quiet; errors are reported in the response
            pass

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict):
            self._send(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
                       "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in STATIC_FILES:
                name, ctype = STATIC_FILES[url.path]
                return self._send(HTTPStatus.OK, (STATIC_DIR / name).read_bytes(), ctype)
            if url.path == "/api/config":
                return self._json(HTTPStatus.OK, app.config())
            if url.path == "/api/nearby":
                q = parse_qs(url.query)
                try:
                    lat, lon = float(q["lat"][0]), float(q["lon"][0])
                except (KeyError, ValueError):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "lat and lon required"})
                return self._json(HTTPStatus.OK, app.nearby(lat, lon))
            tile = TILE_PATH.match(url.path)
            if tile:
                return self._tile(tile.group(1), *(int(v) for v in tile.groups()[1:]))
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _tile(self, style: str, z: int, x: int, y: int):
            try:
                data = app.tile(style, z, x, y)
            except BadRequest as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except MapboxError as exc:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            endpoints = {"/api/run": app.run, "/api/route": app.route, "/api/decode": app.decode}
            endpoint = endpoints.get(urlparse(self.path).path)
            if endpoint is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise BadRequest("expected a JSON object")
                return self._json(HTTPStatus.OK, endpoint(body))
            except (BadRequest, RoutingError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except ValhallaError as exc:
                return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            except MapboxError as exc:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Mapbox: {exc}"})
            except EncodingError as exc:
                return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": f"OpenLR encoding failed: {exc}"})
            except Exception as exc:  # report, keep serving
                traceback.print_exc()
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(app: App, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{server.server_port}/"
    if host not in ("127.0.0.1", "localhost") and app.mapbox:
        print("Warning: the server is reachable from the network and makes Mapbox requests with your token.")
    print(f"Serving on {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
