"""Browser front end (``openlr-demo serve``): enter route points with an FRC, compare all methods.

A small standard-library HTTP server; the page itself (``static/``) uses MapLibre and deck.gl.
The Mapbox token stays on the server - the browser only talks to this process.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shapely.geometry import Point

from . import viz
from .cli import DEFAULT_DIFFERENCE
from .encoder import EncodingError
from .evaluation import PathResult, level_profile
from .mapbox import MapboxClient
from .network import RoadNetwork
from .pipeline import LOCAL_METHODS, METHODS, ScenarioRun, run_route
from .routing import RoutePoint, RoutingError, route_through_points
from .scenarios import SCENARIOS, Scenario
from .sender_map import MapDifference, build_sender_map

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                "/app.css": ("app.css", "text/css")}
FRC_LABELS = {0: "motorway", 1: "trunk road", 2: "primary road", 3: "secondary road", 4: "tertiary road",
              5: "unclassified road", 6: "residential road", 7: "other road"}
MAX_POINTS = 60
MAX_BODY_BYTES = 200_000
CUSTOM = Scenario("custom", "Custom route", "Route entered in the browser", (), lambda line: True)


class BadRequest(ValueError):
    pass


def _r(value: float, digits: int = 7) -> float:
    return round(value, digits)


class App:
    def __init__(self, net: RoadNetwork, mapbox: MapboxClient | None = None):
        self.net = net
        self.mapbox = mapbox
        self.heights = viz.node_heights(net)
        self._senders: dict[MapDifference, RoadNetwork] = {}
        self._mapbox_clients: dict[float, MapboxClient] = {}
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
            out.append({"key": sc.key, "title": sc.title, "description": sc.description, "points": points})
        return out

    # ------------------------------------------------------------------ GET /api/nearby
    def nearby(self, lat: float, lon: float, radius: float = 40.0) -> dict:
        """Distinct roads near a clicked point - on stacked roads the user picks one."""
        roads: dict[tuple, dict] = {}
        for line, d in self.net.lines_near(self.net.proj.to_xy(lon, lat), radius):
            key = (int(line.frc), line.level, line.name)
            if key not in roads:
                roads[key] = {"frc": int(line.frc), "level": line.level, "name": line.name,
                              "highway": line.tags.get("highway", ""), "distance": round(d, 1)}
        return {"roads": sorted(roads.values(), key=lambda r: r["distance"])[:8]}

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
            except (KeyError, TypeError, ValueError):
                raise BadRequest(f"point {i}: expected lat, lon and optionally frc 0-7") from None
            if frc is not None and not 0 <= frc <= 7:
                raise BadRequest(f"point {i}: FRC must be between 0 and 7")
            if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
                raise BadRequest(f"point {i} ({lat:.5f}, {lon:.5f}) is outside the OSM extract "
                                 f"({lat_min}-{lat_max} N, {lon_min}-{lon_max} E)")
            points.append(RoutePoint(lat, lon, frc))
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
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/run":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise BadRequest("expected a JSON object")
                return self._json(HTTPStatus.OK, app.run(body))
            except (BadRequest, RoutingError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
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
