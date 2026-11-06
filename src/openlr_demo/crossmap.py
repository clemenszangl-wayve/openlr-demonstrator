"""Cross-map round trip: a route on OSM -> OpenLR -> decoded on the Mapbox map -> compared.

Unlike the simulated "map A" of the linestring comparison, encoder and decoder maps are now
genuinely different (OSM vs. the Mapbox / ZENRIN road graph).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .decoder import DecodedLocation, decode_binary
from .encoder import MAX_LRP_DISTANCE, EncodedLocation, LineEncoder
from .evaluation import Metrics, PathResult, compare
from .mapbox import transfer_to_osm
from .mapbox_decoder import MapboxDecoder, MapboxDecoding
from .network import RoadNetwork
from .similarity import Piece, compare_routes, similar

# Mapbox Directions returns the fastest route, OpenLR assumes the shortest one. Closer LRPs leave
# less room for the two to differ (see README, "fastest vs. shortest path").
ROUTING_API_LRP_DISTANCE = 2_000.0


def encode_path(net: RoadNetwork, path: PathResult, disambiguate: bool = True,
                max_lrp_distance: float = MAX_LRP_DISTANCE) -> EncodedLocation:
    encoder = LineEncoder(net, max_lrp_distance=max_lrp_distance, disambiguate=disambiguate)
    return encoder.encode(path.lines, path.p_off, path.n_off)


def osm_pieces(path: PathResult) -> list[Piece]:
    return [Piece(piece, int(line.frc), line.level < 0, line.label) for line, piece in path.line_pieces()]


def mapbox_pieces(net: RoadNetwork, decoding: MapboxDecoding) -> list[Piece]:
    return [Piece(net.proj.coords_to_xy(s.coords), s.frc, s.tunnel,
                  f"{s.name or '(unnamed)'} [{s.road_class or '?'}{', tunnel' if s.tunnel else ''}]")
            for s in decoding.segments if len(s.coords) >= 2]


@dataclass
class RoundTrip:
    binary: str
    original: PathResult | None  # the encoded route (None: a pasted OpenLR code)
    local: DecodedLocation  # decoded on OSM again - the reference if there is no original
    mapbox: MapboxDecoding
    transferred: PathResult | None = None  # Mapbox result matched onto OSM
    accuracy: Metrics | None = None
    similarity: dict = field(default_factory=dict)
    similar: bool = False
    seconds: dict[str, float] = field(default_factory=dict)

    @property
    def reference(self) -> PathResult | None:
        return self.original or self.local.result

    @property
    def correct(self) -> bool:
        return bool(self.accuracy and self.accuracy.correct)


def round_trip(net: RoadNetwork, binary: str, decoder: MapboxDecoder,
               original: PathResult | None = None) -> RoundTrip:
    t = time.perf_counter()
    local = decode_binary(binary, net)
    t_local = time.perf_counter()
    mb = decoder.decode(binary)
    t_mb = time.perf_counter()
    run = RoundTrip(binary, original, local, mb,
                    seconds={"osm_decode": t_local - t, "mapbox_decode": t_mb - t_local})
    reference = run.reference
    if mb.segments and reference is not None:
        run.transferred = transfer_to_osm(mb.segments, net)
        run.accuracy = compare(reference, run.transferred)
        run.similarity = compare_routes(osm_pieces(reference), mapbox_pieces(net, mb))
        run.similar = similar(run.similarity)
    return run


# ---------------------------------------------------------------------- scenarios (``openlr-demo crossmap``)
@dataclass
class ScenarioRow:
    key: str
    title: str
    max_lrp_distance: float
    encoded: EncodedLocation
    trip: RoundTrip


def run_scenarios(net: RoadNetwork, client, keys: list[str], spacings: list[float],
                  disambiguate: bool = True) -> list[ScenarioRow]:
    from .scenarios import SCENARIOS

    decoder = MapboxDecoder(client)
    rows = []
    for key in keys:
        sc = SCENARIOS[key]
        truth = sc.route(net)
        for spacing in spacings:
            encoded = encode_path(net, truth, disambiguate, spacing)
            rows.append(ScenarioRow(key, sc.title, spacing, encoded, round_trip(net, encoded.binary, decoder, truth)))
    return rows


def _fmt(value, digits: int = 2, unit: str = "") -> str:
    return "-" if value is None else f"{value:.{digits}f}{unit}"


def _cells(row: ScenarioRow) -> list[str]:
    t, s = row.trip, row.trip.similarity
    acc = t.accuracy
    verdict = ("correct" if t.correct else "WRONG") if acc else f"failed: {t.mapbox.error or 'no route'}"
    b = s.get("buffer", {})
    return [row.key, f"{row.max_lrp_distance / 1000:g} km", str(len(row.encoded.lrps)), row.encoded.binary, verdict,
            _fmt(acc and acc.precision), _fmt(acc and acc.recall), _fmt(acc and acc.level_accuracy),
            _fmt(b.get("10", {}).get("f1")), _fmt(s.get("frechet"), 1, " m"), _fmt(s.get("deviation_p95"), 1, " m"),
            _fmt(s.get("frc_agreement")), _fmt(s.get("tunnel_agreement")), _fmt(s.get("length_ratio"), 3),
            "yes" if t.similar else "no"]


HEADER = ["scenario", "max LRP distance", "LRPs", "OpenLR", "Mapbox decode", "precision", "recall", "level acc.",
          "F1@10 m", "Frechet", "p95 dev.", "FRC agr.", "tunnel agr.", "length ratio", "similar"]


def console(rows: list[ScenarioRow]) -> str:
    table = [HEADER] + [_cells(r) for r in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(HEADER))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in table)


def write_markdown(rows: list[ScenarioRow], path, profile: str):
    lines = ["# Cross-map round trip: OSM -> OpenLR -> Mapbox", "",
             f"Encoded on the OSM extract, decoded on the Mapbox map (`{profile}`) with Tilequery candidates and "
             "Directions legs, then compared with the encoded route. Accuracy: the Mapbox route matched back onto "
             "OSM and compared line by line; similarity: geometry and road attributes.", "",
             "| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    lines += ["| " + " | ".join(c.replace("|", "\\|") for c in _cells(r)) + " |" for r in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
