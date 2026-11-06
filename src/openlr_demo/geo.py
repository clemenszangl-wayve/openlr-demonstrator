"""Small geometry helpers.

All metric computations use a local equirectangular projection around the study area.
For an area of a few kilometres the error is far below one metre, which is plenty for
this demonstrator and keeps the code free of heavy GIS dependencies.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

EARTH_RADIUS_M = 6_371_008.8

XY = tuple[float, float]
LonLat = tuple[float, float]


class LocalProjection:
    """Equirectangular projection centred on (lon0, lat0); x = east, y = north, metres."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0 = lon0
        self.lat0 = lat0
        self._kx = math.radians(1.0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
        self._ky = math.radians(1.0) * EARTH_RADIUS_M

    def to_xy(self, lon: float, lat: float) -> XY:
        return ((lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky)

    def to_lonlat(self, x: float, y: float) -> LonLat:
        return (self.lon0 + x / self._kx, self.lat0 + y / self._ky)

    def coords_to_xy(self, coords: Sequence[LonLat]) -> list[XY]:
        return [self.to_xy(lon, lat) for lon, lat in coords]

    def coords_to_lonlat(self, coords: Sequence[XY]) -> list[LonLat]:
        return [self.to_lonlat(x, y) for x, y in coords]


def dist(a: XY, b: XY) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def bearing(a: XY, b: XY) -> float:
    """Bearing from a to b in degrees clockwise from north, in [0, 360)."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Absolute difference between two bearings, in [0, 180]."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def polyline_length(coords: Sequence[XY]) -> float:
    return sum(dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def point_along(coords: Sequence[XY], distance: float) -> XY:
    """Point `distance` metres along the polyline (clamped to its ends)."""
    if distance <= 0:
        return coords[0]
    remaining = distance
    for i in range(len(coords) - 1):
        seg = dist(coords[i], coords[i + 1])
        if seg >= remaining and seg > 0:
            t = remaining / seg
            return (
                coords[i][0] + t * (coords[i + 1][0] - coords[i][0]),
                coords[i][1] + t * (coords[i + 1][1] - coords[i][1]),
            )
        remaining -= seg
    return coords[-1]


def densify(coords: Sequence[XY], step: float) -> list[XY]:
    """Resample a polyline at a fixed spacing (always keeps the last vertex)."""
    total = polyline_length(coords)
    if total == 0:
        return [coords[0]]
    n = max(1, int(math.ceil(total / step)))
    return [point_along(coords, total * i / n) for i in range(n + 1)]


def local_heading(coords: Sequence[XY], i: int) -> float:
    """Heading of a densified polyline at vertex i (central difference)."""
    a = coords[max(0, i - 1)]
    b = coords[min(len(coords) - 1, i + 1)]
    return bearing(a, b)
