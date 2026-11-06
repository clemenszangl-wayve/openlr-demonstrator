"""End-to-end run of one route: route on map A -> {OpenLR, linestring} -> map B (OSM)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .decoder import DecodedLocation, decode_binary
from .encoder import EncodedLocation, LineEncoder
from .evaluation import Metrics, PathResult, compare
from .geo import LonLat
from .mapbox import MapboxClient, MapboxMatch, match_mapbox
from .matchers import match_hmm, match_nearest
from .network import RoadNetwork
from .scenarios import Scenario
from .sender_map import MapDifference

METHODS = {
    "openlr": "OpenLR (whitepaper encoder + TomTom dereferencer)",
    "nearest": "Linestring: nearest-road snapping",
    "hmm": "Linestring: HMM map matching (Newson & Krumm)",
    "mapbox": "Linestring: Mapbox Map Matching API",
}
LOCAL_METHODS = ("openlr", "nearest", "hmm")


def default_methods(mapbox: MapboxClient | None) -> tuple[str, ...]:
    return (*LOCAL_METHODS, "mapbox") if mapbox else LOCAL_METHODS


@dataclass
class ScenarioRun:
    scenario: Scenario
    difference: MapDifference
    truth: PathResult  # on the receiver map (OSM)
    sender_route: PathResult  # the same route on map A
    polyline: list[LonLat]  # what the linestring approach transmits
    encoded: EncodedLocation
    decoded: DecodedLocation
    results: dict[str, PathResult | None] = field(default_factory=dict)
    metrics: dict[str, Metrics] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    mapbox: MapboxMatch | None = None


def route_polyline(route: PathResult, net: RoadNetwork, precision: int = 5) -> list[LonLat]:
    """Route geometry as transmitted by a linestring approach (1e-5 deg ~ 1 m precision)."""
    coords = []
    for piece in route.pieces_xy():
        for x, y in piece:
            lon, lat = net.proj.to_lonlat(x, y)
            p = (round(lon, precision), round(lat, precision))
            if not coords or coords[-1] != p:
                coords.append(p)
    return coords


def run_route(scenario: Scenario, truth: PathResult, receiver: RoadNetwork, sender: RoadNetwork,
              difference: MapDifference, methods=LOCAL_METHODS, disambiguate: bool = True,
              mapbox: MapboxClient | None = None) -> ScenarioRun:
    """`truth` is the route on the receiver map; map A shares the line IDs with it."""
    s_lines = [sender.lines[ln.id] for ln in truth.lines]
    p_off = truth.p_off * s_lines[0].length / truth.lines[0].length
    n_off = truth.n_off * s_lines[-1].length / truth.lines[-1].length
    sender_route = PathResult(s_lines, p_off, n_off)
    polyline = route_polyline(sender_route, sender)

    t0 = time.perf_counter()
    encoded = LineEncoder(sender, disambiguate=disambiguate).encode(s_lines, p_off, n_off)
    t1 = time.perf_counter()
    decoded = decode_binary(encoded.binary, receiver)
    t2 = time.perf_counter()
    run = ScenarioRun(scenario, difference, truth, sender_route, polyline, encoded, decoded)
    run.seconds["encode"] = t1 - t0
    if "openlr" in methods:
        run.results["openlr"] = decoded.result
        run.seconds["openlr"] = t2 - t1
    if "nearest" in methods:
        t = time.perf_counter()
        run.results["nearest"] = match_nearest(polyline, receiver)
        run.seconds["nearest"] = time.perf_counter() - t
    if "hmm" in methods:
        t = time.perf_counter()
        run.results["hmm"] = match_hmm(polyline, receiver)
        run.seconds["hmm"] = time.perf_counter() - t
    if "mapbox" in methods:
        if mapbox is None:
            raise ValueError("Mapbox is not configured (MAPBOX_ACCESS_TOKEN)")
        t = time.perf_counter()
        run.mapbox = match_mapbox(polyline, receiver, mapbox)
        run.results["mapbox"] = run.mapbox.path
        run.seconds["mapbox"] = time.perf_counter() - t
    run.metrics = {m: compare(truth, r) for m, r in run.results.items()}
    return run


def run_scenario(scenario: Scenario, receiver: RoadNetwork, sender: RoadNetwork,
                 difference: MapDifference, methods=LOCAL_METHODS, disambiguate: bool = True,
                 mapbox: MapboxClient | None = None) -> ScenarioRun:
    return run_route(scenario, scenario.route(receiver), receiver, sender, difference, methods,
                     disambiguate, mapbox)
