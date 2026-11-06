"""Stacked-road test routes in Shibuya / Setagaya (Tokyo).

* Shuto Expressway No.3 Shibuya Line runs on a viaduct (OSM layer 2-4) directly above
  National Route 246 (Tamagawa-dori), which itself dives into a tunnel (layer -1) near
  Dogenzaka-ue - up to three road levels on top of each other.
* The Shuto C2 Central Circular Route runs in the Yamate Tunnel (layer -2) right below
  Yamate-dori (Tokyo Metropolitan Road 317) - horizontally only 0.5-3 m apart in OSM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shapely.geometry import Point

from .evaluation import PathResult
from .network import Line, RoadNetwork


@dataclass(frozen=True)
class Anchor:
    lon: float
    lat: float
    heading: float
    on: Callable[[Line], bool] | None = None  # road the anchor must snap to (default: scenario roads)


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    anchors: tuple[Anchor, ...]
    allowed: Callable[[Line], bool]

    def route(self, net: RoadNetwork) -> PathResult:
        """Ground-truth route: shortest path through the anchors over the allowed roads.

        The route starts / ends exactly at the first / last anchor (-> OpenLR offsets)."""
        anchor_lines = []
        for a in self.anchors:
            line = net.nearest_line(net.proj.to_xy(a.lon, a.lat), a.heading, 60.0, a.on or self.allowed)
            if line is None:
                raise ValueError(f"{self.key}: no allowed road near {a}")
            anchor_lines.append(line)
        lines = [anchor_lines[0]]
        for line in anchor_lines[1:]:
            leg = net.route(lines[-1], line, accept=self.allowed)
            if leg is None:
                raise ValueError(f"{self.key}: anchors not connected ({lines[-1].id} -> {line.id})")
            lines.extend(leg[1:])
        first, last = self.anchors[0], self.anchors[-1]
        p_off = lines[0].geom_xy.project(Point(net.proj.to_xy(first.lon, first.lat)))
        n_off = lines[-1].length - lines[-1].geom_xy.project(Point(net.proj.to_xy(last.lon, last.lat)))
        return PathResult(lines, p_off, n_off)


def _is(highway: str | tuple[str, ...], ref: str | None = None, name: str | None = None):
    highways = (highway,) if isinstance(highway, str) else highway

    def accept(line: Line) -> bool:
        t = line.tags
        return (t.get("highway") in highways
                and (ref is None or t.get("ref") == ref)
                and (name is None or name in t.get("name", "")))
    return accept


def _any(*preds):
    return lambda line: any(p(line) for p in preds)


SHUTO3 = _is("motorway", ref="3")
R246 = _is(("trunk", "trunk_link"), ref="246")
C2 = _is(("motorway", "motorway_link"), ref="C2")
YAMATE = _is(("primary", "primary_link"), ref="317")
IKEJIRI_ONRAMP = _is("motorway_link", name="池尻入口")
SANGENJAYA_EXIT = _is("motorway_link", name="三軒茶屋出口")

SCENARIOS: dict[str, Scenario] = {s.key: s for s in [
    Scenario(
        "shuto3-elevated",
        "Shuto Expressway No.3, Shibuya -> Sangenjaya (elevated, above Route 246)",
        "Outbound carriageway of the Shuto 3 viaduct (OSM layer +3/+4). National Route 246 runs "
        "directly below - at ground level and, near Dogenzaka-ue, in a tunnel.",
        (Anchor(139.7040, 35.6580, 265), Anchor(139.6758, 35.6459, 237)),
        SHUTO3,
    ),
    Scenario(
        "shuto3-exit",
        "Shuto 3 -> Sangenjaya off-ramp -> Route 246 (exit from the viaduct)",
        "Same Shuto 3 viaduct, but the route then leaves it via the Sangenjaya exit and continues "
        "on Route 246 at ground level. Only the viaduct connects to that ramp - Route 246 below "
        "cannot reach it.",
        (Anchor(139.7040, 35.6580, 265, SHUTO3), Anchor(139.6739, 35.6450, 235, SANGENJAYA_EXIT),
         Anchor(139.6705, 35.6427, 225, R246)),
        _any(SHUTO3, SANGENJAYA_EXIT, R246),
    ),
    Scenario(
        "r246-surface",
        "National Route 246, Shibuya -> Sangenjaya (below the expressway)",
        "Outbound Tamagawa-dori below the Shuto 3 viaduct, including the Dogenzaka-ue tunnel and "
        "the Ohashi overpass.",
        (Anchor(139.7005, 35.6572, 250), Anchor(139.6758, 35.6459, 237)),
        R246,
    ),
    Scenario(
        "c2-tunnel",
        "Shuto C2 Yamate Tunnel, Tomigaya -> Ohashi JCT (underground, below Yamate-dori)",
        "Southbound C2 carriageway in the Yamate Tunnel (OSM layer -2) which follows Yamate-dori "
        "almost exactly in plan view.",
        (Anchor(139.6879, 35.6705, 180), Anchor(139.6896, 35.6545, 185)),
        C2,
    ),
    Scenario(
        "yamate-surface",
        "Yamate-dori, Tomigaya -> Ohashi (surface, above the C2 tunnel)",
        "Southbound Yamate-dori (Tokyo Metropolitan Road 317) on top of the Yamate Tunnel.",
        (Anchor(139.6878, 35.6705, 180), Anchor(139.6899, 35.6547, 200)),
        YAMATE,
    ),
    Scenario(
        "r246-ramp-shuto3",
        "Route 246 -> Ikejiri on-ramp -> Shuto 3 (changing levels)",
        "Starts on Route 246 below the viaduct, climbs the Ikejiri on-ramp and continues on the "
        "elevated Shuto 3 - the route changes level halfway.",
        (Anchor(139.6960, 35.6560, 245, R246), Anchor(139.6834, 35.6500, 240, IKEJIRI_ONRAMP),
         Anchor(139.6758, 35.6459, 237, SHUTO3)),
        _any(R246, SHUTO3, IKEJIRI_ONRAMP),
    ),
]}
