"""Browser back end, without Mapbox."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from openlr_demo.webapp import App, BadRequest, make_handler


@pytest.fixture(scope="module")
def app(osm):
    return App(osm, mapbox=None)


@pytest.fixture(scope="module")
def server(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_config(app):
    cfg = app.config()
    assert {p["key"] for p in cfg["presets"]} >= {"shuto3-elevated", "c2-tunnel"}
    methods = {m["key"]: m["available"] for m in cfg["methods"]}
    assert methods == {"openlr": True, "nearest": True, "hmm": True, "mapbox": False}
    assert cfg["mapbox"] is None


def test_nearby_lists_both_stacked_roads(app):
    p = next(p for p in app.config()["presets"] if p["key"] == "shuto3-elevated")["points"][1]
    roads = app.nearby(p["lat"], p["lon"])["roads"]
    assert {0, 1} <= {r["frc"] for r in roads}
    assert len({r["level"] for r in roads}) > 1


def test_run_preset(app):
    preset = next(p for p in app.config()["presets"] if p["key"] == "shuto3-elevated")
    out = app.run({"points": preset["points"], "methods": ["openlr", "hmm", "mapbox"]})
    json.dumps(out)
    methods = {m["key"]: m for m in out["methods"]}
    assert set(methods) == {"openlr", "hmm"}
    assert methods["openlr"]["correct"] and not methods["hmm"]["correct"]
    assert any("Mapbox is not configured" in n for n in out["route"]["notes"])
    assert len(out["openlr"]["lrps"]) >= 2 and out["openlr"]["bytes"] < 40
    assert len(out["profile"]["distance"]) == len(out["profile"]["methods"]["openlr"])


@pytest.mark.parametrize("body", [
    {"points": [{"lat": 35.0, "lon": 139.0}, {"lat": 35.65, "lon": 139.69}]},
    {"points": [{"lat": 35.65, "lon": 139.69}]},
    {"points": [{"lat": 35.65, "lon": 139.69, "frc": 9}, {"lat": 35.651, "lon": 139.691}]},
    {"points": [{"lat": "x", "lon": 139.69}, {"lat": 35.651, "lon": 139.691}]},
    {"points": [{"lat": 35.65, "lon": 139.69}, {"lat": 35.651, "lon": 139.691}], "difference": {"shift": "far"}},
])
def test_bad_requests(app, body):
    with pytest.raises(BadRequest):
        app.run(body)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post(url, data: bytes):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_http(server, app):
    status, body = get(server + "/")
    assert status == 200 and b"/app.js" in body
    assert get(server + "/app.js")[0] == 200
    assert get(server + "/api/config")[0] == 200
    assert get(server + "/.env")[0] == 404
    assert get(server + "/static/../../../.env")[0] == 404
    assert get(server + "/api/nearby?lat=x")[0] == 400

    assert post(server + "/api/run", b"not json")[0] == 400
    status, out = post(server + "/api/run", json.dumps({"points": [{"lat": 35.6580, "lon": 139.7040, "frc": 4},
                                                                  {"lat": 35.6459, "lon": 139.6758}]}).encode())
    assert status == 400 and "FRC4" in out["error"]
    points = app.config()["presets"][0]["points"]
    status, out = post(server + "/api/run", json.dumps({"points": points, "methods": ["openlr"]}).encode())
    assert status == 200 and out["methods"][0]["correct"]
