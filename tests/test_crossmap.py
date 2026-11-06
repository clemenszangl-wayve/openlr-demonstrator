"""Split view: Valhalla route on OSM -> OpenLR -> decoded on the Mapbox map, from recorded API answers.

``tests/data/recorded`` holds the Valhalla and Mapbox responses (no token) that the clients replay
with ``offline=True``; nothing here touches the network.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from openlr_demo.evaluation import compare
from openlr_demo.mapbox import MapboxClient, MapboxConfig
from openlr_demo.routing import RoutePoint
from openlr_demo.scenarios import SCENARIOS
from openlr_demo.similarity import Piece, compare_routes, frechet, similar
from openlr_demo.valhalla import DEFAULT_URL, ValhallaClient, ValhallaError, decode_polyline
from openlr_demo.webapp import App, BadRequest, make_handler

DATA = Path(__file__).parent / "data" / "recorded"
PROFILE = "andrewwayve.zenrin-premium/driving"


@pytest.fixture(scope="module")
def app(osm):
    mapbox = MapboxClient(MapboxConfig(token="unused", profile=PROFILE, cache_dir=DATA / "mapbox"), offline=True)
    return App(osm, mapbox, ValhallaClient(DEFAULT_URL, DATA / "valhalla", offline=True))


def preset(app, key):
    return next(p for p in app.presets if p["key"] == key)["route_points"]


@pytest.fixture(scope="module")
def exit_runs(app):
    out = {}
    for spacing in (15000, 2000):
        route = app.route({"points": preset(app, "shuto3-exit"), "max_lrp_distance": spacing})
        out[spacing] = route, app.decode({"binary": route["openlr"]["binary"], "route_id": route["route_id"]})
    return out


# ---------------------------------------------------------------------- Valhalla
def test_polyline_decode():
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5) == [
        (-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)]


def test_presets_start_and_end_on_the_route(app, osm):
    for p in app.presets:
        points = p["route_points"]
        assert len(points) == 2 and all(0 <= q["heading"] < 360 for q in points)
        truth = SCENARIOS[p["key"]].route(osm)
        assert points[0]["frc"] == int(truth.lines[0].frc) and points[1]["frc"] == int(truth.lines[-1].frc)


def test_valhalla_route_is_placed_on_the_same_osm_ways(app, osm, exit_runs):
    route, _ = exit_runs[2000]
    assert route["osm"]["way_agreement"] > 0.99
    assert [c for c, _ in route["valhalla"]["classes"]] == ["motorway", "trunk"]
    path = app._routes[route["route_id"]]
    m = compare(SCENARIOS["shuto3-exit"].route(osm), path)
    assert m.precision > 0.99 and m.recall > 0.99 and m.level_accuracy > 0.95


def test_valhalla_offline_miss_is_an_error():
    client = ValhallaClient(DEFAULT_URL, DATA / "valhalla", offline=True)
    with pytest.raises(ValhallaError):
        client.route([RoutePoint(35.66, 139.70, 1), RoutePoint(35.65, 139.69, 1)])


# ---------------------------------------------------------------------- decoding on Mapbox
def test_fastest_path_router_leaves_the_viaduct_with_far_apart_lrps(exit_runs):
    """Mapbox Directions minimises travel time; with 2 LRPs 3.6 km apart it takes the Shibuya
    exit and Route 246 below - same length, so every OpenLR check passes."""
    route, out = exit_runs[15000]
    assert len(route["openlr"]["lrps"]) == 2
    assert out["mapbox"]["checked"] and not out["accuracy"]["correct"] and not out["similar"]
    assert out["accuracy"]["precision"] < 0.5
    assert out["similarity"]["buffer"]["20"]["f1"] > 0.95  # in plan view the two routes look the same
    assert out["similarity"]["frc_agreement"] < 0.5
    assert any(level == 0 and "Tamagawa" in name for name, level, _ in out["differences"]["wrong_roads"])


def test_closer_lrps_pin_the_route_to_the_viaduct(exit_runs):
    route, out = exit_runs[2000]
    assert len(route["openlr"]["lrps"]) == 3
    assert out["accuracy"]["correct"] and out["similar"]
    assert out["similarity"]["buffer"]["10"]["f1"] > 0.95 and out["similarity"]["frechet"] < 10
    assert not out["differences"]["wrong_roads"]
    assert all(leg["status"] == "ok" for leg in out["mapbox"]["attempts"])
    assert out["mapbox"]["requests"] == 0  # replayed from the recordings
    json.dumps(out)


def test_pasted_code_is_compared_with_its_osm_decoding(app, exit_runs):
    out = app.decode({"binary": exit_runs[2000][0]["openlr"]["binary"]})
    assert out["original"] is None and out["reference_kind"] == "osm_decoded"
    assert out["accuracy"]["correct"]


@pytest.mark.parametrize("binary", ["", "hello", "C2NYcxlbXwE3PfKc+ecKYwM!", "AAAAAAAAAAAA", "x" * 3000])
def test_bad_codes(app, binary):
    with pytest.raises(BadRequest):
        app.decode({"binary": binary})


def test_route_needs_valhalla(osm):
    with pytest.raises(BadRequest):
        App(osm).route({"points": [{"lat": 35.65, "lon": 139.69}, {"lat": 35.651, "lon": 139.691}]})


# ---------------------------------------------------------------------- similarity metrics
def piece(xy, frc=0, tunnel=False):
    return Piece(xy, frc, tunnel)


def test_identical_routes():
    a = [piece([(0, 0), (500, 0)]), piece([(500, 0), (500, 300)])]
    m = compare_routes(a, a)
    assert m["buffer"]["5"]["f1"] == 1 and m["frechet"] == 0 and m["hausdorff"] == 0
    assert m["frc_agreement"] == 1 and m["length_ratio"] == 1 and similar(m)


def test_parallel_road_8m_away():
    m = compare_routes([piece([(0, 0), (1000, 0)])], [piece([(0, 8), (1000, 8)], frc=1)])
    assert m["buffer"]["5"]["f1"] == 0 and m["buffer"]["10"]["f1"] == 1
    assert m["frechet"] == pytest.approx(8) and m["deviation_median"] == pytest.approx(8)
    assert m["frc_agreement"] == 0 and not similar(m)  # same geometry, different road class


def test_frechet_respects_direction():
    a = [(0, 0), (100, 0), (200, 0)]
    assert frechet(a, a[::-1]) == pytest.approx(200)
    assert frechet(a, [(0, 3), (200, 3)]) == pytest.approx(100, abs=1)  # needs densified input


# ---------------------------------------------------------------------- HTTP
def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post(url, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_http(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, page = get(base + "/")
        assert status == 200 and b"/split.js" in page
        assert get(base + "/split.js")[0] == 200 and get(base + "/split.css")[0] == 200
        assert get(base + "/compare")[0] == 200
        status, config = get(base + "/api/config")
        assert b"unused" not in config and json.loads(config)["valhalla"]["url"] == DEFAULT_URL
        assert get(base + "/tiles/mapbox/streets/a/0/0.png")[0] == 404
        assert get(base + "/tiles/mapbox/evil/1/0/0.png")[0] == 400
        assert get(base + "/tiles/mapbox/streets/2/9/0.png")[0] == 400
        assert get(base + "/tiles/mapbox/streets/2/1/1.png")[0] == 502  # offline: not fetched
        status, out = post(base + "/api/route", {"points": preset(app, "shuto3-exit"), "max_lrp_distance": 2000})
        assert status == 200
        status, out = post(base + "/api/decode", {"binary": out["openlr"]["binary"], "route_id": out["route_id"]})
        assert status == 200 and out["accuracy"]["correct"]
        assert post(base + "/api/decode", {"binary": "hello"})[0] == 400
        assert post(base + "/api/route", {"points": [{"lat": 35.66, "lon": 139.70, "frc": 1},
                                                     {"lat": 35.65, "lon": 139.69, "frc": 1}]})[0] == 422
    finally:
        httpd.shutdown()
        httpd.server_close()
