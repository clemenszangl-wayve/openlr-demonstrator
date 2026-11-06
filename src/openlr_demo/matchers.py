"""Geometry-only map matchers used as baselines against OpenLR.

Both matchers receive nothing but the route polyline (the "simple linestring" approach):

* :func:`match_nearest` - the classic naive approach: resample the polyline, snap every
  sample to the closest road with a compatible heading, glue the snapped roads together.
* :func:`match_hmm` - a topology-aware hidden Markov model matcher after Newson & Krumm
  (2009), the method behind OSRM / Valhalla / GraphHopper map matching. It is much more
  robust than nearest-road snapping, but still only knows about geometry.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from shapely.geometry import Point

from .evaluation import PathResult
from .geo import LonLat, angle_diff, densify, dist, local_heading
from .network import Line, RoadNetwork


def _snap_candidates(net: RoadNetwork, p, heading: float, radius: float, max_heading_diff: float):
    out = []
    for line, d in net.lines_near(p, radius):
        off = line.geom_xy.project(Point(p))
        if angle_diff(line.heading_at(off), heading) <= max_heading_diff:
            out.append((line, off, d))
    return out


def _stitch(net: RoadNetwork, lines: list[Line], connect_max: float) -> tuple[list[Line], int]:
    path, gaps = [lines[0]], 0
    for ln in lines[1:]:
        prev = path[-1]
        if prev is ln:
            continue
        if prev.end == ln.start:
            path.append(ln)
            continue
        r = net.route(prev, ln, cutoff=connect_max)
        if r is None:
            gaps += 1
            path.append(ln)
        else:
            path.extend(r[1:])
    return path, gaps


def match_nearest(polyline: list[LonLat], net: RoadNetwork, step: float = 10.0, radius: float = 30.0,
                  max_heading_diff: float = 45.0, tie_m: float = 2.0, connect_max: float = 300.0) -> PathResult | None:
    """Snap every sample to the nearest road; on near-ties (within `tie_m`) prefer staying on /
    continuing from the previously matched road. Single-sample blips are discarded."""
    pts = densify(net.proj.coords_to_xy(polyline), step)
    snapped = []
    prev: Line | None = None
    for i, p in enumerate(pts):
        cands = _snap_candidates(net, p, local_heading(pts, i), radius, max_heading_diff)
        if not cands:
            continue
        best = cands[0]
        if prev is not None:
            for c in cands:
                if c[2] > best[2] + tie_m:
                    break
                if c[0] is prev or c[0].start == prev.end:
                    best = c
                    break
        snapped.append(best)
        prev = best[0]
    if not snapped:
        return None
    runs: list[list] = []
    for line, _, _ in snapped:
        if runs and runs[-1][0] is line:
            runs[-1][1] += 1
        else:
            runs.append([line, 1])
    if len(runs) > 2:
        runs = [runs[0], *[r for r in runs[1:-1] if r[1] > 1], runs[-1]]
    seq = [r[0] for r in runs]
    path, gaps = _stitch(net, seq, connect_max)
    first, last = snapped[0], snapped[-1]
    return PathResult(path, first[1], max(0.0, last[0].length - last[1]), gaps)


@dataclass(slots=True)
class _Cand:
    line: Line
    off: float
    d: float


def match_hmm(polyline: list[LonLat], net: RoadNetwork, step: float = 25.0, radius: float = 40.0,
              sigma: float = 5.0, beta: float = 10.0, max_heading_diff: float = 60.0,
              max_candidates: int = 8) -> PathResult | None:
    pts = densify(net.proj.coords_to_xy(polyline), step)
    return hmm_path(pts, net, radius, sigma, beta, max_heading_diff, max_candidates, step)


def hmm_path(pts, net: RoadNetwork, radius: float = 40.0, sigma: float = 5.0, beta: float = 10.0,
             max_heading_diff: float = 60.0, max_candidates: int = 8, step: float = 25.0,
             penalty: Callable[[int, Line], float] | None = None) -> PathResult | None:
    """Viterbi over road candidates of the sample points `pts` (metric coordinates).

    `penalty(i, line)` adds metres to the distance of sample i to `line` in the emission."""
    layers: list[list[_Cand]] = []
    for i, p in enumerate(pts):
        cands = [_Cand(*c) for c in _snap_candidates(net, p, local_heading(pts, i), radius, max_heading_diff)]
        if penalty:
            for c in cands:
                c.d += penalty(i, c.line)
            cands.sort(key=lambda c: c.d)
        layers.append(cands[:max_candidates])
    keep = [i for i, layer in enumerate(layers) if layer]
    layers, pts = [layers[i] for i in keep], [pts[i] for i in keep]
    if not layers:
        return None

    def emission(c: _Cand) -> float:
        return -0.5 * (c.d / sigma) ** 2

    score = [[emission(c) for c in layers[0]]]
    back: list[list[int | None]] = [[None] * len(layers[0])]
    breaks = 0
    for t in range(1, len(layers)):
        prev, cur = layers[t - 1], layers[t]
        gc = dist(pts[t - 1], pts[t])
        cutoff = 3 * gc + 100.0
        row = [-math.inf] * len(cur)
        brow: list[int | None] = [None] * len(cur)
        for ia, a in enumerate(prev):
            if score[t - 1][ia] == -math.inf:
                continue
            reach, _ = net.dijkstra(a.line.end, cutoff)
            for ib, b in enumerate(cur):
                if b.line is a.line and b.off >= a.off:
                    route_d = b.off - a.off
                elif b.line.start in reach:
                    route_d = a.line.length - a.off + reach[b.line.start] + b.off
                else:
                    continue
                s = score[t - 1][ia] - abs(route_d - gc) / beta + emission(b)
                if s > row[ib]:
                    row[ib], brow[ib] = s, ia
        if all(s == -math.inf for s in row):  # HMM break: restart the chain here
            breaks += 1
            row = [emission(c) for c in cur]
        score.append(row)
        back.append(brow)

    # Viterbi backtracking
    best = max(range(len(layers[-1])), key=lambda i: score[-1][i])
    chosen = [best]
    for t in range(len(layers) - 1, 0, -1):
        prev_idx = back[t][chosen[-1]]
        if prev_idx is None:  # chain was restarted - take the best state of the previous layer
            prev_idx = max(range(len(layers[t - 1])), key=lambda i: score[t - 1][i])
        chosen.append(prev_idx)
    chosen.reverse()
    states = [layers[t][i] for t, i in enumerate(chosen)]

    lines: list[Line] = [states[0].line]
    gaps = 0
    for a, b in zip(states, states[1:]):
        if b.line is a.line and b.off >= a.off:
            continue
        middle = net.node_path(a.line.end, b.line.start, cutoff=3 * step + 100.0)
        if middle is None:
            gaps += 1
            middle = []
        for ln in [*middle, b.line]:
            if lines[-1] is not ln:
                lines.append(ln)
    return PathResult(lines, states[0].off, max(0.0, states[-1].line.length - states[-1].off), gaps)
