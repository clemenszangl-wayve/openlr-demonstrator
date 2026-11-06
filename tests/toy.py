"""Small synthetic networks for encoder / decoder tests (coordinates in metres around Shibuya)."""

from __future__ import annotations

import math

from openlr_demo.network import RoadNetwork

LON0, LAT0 = 139.70, 35.65


def _lonlat(x: float, y: float) -> list[float]:
    return [LON0 + x / (111_320 * math.cos(math.radians(LAT0))), LAT0 + y / 111_320]


def build(nodes: dict[int, tuple[float, float]], ways: list[tuple[int, list[int], dict]]) -> RoadNetwork:
    lonlat = {str(n): _lonlat(*xy) for n, xy in nodes.items()}
    lons = [c[0] for c in lonlat.values()]
    lats = [c[1] for c in lonlat.values()]
    return RoadNetwork.from_osm({
        "meta": {"bbox": [min(lons), min(lats), max(lons), max(lats)]},
        "nodes": lonlat,
        "ways": [{"id": wid, "part": 0, "nodes": ns, "tags": tags} for wid, ns, tags in ways],
    })


W, A, D, C, E = 1, 2, 3, 4, 5
F1, F2, F3 = 6, 7, 8


def detour_network() -> RoadNetwork:
    """W - A, then the location goes A -> D -> C (900 m) although the diagonal A -> C is shorter.

        D ---------- C ---- E
        |          /
        |       /   (diagonal, 640 m)
        |    /
    W - A

    D only connects A and C (2 in / 2 out, 2 neighbours) and is therefore an invalid node.
    F1 -> F2 -> F3 is a separate one-way road; F2 (1 in / 1 out) is invalid as well.
    """
    road = {"highway": "tertiary"}
    oneway = {"highway": "tertiary", "oneway": "yes"}
    return build(
        {W: (-300, 0), A: (0, 0), D: (0, 400), C: (500, 400), E: (800, 400),
         F1: (0, -1000), F2: (300, -1000), F3: (600, -1000)},
        [(10, [W, A], road), (11, [A, D], road), (12, [D, C], road), (13, [C, E], road),
         (14, [A, C], road), (20, [F1, F2], oneway), (21, [F2, F3], oneway)],
    )


R, Y, P, Q, M, Z = 11, 12, 13, 14, 15, 16


def y_split_network() -> RoadNetwork:
    """A one-way trunk road splits at Y into two parallel slip roads that merge again at M.

    Both branches have the same FRC, FOW, length and a bearing only 17 deg apart, so an LRP at
    Y cannot tell the decoder which branch was meant.
    """
    trunk = {"highway": "trunk", "oneway": "yes"}
    link = {"highway": "trunk_link", "oneway": "yes"}
    return build(
        {R: (-300, 0), Y: (0, 0), P: (200, 30), Q: (200, -30), M: (400, 0), Z: (700, 0)},
        [(30, [R, Y], trunk), (31, [Y, P], link), (32, [P, M], link), (33, [Y, Q], link),
         (34, [Q, M], link), (35, [M, Z], trunk)],
    )


def path(net: RoadNetwork, *nodes: int):
    """Lines along the node sequence."""
    lines = []
    for a, b in zip(nodes, nodes[1:]):
        lines.append(next(ln for ln in net.out_lines[a] if ln.end == b))
    return lines
