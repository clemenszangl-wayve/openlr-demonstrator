"""End-to-end checks on the bundled Shibuya OSM extract."""

import pytest

from openlr_demo.cli import DEFAULT_DIFFERENCE
from openlr_demo.pipeline import run_scenario
from openlr_demo.scenarios import SCENARIOS
from openlr_demo.sender_map import MapDifference, build_sender_map


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_identical_maps(osm, key):
    run = run_scenario(SCENARIOS[key], osm, osm, MapDifference(), methods=("openlr", "hmm"))
    assert run.metrics["openlr"].correct
    assert run.metrics["hmm"].correct
    assert len(run.encoded.lrps) >= 2


@pytest.fixture(scope="module")
def demo_runs(osm):
    sender = build_sender_map(osm, DEFAULT_DIFFERENCE)
    return {key: run_scenario(s, osm, sender, DEFAULT_DIFFERENCE) for key, s in SCENARIOS.items()}


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_openlr_survives_map_differences(demo_runs, key):
    assert demo_runs[key].metrics["openlr"].correct


def test_ramp_split_needs_the_extra_lrp(osm, demo_runs):
    """Two trunk_link branches at the Ikejiri split rate equally for the decoder (step 8)."""
    sender = build_sender_map(osm, DEFAULT_DIFFERENCE)
    plain = run_scenario(SCENARIOS["r246-ramp-shuto3"], osm, sender, DEFAULT_DIFFERENCE,
                         methods=("openlr",), disambiguate=False)
    assert not plain.metrics["openlr"].correct
    assert demo_runs["r246-ramp-shuto3"].metrics["openlr"].correct


@pytest.mark.parametrize("key", ["shuto3-elevated", "r246-surface", "r246-ramp-shuto3"])
def test_linestring_matchers_pick_the_wrong_level(demo_runs, key):
    """The stacked-road claim of the demo: both geometry matchers fail, OpenLR does not."""
    metrics = demo_runs[key].metrics
    assert not metrics["nearest"].correct
    assert not metrics["hmm"].correct
    assert metrics["openlr"].level_accuracy > 0.95 > metrics["hmm"].level_accuracy
