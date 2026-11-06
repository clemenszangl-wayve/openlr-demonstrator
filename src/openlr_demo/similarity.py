"""How similar are two routes on different maps? Geometry plus road attributes.

Geometry alone cannot separate stacked roads (Shuto 3 and Route 246 are ~6 m apart in plan
view), so besides buffer overlap, Hausdorff and Frechet distance the comparison also reports
how much of the route agrees in road class (FRC) and tunnel / not-tunnel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely import STRtree
from shapely.geometry import LineString, Point

from .geo import XY, densify, dist, polyline_length

TOLERANCES_M = (5.0, 10.0, 20.0)
ATTRIBUTE_RADIUS_M = 25.0


@dataclass
class Piece:
    """A stretch of a route with its road attributes (metric coordinates)."""

    xy: list[XY]
    frc: int | None
    tunnel: bool
    label: str = ""


def _samples(pieces: list[Piece], step: float) -> list[tuple[XY, Piece]]:
    out: list[tuple[XY, Piece]] = []
    for piece in pieces:
        if len(piece.xy) < 2 or polyline_length(piece.xy) == 0:
            continue
        pts = densify(piece.xy, step)
        out.extend((p, piece) for p in (pts[1:] if out else pts))
    return out


def _chain(pieces: list[Piece]) -> list[XY]:
    out: list[XY] = []
    for piece in pieces:
        for p in piece.xy:
            if not out or dist(out[-1], p) > 0.01:
                out.append(p)
    return out


def frechet(a: list[XY], b: list[XY]) -> float:
    """Discrete Frechet distance (Eiter & Mannila) - respects the order of travel."""
    n, m = len(a), len(b)
    prev = [0.0] * m
    for i in range(n):
        cur = [0.0] * m
        for j in range(m):
            d = dist(a[i], b[j])
            if i == 0 and j == 0:
                cur[j] = d
            elif i == 0:
                cur[j] = max(cur[j - 1], d)
            elif j == 0:
                cur[j] = max(prev[0], d)
            else:
                cur[j] = max(min(prev[j], prev[j - 1], cur[j - 1]), d)
        prev = cur
    return prev[-1]


def _resample(xy: list[XY], max_points: int = 400) -> list[XY]:
    length = polyline_length(xy)
    return densify(xy, max(2.0, length / max_points)) if length else xy


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def compare_routes(original: list[Piece], decoded: list[Piece], step: float = 2.0) -> dict:
    """Similarity of `decoded` to `original`; both in the same metric projection."""
    a_line, b_line = _chain(original), _chain(decoded)
    if len(a_line) < 2 or len(b_line) < 2:
        return {"error": "a route is empty"}
    a_geom, b_geom = LineString(a_line), LineString(b_line)
    a_samples, b_samples = _samples(original, step), _samples(decoded, step)
    b_to_a = [a_geom.distance(Point(p)) for p, _ in b_samples]
    a_to_b = [b_geom.distance(Point(p)) for p, _ in a_samples]

    buffer = {}
    for tol in TOLERANCES_M:
        precision = sum(d <= tol for d in b_to_a) / len(b_to_a)
        recall = sum(d <= tol for d in a_to_b) / len(a_to_b)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        buffer[f"{tol:g}"] = {"precision": precision, "recall": recall, "f1": f1}

    # attributes: for every sample of the original, the decoded piece closest to it
    tree = STRtree([LineString(p.xy) for p in decoded if len(p.xy) >= 2])
    usable = [p for p in decoded if len(p.xy) >= 2]
    frc_same = frc_known = tunnel_same = covered = 0
    for p, piece in a_samples:
        idx = tree.query_nearest(Point(p), max_distance=ATTRIBUTE_RADIUS_M)
        if len(idx) == 0:
            continue
        covered += 1
        other = usable[int(idx[0])]
        if piece.frc is not None and other.frc is not None:
            frc_known += 1
            frc_same += piece.frc == other.frc
        tunnel_same += piece.tunnel == other.tunnel
    n = len(a_samples)
    length_a, length_b = polyline_length(a_line), polyline_length(b_line)
    return {
        "length_original": length_a,
        "length_decoded": length_b,
        "length_ratio": length_b / length_a if length_a else math.nan,
        "buffer": buffer,
        "deviation_mean": sum(b_to_a) / len(b_to_a),
        "deviation_median": _percentile(b_to_a, 0.5),
        "deviation_p95": _percentile(b_to_a, 0.95),
        "hausdorff": max(max(b_to_a), max(a_to_b)),
        "frechet": frechet(_resample(a_line), _resample(b_line)),
        "start_offset": dist(a_line[0], b_line[0]),
        "end_offset": dist(a_line[-1], b_line[-1]),
        "frc_agreement": frc_same / frc_known if frc_known else math.nan,
        "tunnel_agreement": tunnel_same / covered if covered else math.nan,
        "attribute_coverage": covered / n if n else 0.0,
    }


def similar(metrics: dict, f1_min: float = 0.9, frechet_max: float = 30.0, frc_min: float = 0.75) -> bool:
    """Same route: geometry overlaps within 10 m, no large detour, and the road classes agree.

    ``frc_min`` leaves room for maps classifying ramps differently (OSM ``trunk_link`` vs.
    Mapbox ``motorway_link``): a correct decode of the ramp scenario agrees on ~80 %.
    """
    if "error" in metrics:
        return False
    return (metrics["buffer"]["10"]["f1"] >= f1_min and metrics["frechet"] <= frechet_max
            and not metrics["frc_agreement"] < frc_min)
