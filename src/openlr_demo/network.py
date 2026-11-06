"""Directed road network built from OSM ways, as required by OpenLR (whitepaper ch. B 4.1).

* Nodes are OSM nodes where ways meet (junctions) or end.
* Lines are directed: a two-way OSM segment yields two lines, a one-way segment one line.
* Every line carries length, geometry, FRC and FOW. FRC/FOW are derived from OSM tags,
  which the whitepaper explicitly allows ("may be derived from other information").
* Nodes where ways merely continue (e.g. a bridge starts) remain in the graph; OpenLR calls
  them *invalid nodes* (Rule 4) and the encoder must avoid placing LRPs on them.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from openlr import FOW, FRC
from shapely import STRtree
from shapely.geometry import LineString, Point

from .geo import XY, LocalProjection, LonLat, angle_diff, bearing, point_along, polyline_length

FRC_BY_HIGHWAY = {
    "motorway": FRC.FRC0,
    "trunk": FRC.FRC1,
    "primary": FRC.FRC2,
    "secondary": FRC.FRC3,
    "tertiary": FRC.FRC4,
    "unclassified": FRC.FRC5,
    "residential": FRC.FRC6,
    "living_street": FRC.FRC6,
}
_DUAL_CARRIAGEWAY_CLASSES = {"trunk", "primary", "secondary"}


def frc_for(tags: dict[str, str]) -> FRC:
    highway = tags.get("highway", "")
    return FRC_BY_HIGHWAY.get(highway.removesuffix("_link"), FRC.FRC7)


def fow_for(tags: dict[str, str]) -> FOW:
    highway = tags.get("highway", "")
    if tags.get("junction") in ("roundabout", "circular"):
        return FOW.ROUNDABOUT
    if highway == "motorway":
        return FOW.MOTORWAY
    if highway.endswith("_link"):
        return FOW.SLIPROAD
    # In OSM, a one-way major road is (almost always) one carriageway of a dual carriageway.
    if highway in _DUAL_CARRIAGEWAY_CLASSES and tags.get("oneway") in ("yes", "1", "true", "-1"):
        return FOW.MULTIPLE_CARRIAGEWAY
    return FOW.SINGLE_CARRIAGEWAY


def level_for(tags: dict[str, str]) -> int:
    """Vertical level: OSM `layer`, defaulting to +1 for bridges and -1 for tunnels."""
    layer = tags.get("layer")
    if layer:
        try:
            return int(float(layer.split(";")[0]))
        except ValueError:
            pass
    if tags.get("bridge", "no") != "no":
        return 1
    if tags.get("tunnel", "no") != "no":
        return -1
    return 0


def _directions(tags: dict[str, str]) -> tuple[bool, bool]:
    oneway = tags.get("oneway", "")
    highway = tags.get("highway", "")
    if oneway in ("yes", "1", "true"):
        return True, False
    if oneway in ("-1", "reverse"):
        return False, True
    if oneway == "no":
        return True, True
    implied = highway in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular")
    return True, not implied


@dataclass(slots=True, eq=False)
class Line:
    id: str
    start: int
    end: int
    coords: list[LonLat]
    xy: list[XY]
    length: float
    frc: FRC
    fow: FOW
    way_id: int
    tags: dict[str, str]
    level: int
    geom_xy: LineString = field(init=False, repr=False)

    def __post_init__(self):
        self.geom_xy = LineString(self.xy)

    @property
    def name(self) -> str:
        t = self.tags
        name = t.get("name", "")
        en = t.get("name:en", "")
        if name and en and en != name:
            name = f"{name} ({en})"
        name = name or en or (f"ref {t['ref']}" if "ref" in t else "(unnamed)")
        return name

    @property
    def label(self) -> str:
        return f"{self.name} [{self.tags.get('highway')}, level {self.level:+d}]"

    def heading_at(self, offset: float, span: float = 10.0) -> float:
        a = point_along(self.xy, max(0.0, offset - span / 2))
        b = point_along(self.xy, min(self.length, offset + span / 2))
        return bearing(a, b)


class RoadNetwork:
    def __init__(self, nodes: dict[int, LonLat], lines: Iterable[Line], proj: LocalProjection, meta=None):
        self.nodes = nodes
        self.proj = proj
        self.node_xy = {n: proj.to_xy(*c) for n, c in nodes.items()}
        self.lines: dict[str, Line] = {}
        self.out_lines: dict[int, list[Line]] = defaultdict(list)
        self.in_lines: dict[int, list[Line]] = defaultdict(list)
        for line in lines:
            self.lines[line.id] = line
            self.out_lines[line.start].append(line)
            self.in_lines[line.end].append(line)
        self.meta = meta or {}
        self._line_list = list(self.lines.values())
        self._tree = STRtree([ln.geom_xy for ln in self._line_list])

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_osm(cls, data: dict) -> RoadNetwork:
        coords = {int(k): (v[0], v[1]) for k, v in data["nodes"].items()}
        lon_min, lat_min, lon_max, lat_max = data["meta"]["bbox"]
        proj = LocalProjection((lon_min + lon_max) / 2, (lat_min + lat_max) / 2)

        usage = defaultdict(int)
        for way in data["ways"]:
            for n in way["nodes"]:
                usage[n] += 1
        lines: list[Line] = []
        for way in data["ways"]:
            tags = way["tags"]
            frc, fow, level = frc_for(tags), fow_for(tags), level_for(tags)
            forward, backward = _directions(tags)
            seg, chunk = 0, [way["nodes"][0]]
            for n in way["nodes"][1:]:
                chunk.append(n)
                if usage[n] > 1 or n == way["nodes"][-1]:
                    ll = [coords[c] for c in chunk]
                    xy = proj.coords_to_xy(ll)
                    length = polyline_length(xy)
                    if length > 0.01:
                        base = f"{way['id']}.{way['part']}.{seg}"
                        if forward:
                            lines.append(Line(base + "+", chunk[0], chunk[-1], ll, xy, length,
                                              frc, fow, way["id"], tags, level))
                        if backward:
                            lines.append(Line(base + "-", chunk[-1], chunk[0], ll[::-1], xy[::-1],
                                              length, frc, fow, way["id"], tags, level))
                    seg, chunk = seg + 1, [n]
        used = {ln.start for ln in lines} | {ln.end for ln in lines}
        return cls({n: coords[n] for n in used}, lines, proj, data.get("meta"))

    def transformed(self, displace: Callable[[float, float], XY], simplify_m: float = 0.0) -> RoadNetwork:
        """Copy of this network with displaced (and optionally generalised) geometry.

        Topology, IDs and attributes are kept; only coordinates and lengths change.
        """
        node_xy = {n: displace(*xy) for n, xy in self.node_xy.items()}
        nodes = {n: self.proj.to_lonlat(*xy) for n, xy in node_xy.items()}
        lines = []
        for ln in self._line_list:
            xy = [node_xy[ln.start]] + [displace(*p) for p in ln.xy[1:-1]] + [node_xy[ln.end]]
            if simplify_m > 0 and len(xy) > 2:
                xy = list(LineString(xy).simplify(simplify_m, preserve_topology=False).coords)
            lines.append(Line(ln.id, ln.start, ln.end, self.proj.coords_to_lonlat(xy), xy,
                              polyline_length(xy), ln.frc, ln.fow, ln.way_id, ln.tags, ln.level))
        return RoadNetwork(nodes, lines, self.proj, self.meta)

    # ------------------------------------------------------------------ queries
    def lines_near(self, xy: XY, radius: float) -> list[tuple[Line, float]]:
        pt = Point(xy)
        idx = self._tree.query(pt, predicate="dwithin", distance=radius)
        out = [(self._line_list[i], self._line_list[i].geom_xy.distance(pt)) for i in idx]
        return sorted(out, key=lambda t: t[1])

    def nearest_line(self, xy: XY, heading: float | None = None, radius: float = 50.0,
                     accept: Callable[[Line], bool] | None = None, max_heading_diff: float = 45.0) -> Line | None:
        for line, _d in self.lines_near(xy, radius):
            if accept and not accept(line):
                continue
            if heading is not None:
                off = line.geom_xy.project(Point(xy))
                if angle_diff(line.heading_at(off), heading) > max_heading_diff:
                    continue
            return line
        return None

    def is_valid_node(self, node: int) -> bool:
        """OpenLR Rule 4: junction nodes are valid; pure pass-through nodes are not.

        Invalid: exactly one incoming and one outgoing line (not a dead end), or two incoming
        and two outgoing lines that only connect two adjacent nodes.
        """
        ins, outs = self.in_lines.get(node, []), self.out_lines.get(node, [])
        neighbours = {ln.start for ln in ins} | {ln.end for ln in outs}
        if len(ins) == 1 and len(outs) == 1:
            return len(neighbours) == 1  # dead end of a two-way road: only a U-turn is possible
        if len(ins) == 2 and len(outs) == 2:
            return len(neighbours) != 2
        return True

    # ------------------------------------------------------------------ routing
    def dijkstra(self, source: int, cutoff: float = math.inf, target: int | None = None,
                 accept: Callable[[Line], bool] | None = None):
        """Node based Dijkstra. Returns (distance, predecessor line) dictionaries."""
        dist = {source: 0.0}
        pred: dict[int, Line] = {}
        done = set()
        counter = itertools.count()
        heap = [(0.0, next(counter), source)]
        while heap:
            d, _, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if node == target:
                break
            for line in self.out_lines.get(node, ()):
                if accept and not accept(line):
                    continue
                nd = d + line.length
                if nd <= cutoff and nd < dist.get(line.end, math.inf):
                    dist[line.end] = nd
                    pred[line.end] = line
                    heapq.heappush(heap, (nd, next(counter), line.end))
        return dist, pred

    @staticmethod
    def unwind(pred: dict[int, Line], source: int, target: int) -> list[Line]:
        path = []
        node = target
        while node != source:
            line = pred[node]
            path.append(line)
            node = line.start
        return path[::-1]

    def node_path(self, source: int, target: int, cutoff: float = math.inf,
                  accept: Callable[[Line], bool] | None = None) -> list[Line] | None:
        if source == target:
            return []
        dist, pred = self.dijkstra(source, cutoff, target, accept)
        if target not in dist:
            return None
        return self.unwind(pred, source, target)

    def route(self, start: Line, end: Line, cutoff: float = math.inf,
              accept: Callable[[Line], bool] | None = None) -> list[Line] | None:
        """Shortest path that starts with line `start` and ends with line `end` (both included)."""
        if start is end:
            return [start]
        middle = self.node_path(start.end, end.start, cutoff, accept)
        if middle is None:
            return None
        return [start, *middle, end]
