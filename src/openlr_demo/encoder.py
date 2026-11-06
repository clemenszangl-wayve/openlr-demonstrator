"""OpenLR line location encoder, following the OpenLR whitepaper v1.5, section 11.1.

The physical (binary, version 3) encoding is delegated to TomTom's ``openlr`` package;
everything map dependent - validity checks, valid nodes, shortest-path coverage,
intermediate LRPs and the LRP attributes - is implemented here.

Step numbers in the comments refer to the whitepaper.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from openlr import FRC, FOW, LineLocationReference, LocationReferencePoint, binary_encode

from .geo import angle_diff, bearing, point_along
from .network import Line, RoadNetwork

MAX_LRP_DISTANCE = 15_000.0  # Rule 1
BEAR_DIST = 20.0  # Table 7, BEARDIST


def _lrp_bearing(line: Line, last: bool) -> float:
    """BEAR (5.2.4): towards the point BEARDIST along the line; backwards for the last LRP."""
    if last:
        return bearing(line.xy[-1], point_along(line.xy[::-1], BEAR_DIST))
    return bearing(line.xy[0], point_along(line.xy, BEAR_DIST))


class EncodingError(Exception):
    pass


@dataclass
class LRP:
    """A location reference point together with the map objects it was derived from."""

    index: int
    node: int
    line: Line  # outgoing line (incoming line for the last LRP)
    lon: float
    lat: float
    frc: FRC
    fow: FOW
    bear: int
    lfrcnp: FRC
    dnp: int
    path: list[Line] = field(default_factory=list)  # location reference path to the next LRP
    node_valid: bool = True

    @property
    def is_last(self) -> bool:
        return not self.path

    def to_openlr(self) -> LocationReferencePoint:
        return LocationReferencePoint(self.lon, self.lat, self.frc, self.fow, self.bear, self.lfrcnp, self.dnp)


@dataclass
class EncodedLocation:
    location: list[Line]
    lines: list[Line]  # location after expansion to valid nodes (step 2)
    p_off: int  # metres, relative to `lines`
    n_off: int
    lrps: list[LRP]
    reference: LineLocationReference
    binary: str
    steps: list[str]

    @property
    def length(self) -> float:
        return sum(ln.length for ln in self.location)


class LineEncoder:
    """Line location encoder.

    `disambiguate` enables the optional critical-situation check of step 8: if the line of an
    LRP has a sibling line at the same node with identical FRC and FOW and a similar bearing
    (e.g. a Y-split into two slip roads), the decoder cannot tell them apart. An extra
    intermediate LRP right behind the split then pins the correct branch.
    """

    def __init__(self, network: RoadNetwork, max_lrp_distance: float = MAX_LRP_DISTANCE,
                 disambiguate: bool = True, ambiguous_bearing_deg: float = 22.5):
        self.net = network
        self.max_dist = max_lrp_distance
        self.disambiguate = disambiguate
        self.ambiguous_bearing = ambiguous_bearing_deg

    def encode(self, location: list[Line], p_off: float = 0.0, n_off: float = 0.0) -> EncodedLocation:
        steps: list[str] = []
        lines = list(location)

        # Step 1 - check validity of the location and the offsets.
        if not lines:
            raise EncodingError("empty location")
        for a, b in zip(lines, lines[1:]):
            if a.end != b.start:
                raise EncodingError(f"location not connected between {a.id} and {b.id}")
        if p_off < 0 or n_off < 0 or p_off + n_off >= sum(ln.length for ln in lines):
            raise EncodingError("offsets exceed the location length")
        while p_off >= lines[0].length:
            p_off -= lines.pop(0).length
        while n_off >= lines[-1].length:
            n_off -= lines.pop().length
        if max(abs(lat) for ln in lines for _, lat in ln.coords) > 65.7:
            raise EncodingError("binary format: Rule 1 needs adapting above |lat| 65.7 deg")
        steps.append(f"Step 1: location valid - {len(lines)} connected lines, "
                     f"{sum(ln.length for ln in lines):.0f} m")

        # Step 2 - expand start / end of the location to valid nodes (Rule 4).
        p_off, n_off, n_before, n_after = self._expand(lines, p_off, n_off)
        steps.append(f"Step 2: expanded by {n_before} line(s) at the start and {n_after} at the end "
                     f"to reach valid nodes (offsets now {p_off:.0f} m / {n_off:.0f} m)")

        # Steps 3-7 - cover the location with shortest paths, adding intermediate LRPs.
        lrp_idx = self._cover_with_shortest_paths(lines, steps)

        # Steps 8-9 - Rule 1 (max. 15 km between LRPs) and offsets vs. first/last LRP path.
        if self.disambiguate:
            lrp_idx = self._pin_ambiguous_branches(lines, lrp_idx, steps)
        lrp_idx = self._enforce_max_distance(lines, lrp_idx, steps)
        lines, lrp_idx, p_off, n_off = self._drop_superfluous_ends(lines, lrp_idx, p_off, n_off, steps)

        lrps = self._build_lrps(lines, lrp_idx)
        # Relative offsets (binary format, 5.4): 256 buckets of the first / last LRP path, < 1.
        poffs = min(round(p_off) / lrps[0].dnp, 255.5 / 256) if p_off >= 0.5 else 0.0
        noffs = min(round(n_off) / lrps[-2].dnp, 255.5 / 256) if n_off >= 0.5 else 0.0

        # Step 10 - physical representation (binary format version 3, base64).
        reference = LineLocationReference([p.to_openlr() for p in lrps], poffs, noffs)
        binary = binary_encode(reference)
        steps.append(f"Step 10: {len(lrps)} LRPs -> {len(binary)} base64 characters")
        return EncodedLocation(list(location), lines, round(p_off), round(n_off), lrps, reference, binary, steps)

    # ------------------------------------------------------------------ step 2
    def _expand(self, lines: list[Line], p_off: float, n_off: float):
        net, before, after = self.net, 0, 0
        while not net.is_valid_node(lines[0].start) and p_off < self.max_dist:
            first = lines[0]
            incoming = [ln for ln in net.in_lines[first.start] if ln.start != first.end]
            if len(incoming) != 1 or incoming[0] in lines:
                break
            lines.insert(0, incoming[0])
            p_off += incoming[0].length
            before += 1
        while not net.is_valid_node(lines[-1].end) and n_off < self.max_dist:
            last = lines[-1]
            outgoing = [ln for ln in net.out_lines[last.end] if ln.end != last.start]
            if len(outgoing) != 1 or outgoing[0] in lines:
                break
            lines.append(outgoing[0])
            n_off += outgoing[0].length
            after += 1
        return p_off, n_off, before, after

    # ------------------------------------------------------------------ steps 3-7
    def _covers(self, lines: list[Line], i: int, j: int) -> int | None:
        """Index (relative to i) of the first line where the shortest path from lines[i] to
        lines[j] deviates from the location, or None if the location is covered."""
        sp = self.net.route(lines[i], lines[j])
        part = lines[i:j + 1]
        for k, (a, b) in enumerate(zip(sp, part)):
            if a is not b:
                return k
        return None if len(sp) == len(part) else min(len(sp), len(part))

    def _cover_with_shortest_paths(self, lines: list[Line], steps: list[str]) -> list[int]:
        last = len(lines) - 1
        lrp_idx, cur = [0], 0
        while True:
            # Step 3 / 4 - shortest path from the current start line to the end line.
            k = self._covers(lines, cur, last)
            if k is None:
                break
            # Step 5 - new intermediate LRP where the shortest path leaves the location,
            # moved back to the closest valid node if necessary.
            j = cur + max(k, 1)
            for jj in range(j, cur, -1):
                if self.net.is_valid_node(lines[jj].start) and self._covers(lines, cur, jj) is None:
                    j = jj
                    break
            steps.append(f"Step 5: shortest path deviates from the location after "
                         f"{sum(ln.length for ln in lines[cur:cur + k]):.0f} m -> intermediate LRP "
                         f"at node {lines[j].start} ({lines[j].label})")
            lrp_idx.append(j)
            cur = j  # Step 6 - continue from the new intermediate.
        lrp_idx.append(last)
        steps.append(f"Step 7: location covered by {len(lrp_idx) - 1} shortest path(s)")
        return lrp_idx

    # ------------------------------------------------------------------ steps 8-9
    def _has_twin(self, line: Line, outgoing: bool) -> Line | None:
        node = line.start if outgoing else line.end
        siblings = self.net.out_lines[node] if outgoing else self.net.in_lines[node]
        bear = _lrp_bearing(line, not outgoing)
        for other in siblings:
            if (other is not line and other.frc == line.frc and other.fow == line.fow
                    and angle_diff(_lrp_bearing(other, not outgoing), bear) <= self.ambiguous_bearing):
                return other
        return None

    def _pin_ambiguous_branches(self, lines: list[Line], lrp_idx: list[int], steps: list[str]) -> list[int]:
        # lrp_idx[:-1] are LRPs at the start node of lines[i]; lrp_idx[-1] is the end node of the last line
        last = len(lines) - 1
        starts = list(lrp_idx[:-1])
        for idx in lrp_idx[:-1]:
            twin = self._has_twin(lines[idx], outgoing=True)
            if twin and idx + 1 <= last and idx + 1 not in starts:
                starts.append(idx + 1)
                steps.append(f"Step 8: LRP at node {lines[idx].start} is ambiguous ({lines[idx].label} vs. "
                             f"{twin.label}) -> extra LRP at node {lines[idx + 1].start}")
        twin = self._has_twin(lines[last], outgoing=False)
        if twin and last not in starts:
            starts.append(last)
            steps.append(f"Step 8: last LRP is ambiguous ({lines[last].label} vs. {twin.label}) "
                         f"-> extra LRP at node {lines[last].start}")
        return [*sorted(starts), last]

    def _segment(self, lines: list[Line], lrp_idx: list[int], i: int) -> list[Line]:
        a, b = lrp_idx[i], lrp_idx[i + 1]
        return lines[a:b + 1] if i + 1 == len(lrp_idx) - 1 else lines[a:b]

    def _enforce_max_distance(self, lines: list[Line], lrp_idx: list[int], steps: list[str]) -> list[int]:
        """Insert LRPs (preferably on valid nodes) so that no DNP exceeds Rule 1.

        Sub-paths of a shortest path are shortest paths, so coverage is preserved."""
        result = [lrp_idx[0]]
        for i in range(len(lrp_idx) - 1):
            start, b = lrp_idx[i], lrp_idx[i + 1]
            end = b + 1 if i + 1 == len(lrp_idx) - 1 else b
            acc, best, j = 0.0, None, start
            while j < end:
                if j > start and self.net.is_valid_node(lines[j].start):
                    best = j
                if j > start and acc + lines[j].length > self.max_dist:
                    split = best if best is not None else j
                    result.append(split)
                    steps.append(f"Step 9: inserted LRP at node {lines[split].start} (Rule 1, 15 km)")
                    start, best = split, None
                    acc = sum(ln.length for ln in lines[split:j])
                    continue
                acc += lines[j].length
                j += 1
            result.append(b)
        return result

    def _drop_superfluous_ends(self, lines, lrp_idx, p_off, n_off, steps):
        """Rule 5: the offsets must be shorter than the path of the first / last LRP pair.

        Returns the lines between the remaining first and last LRP, re-indexed."""
        def seg_len(i: int) -> float:
            return sum(ln.length for ln in self._segment(lines, lrp_idx, i))

        while len(lrp_idx) > 2 and p_off >= seg_len(0):
            p_off -= seg_len(0)
            lrp_idx.pop(0)
            steps.append("Step 9: removed first LRP (positive offset longer than its path)")
        while len(lrp_idx) > 2 and n_off >= seg_len(len(lrp_idx) - 2):
            n_off -= seg_len(len(lrp_idx) - 2)
            lrp_idx.pop()
            lrp_idx[-1] -= 1  # the last LRP refers to the end of its line, i.e. the line before
            steps.append("Step 9: removed last LRP (negative offset longer than its path)")
        first, last = lrp_idx[0], lrp_idx[-1]
        return lines[first:last + 1], [i - first for i in lrp_idx], p_off, n_off

    # ------------------------------------------------------------------ attributes (step 7)
    def _build_lrps(self, lines: list[Line], lrp_idx: list[int]) -> list[LRP]:
        lrps = []
        for i, idx in enumerate(lrp_idx):
            is_last = i == len(lrp_idx) - 1
            line = lines[idx]
            bear = _lrp_bearing(line, is_last)
            if is_last:
                node, path = line.end, []
                lfrcnp, dnp = FRC.FRC7, 0
            else:
                node, path = line.start, self._segment(lines, lrp_idx, i)
                lfrcnp = FRC(max(ln.frc for ln in path))
                dnp = round(sum(ln.length for ln in path))  # Rule 2: integer lengths
            lon, lat = self.net.nodes[node]
            lrps.append(LRP(i, node, line, lon, lat, line.frc, line.fow, round(bear) % 360,
                            lfrcnp, dnp, path, self.net.is_valid_node(node)))
        return lrps
