"""OpenLR decoding on the OSM network with TomTom's ``openlr-dereferencer``.

The dereferencer implements the whitepaper decoder (chapter G, section 12.1): candidate
lines per LRP, rating by distance / FRC / FOW / bearing, shortest paths between candidates
restricted by LFRCNP, DNP validation and offset trimming. We only provide a MapReader
adapter for :class:`~openlr_demo.network.RoadNetwork`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import chain

from openlr import FRC, Coordinates, LineLocationReference, binary_decode
from openlr_dereferencer import Config, decode
from openlr_dereferencer.decoding.error import LRDecodeError
from openlr_dereferencer.maps import Line as AbstractLine
from openlr_dereferencer.maps import MapReader
from openlr_dereferencer.maps import Node as AbstractNode
from openlr_dereferencer.observer import DecoderObserver
from shapely.geometry import LineString, Point

from .evaluation import PathResult
from .network import Line, RoadNetwork

DECODER_CONFIG = Config(
    search_radius=50.0,
    max_dnp_deviation=0.1,
    tolerated_dnp_dev=30,
    min_score=0.3,
    # The whitepaper suggests to relax LFRCNP if the decoder map may classify roads
    # differently from the encoder map - we accept one class lower than requested.
    tolerated_lfrc={frc: FRC(min(frc + 1, 7)) for frc in FRC},
    candidate_threshold=20,
    max_bear_deviation=45.0,
    fow_weight=1 / 4,
    frc_weight=1 / 4,
    geo_weight=1 / 4,
    bear_weight=1 / 4,
    bear_dist=20,
)


class _Node(AbstractNode):
    def __init__(self, reader: NetworkMapReader, node_id: int):
        self._reader = reader
        self._id = node_id

    @property
    def node_id(self):
        return self._id

    @property
    def coordinates(self) -> Coordinates:
        return Coordinates(*self._reader.net.nodes[self._id])

    def outgoing_lines(self) -> Iterable[_Line]:
        return [self._reader.get_line(ln.id) for ln in self._reader.net.out_lines.get(self._id, ())]

    def incoming_lines(self) -> Iterable[_Line]:
        return [self._reader.get_line(ln.id) for ln in self._reader.net.in_lines.get(self._id, ())]

    def connected_lines(self) -> Iterable[_Line]:
        return chain(self.incoming_lines(), self.outgoing_lines())


class _Line(AbstractLine):
    def __init__(self, reader: NetworkMapReader, line: Line):
        self._reader = reader
        self.line = line
        self._geometry = LineString(line.coords)

    @property
    def line_id(self):
        return self.line.id

    @property
    def start_node(self) -> _Node:
        return self._reader.get_node(self.line.start)

    @property
    def end_node(self) -> _Node:
        return self._reader.get_node(self.line.end)

    @property
    def frc(self):
        return self.line.frc

    @property
    def fow(self):
        return self.line.fow

    @property
    def geometry(self) -> LineString:
        return self._geometry

    @property
    def length(self) -> float:
        return self.line.length

    def distance_to(self, coord: Coordinates) -> float:
        return self.line.geom_xy.distance(Point(self._reader.net.proj.to_xy(coord.lon, coord.lat)))


class NetworkMapReader(MapReader):
    def __init__(self, network: RoadNetwork):
        self.net = network
        self._lines: dict[str, _Line] = {}
        self._nodes: dict[int, _Node] = {}

    def get_line(self, line_id) -> _Line:
        if line_id not in self._lines:
            self._lines[line_id] = _Line(self, self.net.lines[line_id])
        return self._lines[line_id]

    def get_lines(self):
        return (self.get_line(i) for i in self.net.lines)

    def get_linecount(self) -> int:
        return len(self.net.lines)

    def get_node(self, node_id) -> _Node:
        if node_id not in self._nodes:
            self._nodes[node_id] = _Node(self, node_id)
        return self._nodes[node_id]

    def get_nodes(self):
        return (self.get_node(n) for n in self.net.nodes)

    def get_nodecount(self) -> int:
        return len(self.net.nodes)

    def find_nodes_close_to(self, coord: Coordinates, dist: float):
        xy = self.net.proj.to_xy(coord.lon, coord.lat)
        seen = set()
        for line, _ in self.net.lines_near(xy, dist):
            for n in (line.start, line.end):
                nx, ny = self.net.node_xy[n]
                if n not in seen and (nx - xy[0]) ** 2 + (ny - xy[1]) ** 2 <= dist * dist:
                    seen.add(n)
                    yield self.get_node(n)

    def find_lines_close_to(self, coord: Coordinates, dist: float):
        xy = self.net.proj.to_xy(coord.lon, coord.lat)
        return [self.get_line(line.id) for line, _ in self.net.lines_near(xy, dist)]


@dataclass
class Candidate:
    line: Line
    relative_offset: float
    score: float


@dataclass
class _Observer(DecoderObserver):
    points: list
    candidates: dict[int, list[Candidate]] = field(default_factory=dict)
    routes_tried: int = 0

    def _index(self, lrp) -> int:
        return next(i for i, p in enumerate(self.points) if p is lrp)

    def on_candidates_found(self, lrp, candidates):
        self.candidates[self._index(lrp)] = sorted(
            (Candidate(c.line.line, c.relative_offset, c.score) for c in candidates),
            key=lambda c: -c.score,
        )

    def on_route_success(self, from_lrp, to_lrp, from_line, to_line, path):
        self.routes_tried += 1

    def on_route_fail(self, from_lrp, to_lrp, from_line, to_line):
        self.routes_tried += 1

    def on_matching_fail(self, from_lrp, to_lrp, from_candidates, to_candidates):
        pass


@dataclass
class DecodedLocation:
    reference: LineLocationReference
    result: PathResult | None
    candidates: dict[int, list[Candidate]]
    routes_tried: int
    error: str | None = None


def decode_binary(binary: str, network: RoadNetwork, config: Config = DECODER_CONFIG) -> DecodedLocation:
    """Decode a base64 OpenLR line location on `network` (step 1: physical -> logical)."""
    reference = binary_decode(binary)
    observer = _Observer(reference.points)
    try:
        location = decode(reference, NetworkMapReader(network), observer=observer, config=config)
    except LRDecodeError as exc:
        return DecodedLocation(reference, None, observer.candidates, observer.routes_tried, str(exc))
    result = PathResult([ln.line for ln in location.lines], location.p_off, location.n_off)
    return DecodedLocation(reference, result, observer.candidates, observer.routes_tried)
