"""Mapbox matcher on recorded answers - no network access and no token needed."""

import json
from pathlib import Path

import pytest

from openlr_demo import mapbox, settings
from openlr_demo.cli import DEFAULT_DIFFERENCE
from openlr_demo.evaluation import compare
from openlr_demo.mapbox import MapboxConfig, class_frc, limit_coordinates, match_mapbox, parse_response
from openlr_demo.pipeline import run_route, run_scenario
from openlr_demo.scenarios import SCENARIOS
from openlr_demo.sender_map import build_sender_map

DATA = Path(__file__).with_name("data")


def fixture(key: str) -> dict:
    return json.loads((DATA / f"mapbox_{key}.json").read_text(encoding="utf-8"))


class RecordedClient:
    def __init__(self, data: dict):
        self.config = MapboxConfig(token="unused", profile="test/driving", cache_dir=None)
        self.data = data
        self.requests: list = []

    def request(self, coords):
        self.requests.append(coords)
        return self.data, True


@pytest.fixture(scope="module")
def sender(osm):
    return build_sender_map(osm, DEFAULT_DIFFERENCE)


def test_class_frc():
    assert class_frc("motorway") == 0
    assert class_frc("trunk_link") == 1
    assert class_frc("primary") == 2
    assert class_frc("street") == 6
    assert class_frc(None) is None


def test_parse_splits_steps_at_intersections():
    segments, confidence, matched = parse_response(fixture("shuto3-elevated"))
    assert matched == len(fixture("shuto3-elevated")["tracepoints"])
    assert len(confidence) == 1 and 0 < confidence[0] <= 1
    assert all(len(s.coords) >= 2 for s in segments)
    assert {s.road_class for s in segments} >= {"trunk"}
    # pieces of one step share their cut vertex
    joined = sum(a.coords[-1] == b.coords[0] for a, b in zip(segments, segments[1:]))
    assert joined >= len(segments) // 2


@pytest.mark.parametrize(("key", "correct"), [("shuto3-elevated", False), ("yamate-surface", True)])
def test_recorded_answers(osm, sender, key, correct):
    """At a 5 m map difference Mapbox matches Shuto 3 onto Route 246 below it, Yamate-dori correctly."""
    run = run_route(SCENARIOS[key], SCENARIOS[key].route(osm), osm, sender, DEFAULT_DIFFERENCE,
                    ("openlr", "mapbox"), mapbox=RecordedClient(fixture(key)))
    assert run.metrics["openlr"].correct
    assert run.metrics["mapbox"].correct is correct
    assert run.mapbox.cached and not run.mapbox.error


def test_transfer_follows_the_reported_class(osm):
    """The answer says trunk: the OSM transfer must stay on Route 246 (FRC1), not the viaduct above."""
    run = match_mapbox([(139.70, 35.66), (139.69, 35.65)], osm, RecordedClient(fixture("shuto3-elevated")))
    frcs = {int(line.frc) for line in run.path.lines}
    assert 0 not in frcs
    assert 1 in frcs


def test_error_answers(osm):
    run = match_mapbox([(139.70, 35.66), (139.69, 35.65)], osm,
                       RecordedClient({"code": "NoMatch", "message": "Could not match the trace."}))
    assert run.path is None
    assert run.error.startswith("NoMatch")


def test_mapbox_needs_a_client(osm):
    with pytest.raises(ValueError):
        run_route(SCENARIOS["c2-tunnel"], SCENARIOS["c2-tunnel"].route(osm), osm, osm,
                  DEFAULT_DIFFERENCE, ("mapbox",))


def test_coordinate_limit(osm):
    truth = SCENARIOS["shuto3-elevated"].route(osm)
    coords = osm.proj.coords_to_lonlat([p for _, piece in truth.line_pieces() for p in piece])
    many = [c for c in coords for _ in range(3)]
    assert len(many) > mapbox.MAX_COORDINATES
    limited = limit_coordinates(many, osm)
    assert 2 <= len(limited) <= mapbox.MAX_COORDINATES


def test_config_from_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nMAPBOX_ACCESS_TOKEN = 'pk.test'\nMAPBOX_PROFILE=someone.custom\n", encoding="utf-8")
    monkeypatch.setattr(settings, "ENV_FILE", env)
    monkeypatch.delenv("MAPBOX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MAPBOX_PROFILE", raising=False)
    monkeypatch.delenv("MAPBOX_RADIUS", raising=False)
    config = MapboxConfig.from_env()
    assert config.token == "pk.test"
    assert config.profile == "someone.custom/driving"
    assert "pk.test" not in repr(config)
    monkeypatch.setenv("MAPBOX_ACCESS_TOKEN", "")
    assert MapboxConfig.from_env() is None


def test_scenario_runs_without_mapbox(osm, sender):
    run = run_scenario(SCENARIOS["c2-tunnel"], osm, sender, DEFAULT_DIFFERENCE)
    assert "mapbox" not in run.results
    assert compare(run.truth, run.truth).correct
