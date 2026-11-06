"""Comparison of a matched / decoded path with the ground truth on the receiver map."""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Point

from .geo import XY, densify, point_along
from .network import Line


@dataclass
class PathResult:
    """A path on the receiver map: lines plus offsets into the first / from the end of the last line."""

    lines: list[Line]
    p_off: float = 0.0
    n_off: float = 0.0
    gaps: int = 0  # number of places where a matcher could not connect consecutive lines

    @property
    def length(self) -> float:
        return sum(b - a for spans in self.intervals().values() for a, b in spans)

    def intervals(self) -> dict[str, list[tuple[float, float]]]:
        """Covered parts of every line, merged (a path driving a line twice counts once)."""
        raw: dict[str, list[tuple[float, float]]] = {}
        last = len(self.lines) - 1
        for i, ln in enumerate(self.lines):
            a = self.p_off if i == 0 else 0.0
            b = ln.length - self.n_off if i == last else ln.length
            if b > a:
                raw.setdefault(ln.id, []).append((a, b))
        out = {}
        for key, spans in raw.items():
            merged: list[tuple[float, float]] = []
            for a, b in sorted(spans):
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            out[key] = merged
        return out

    def pieces_xy(self) -> list[list[XY]]:
        """Clipped geometry of every line of the path (metric coordinates)."""
        return [piece for _, piece in self.line_pieces()]

    def line_pieces(self) -> list[tuple[Line, list[XY]]]:
        pieces = []
        last = len(self.lines) - 1
        for i, ln in enumerate(self.lines):
            a = self.p_off if i == 0 else 0.0
            b = ln.length - self.n_off if i == last else ln.length
            if b <= a:
                continue
            if a == 0.0 and b == ln.length:
                pieces.append((ln, list(ln.xy)))
                continue
            inner = [p for p, d in zip(ln.xy, _cumulative(ln.xy)) if a < d < b]
            pieces.append((ln, [point_along(ln.xy, a), *inner, point_along(ln.xy, b)]))
        return pieces


def _cumulative(xy: list[XY]) -> list[float]:
    out, acc = [0.0], 0.0
    for p, q in zip(xy, xy[1:]):
        acc += ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2) ** 0.5
        out.append(acc)
    return out


def _overlap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(x2, y2) - max(x1, y1)) for x1, x2 in a for y1, y2 in b)


@dataclass
class Metrics:
    precision: float  # share of the matched length that lies on the true route
    recall: float  # share of the true route that was matched
    level_accuracy: float  # share of the true route matched on the correct vertical level
    correct: bool

    @property
    def f1(self) -> float:
        s = self.precision + self.recall
        return 2 * self.precision * self.recall / s if s else 0.0


FAILED = Metrics(0.0, 0.0, 0.0, False)


def compare(truth: PathResult, result: PathResult | None, threshold: float = 0.9) -> Metrics:
    if result is None or not result.lines:
        return FAILED
    ti, ri = truth.intervals(), result.intervals()
    common = sum(_overlap(ti[k], ri[k]) for k in ti.keys() & ri.keys())
    precision = common / result.length if result.length else 0.0
    recall = common / truth.length if truth.length else 0.0
    profile = level_profile(truth, result)
    level_acc = sum(1 for _, t, r in profile if r == t) / len(profile)
    return Metrics(precision, recall, level_acc, precision >= threshold and recall >= threshold)


def level_profile(truth: PathResult, result: PathResult | None, step: float = 10.0, radius: float = 30.0):
    """[(distance along truth, truth level, matched level or None)] sampled every `step` m.

    The matched level is the level of the closest line of `result` near each sample point."""
    samples = []
    offset = 0.0
    pieces = truth.pieces_xy()
    clipped_lines = [ln for ln in truth.lines if ln.id in truth.intervals()]
    cand = []
    if result is not None:
        cand = [(ln, ln.geom_xy) for ln in result.lines]
    for line, piece in zip(clipped_lines, pieces):
        pts = densify(piece, step)
        piece_len = _cumulative(piece)[-1]
        for i, p in enumerate(pts[:-1]):
            matched = None
            best = radius
            for ln, g in cand:
                d = g.distance(Point(p))
                if d < best:
                    best, matched = d, ln.level
            samples.append((offset + piece_len * i / (len(pts) - 1), line.level, matched))
        offset += piece_len
    return samples
