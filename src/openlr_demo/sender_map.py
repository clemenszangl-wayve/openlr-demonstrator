"""Simulated encoder map ("map A").

OpenLR exists because sender and receiver use *different* maps - otherwise one could simply
transmit line IDs. Here map A is derived from the OSM receiver map ("map B") by applying
typical differences between map vendors / versions:

* a systematic shift (different digitisation reference, e.g. imagery offset),
* a smooth, spatially varying warp (different survey sources / rubber sheeting),
* geometric generalisation (Douglas-Peucker simplification).

Topology and road attributes are kept identical, which also gives us the ground truth.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .network import RoadNetwork


@dataclass(frozen=True)
class MapDifference:
    shift_m: float = 0.0
    shift_bearing: float = 0.0  # direction (deg from north) in which map A is displaced
    warp_m: float = 0.0
    warp_wavelength_m: float = 600.0
    simplify_m: float = 0.0
    seed: int = 7

    def displacement(self):
        rnd = random.Random(self.seed)
        a1, a2 = rnd.uniform(0, math.pi), rnd.uniform(0, math.pi)
        p1, p2 = rnd.uniform(0, 2 * math.pi), rnd.uniform(0, 2 * math.pi)
        k = 2 * math.pi / self.warp_wavelength_m
        sx = self.shift_m * math.sin(math.radians(self.shift_bearing))
        sy = self.shift_m * math.cos(math.radians(self.shift_bearing))

        def displace(x: float, y: float):
            wx = self.warp_m * math.sin(k * (x * math.cos(a1) + y * math.sin(a1)) + p1)
            wy = self.warp_m * math.sin(k * (x * math.cos(a2) + y * math.sin(a2)) + p2)
            return x + sx + wx, y + sy + wy

        return displace

    def describe(self) -> str:
        parts = []
        if self.shift_m:
            parts.append(f"shift {self.shift_m:g} m towards {self.shift_bearing:g} deg")
        if self.warp_m:
            parts.append(f"warp +/-{self.warp_m:g} m (wavelength {self.warp_wavelength_m:g} m)")
        if self.simplify_m:
            parts.append(f"simplified with {self.simplify_m:g} m tolerance")
        return ", ".join(parts) or "identical to OSM"


def build_sender_map(receiver: RoadNetwork, diff: MapDifference) -> RoadNetwork:
    return receiver.transformed(diff.displacement(), diff.simplify_m)
