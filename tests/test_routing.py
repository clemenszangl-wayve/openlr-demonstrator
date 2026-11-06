"""Ground truth from lat/lon + FRC points."""

import pytest
from shapely.geometry import Point

from openlr_demo.evaluation import compare
from openlr_demo.routing import RoutePoint, RoutingError, route_through_points
from openlr_demo.scenarios import SCENARIOS

STACKED = [(35.6580, 139.7040), (35.6459, 139.6758)]  # Shuto 3 viaduct above Route 246


def anchor_points(net, scenario):
    truth = scenario.route(net)
    points = []
    for a in scenario.anchors:
        p = Point(net.proj.to_xy(a.lon, a.lat))
        line = min(truth.lines, key=lambda ln: ln.geom_xy.distance(p))
        points.append(RoutePoint(a.lat, a.lon, int(line.frc)))
    return truth, points


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_scenario_anchors_reproduce_the_truth(osm, key):
    truth, points = anchor_points(osm, SCENARIOS[key])
    route = route_through_points(osm, points)
    m = compare(truth, route.path)
    assert m.precision > 0.99 and m.recall > 0.99
    assert not route.notes


def test_frc_selects_the_level(osm):
    upper = route_through_points(osm, [RoutePoint(lat, lon, 0) for lat, lon in STACKED])
    lower = route_through_points(osm, [RoutePoint(lat, lon, 1) for lat, lon in STACKED])
    assert {int(ln.frc) for ln in upper.path.lines} == {0}
    assert {int(ln.frc) for ln in lower.path.lines} == {1}
    # Route 246 rises over junctions, but always stays below the viaduct
    assert max(ln.level for ln in lower.path.lines) < min(ln.level for ln in upper.path.lines)
    assert compare(upper.path, lower.path).precision == 0


def test_any_road_snaps_to_the_nearest(osm):
    route = route_through_points(osm, [RoutePoint(lat, lon) for lat, lon in STACKED])
    assert route.path.length > 2000


def test_missing_frc_is_explained(osm):
    with pytest.raises(RoutingError, match=r"point 1: no FRC4 road .*roads there: FRC0, FRC1"):
        route_through_points(osm, [RoutePoint(*STACKED[0], 4), RoutePoint(*STACKED[1], 1)])


def test_needs_two_points(osm):
    with pytest.raises(RoutingError):
        route_through_points(osm, [RoutePoint(*STACKED[0], 0)])
