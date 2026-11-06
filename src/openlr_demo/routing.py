"""Ground-truth route from user points: lat/lon plus the FRC of the road that is meant.

The FRC is what makes a point unambiguous on stacked roads: (35.6567, 139.6990) with FRC0 is
the Shuto 3 viaduct, with FRC1 Route 246 below it. Each point snaps to a nearby line of its
FRC heading towards the next point; between two points the route uses roads whose FRC lies
between the two points' FRCs (relaxed if no such path exists). The combination of snapped
lines with the shortest total length plus snapping distance wins (Viterbi).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .evaluation import PathResult
from .geo import bearing
from .matchers import _snap_candidates
from .network import Line, RoadNetwork

SNAP_WEIGHT = 3.0  # metres of route length per metre of snapping distance


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class RoutePoint:
    lat: float
    lon: float
    frc: int | None = None  # None: any road


@dataclass
class SnappedPoint:
    point: RoutePoint
    line: Line
    offset: float  # along the line
    distance: float  # from the point to the line


@dataclass
class PointRoute:
    path: PathResult
    snapped: list[SnappedPoint]
    notes: list[str]


RELAXATION = ("roads with an FRC between the two points' FRCs", "also less important roads", "any road")


def _frc_filter(lo: int | None, hi: int | None) -> Callable[[Line], bool] | None:
    if lo is None and hi is None:
        return None
    return lambda line: (lo is None or line.frc >= lo) and (hi is None or line.frc <= hi)


def _leg_range(a: SnappedPoint, b: SnappedPoint, level: int) -> tuple[int | None, int | None]:
    """Allowed FRC range between two points; `level` 0 is the strictest (see RELAXATION)."""
    lo, hi = sorted((int(a.line.frc), int(b.line.frc)))
    return [(lo, hi), (lo, None), (None, None)][level]


def _candidates(net: RoadNetwork, xy, point: RoutePoint, heading: float, radius: float, k: int):
    seen, out = set(), []
    for line, off, d in _snap_candidates(net, xy, heading, radius, 90.0):
        if (point.frc is None or int(line.frc) == point.frc) and line.id not in seen:
            seen.add(line.id)
            out.append(SnappedPoint(point, line, off, d))
    return out[:k]


def _leg_length(net: RoadNetwork, a: SnappedPoint, b: SnappedPoint, frc_range, cutoff: float, cache: dict):
    if b.line is a.line and b.offset >= a.offset:
        return b.offset - a.offset
    key = (a.line.end, frc_range)
    if key not in cache:
        cache[key] = net.dijkstra(a.line.end, cutoff, accept=_frc_filter(*frc_range))[0]
    reach = cache[key]
    if b.line.start not in reach:
        return math.inf
    return a.line.length - a.offset + reach[b.line.start] + b.offset


def route_through_points(net: RoadNetwork, points: list[RoutePoint], radius: float = 50.0,
                         max_candidates: int = 6) -> PointRoute:
    if len(points) < 2:
        raise RoutingError("a route needs at least two points")
    xy = [net.proj.to_xy(p.lon, p.lat) for p in points]
    n = len(points)
    layers: list[list[SnappedPoint]] = []
    for i, p in enumerate(points):
        a, b = xy[max(i - 1, 0)], xy[min(i + 1, n - 1)]
        cands = _candidates(net, xy[i], p, bearing(a, b), radius, max_candidates)
        if not cands:
            found = sorted({int(ln.frc) for ln, _ in net.lines_near(xy[i], radius)})
            what = f"FRC{p.frc} road" if p.frc is not None else "road"
            hint = f" (roads there: {', '.join(f'FRC{f}' for f in found)})" if found else ""
            raise RoutingError(f"point {i + 1}: no {what} within {radius:.0f} m in the direction of travel{hint}")
        layers.append(cands)

    notes: list[str] = []
    cost = [[SNAP_WEIGHT * c.distance for c in layers[0]]]
    back: list[list[tuple[int, int] | None]] = [[None] * len(layers[0])]
    for i in range(1, n):
        straight = math.dist(xy[i - 1], xy[i])
        cutoff = 4 * straight + 2000.0
        for level in range(3):
            cache: dict = {}
            row, brow = [math.inf] * len(layers[i]), [None] * len(layers[i])
            for ja, a in enumerate(layers[i - 1]):
                if cost[i - 1][ja] == math.inf:
                    continue
                for jb, b in enumerate(layers[i]):
                    leg = _leg_length(net, a, b, _leg_range(a, b, level), cutoff, cache)
                    total = cost[i - 1][ja] + leg + SNAP_WEIGHT * b.distance
                    if total < row[jb]:
                        row[jb], brow[jb] = total, (ja, level)
            if any(c < math.inf for c in row):
                if level:
                    notes.append(f"points {i}-{i + 1}: no path over {RELAXATION[0]}, used {RELAXATION[level]}")
                break
        else:
            raise RoutingError(f"points {i} and {i + 1} are not connected (one-way roads?)")
        cost.append(row)
        back.append(brow)

    j = min(range(len(layers[-1])), key=lambda k: cost[-1][k])
    chosen, levels = [j], []
    for i in range(n - 1, 0, -1):
        j, level = back[i][chosen[-1]]
        chosen.append(j)
        levels.append(level)
    chosen.reverse()
    levels.reverse()
    snapped = [layers[i][j] for i, j in enumerate(chosen)]

    lines = [snapped[0].line]
    for (a, b), level in zip(zip(snapped, snapped[1:]), levels):
        if b.line is a.line and b.offset >= a.offset:
            continue
        middle = net.node_path(a.line.end, b.line.start, accept=_frc_filter(*_leg_range(a, b, level))) or []
        for ln in [*middle, b.line]:
            lines.append(ln)
    p_off = snapped[0].offset
    n_off = snapped[-1].line.length - snapped[-1].offset
    path = PathResult(lines, p_off, n_off)
    if path.length < 20:
        raise RoutingError("route is shorter than 20 m")
    return PointRoute(path, snapped, notes)
