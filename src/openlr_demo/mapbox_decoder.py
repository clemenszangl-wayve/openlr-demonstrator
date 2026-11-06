"""OpenLR line decoder on the Mapbox map (e.g. the ZENRIN routing profile).

Mapbox does not expose its road graph, so the decoder of whitepaper section 12 is built
from two APIs:

* **Candidate lines (step 3-4)** - Tilequery on ``mapbox-streets-v8`` lists the roads within
  a radius of every LRP with class, structure (bridge / tunnel) and one-way flag. They are
  rated like the whitepaper's candidate rating: distance, FRC and FOW (equal weights); the
  bearing is enforced by the routing request.
* **Shortest path (step 5-6)** - Directions from the candidate of LRP *i* to a candidate of
  LRP *i+1*, starting in the LRP's bearing. The leg is accepted if it starts on the road of
  the candidate (same class - this is what keeps a route on the right level), its length
  matches DNP, and no road on it is less important than LFRCNP.
* **Backtracking (step 7)** - if a leg fails, the next candidate combination is tried.
* **Offsets (step 8)** - positive / negative offset are cut from the first / last leg.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from openlr import FOW, LineLocationReference, binary_decode

from .geo import LocalProjection, LonLat, polyline_length
from .mapbox import MapboxClient, MapboxError, MapboxSegment, class_frc, parse_response

SEARCH_RADIUS_M = 50.0
MAX_CANDIDATES = 4
SNAP_RADII_M = (4.0, 12.0)  # tight first: stacked roads are only metres apart
BEARING_RANGE_DEG = 40
DNP_TOLERANCE = (40.0, 0.10)  # metres + share of DNP (the binary DNP has 58.6 m resolution)
LFRCNP_SLACK = 1
MAX_LEG_REQUESTS = 30
DRIVABLE = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
            "secondary_link", "tertiary", "tertiary_link", "street", "street_limited", "service"}


def class_fow(road_class: str, oneway: bool) -> FOW:
    if road_class == "motorway":
        return FOW.MOTORWAY
    if road_class.endswith("_link"):
        return FOW.SLIPROAD
    if road_class in ("trunk", "primary", "secondary") and oneway:
        return FOW.MULTIPLE_CARRIAGEWAY
    return FOW.SINGLE_CARRIAGEWAY


@dataclass
class Candidate:
    lrp: int
    lon: float
    lat: float
    road_class: str
    structure: str
    oneway: bool
    name: str
    distance: float
    score: float = 0.0

    @property
    def frc(self) -> int:
        return class_frc(self.road_class)

    @property
    def label(self) -> str:
        extra = "" if self.structure in ("", "none") else f", {self.structure}"
        return f"{self.name or '(unnamed)'} [{self.road_class}{extra}]"


@dataclass
class Leg:
    index: int  # from LRP index
    start: Candidate
    end: Candidate
    status: str  # "ok" or the reason for rejecting it
    length: float = 0.0
    dnp: int = 0
    start_class: str | None = None
    bearing: float | None = None
    lowest_frc: int | None = None
    segments: list[MapboxSegment] = field(default_factory=list)
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class MapboxDecoding:
    binary: str
    reference: LineLocationReference | None = None
    candidates: list[list[Candidate]] = field(default_factory=list)
    attempts: list[Leg] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    segments: list[MapboxSegment] = field(default_factory=list)  # after offsets
    p_off: float = 0.0
    n_off: float = 0.0
    checked: bool = False  # every leg passed the checks
    error: str | None = None

    @property
    def coords(self) -> list[LonLat]:
        out: list[LonLat] = []
        for s in self.segments:
            out.extend(s.coords[1:] if out and s.coords[0] == out[-1] else s.coords)
        return out


class MapboxDecoder:
    def __init__(self, client: MapboxClient, radius: float = SEARCH_RADIUS_M, max_candidates: int = MAX_CANDIDATES):
        self.client = client
        self.radius = radius
        self.max_candidates = max_candidates

    # ------------------------------------------------------------------ public
    def decode(self, binary: str) -> MapboxDecoding:
        out = MapboxDecoding(binary)
        try:
            ref = out.reference = binary_decode(binary)
        except Exception as exc:  # the openlr package raises plain exceptions for bad input
            out.error = f"not a valid OpenLR line location: {exc}"
            return out
        try:
            out.candidates = [self._candidates(i, p) for i, p in enumerate(ref.points)]
        except MapboxError as exc:
            out.error = f"Tilequery failed: {exc}"
            return out
        for i, cands in enumerate(out.candidates):
            if not cands:
                out.error = f"LRP {i + 1}: no road within {self.radius:.0f} m on the Mapbox map"
                return out
        try:
            legs = self._search(ref, out)
            out.checked = legs is not None
            if legs is None:  # best effort: the top candidates, unchecked
                legs = self._best_effort(ref, out)
        except MapboxError as exc:
            out.error = f"Directions failed: {exc}"
            return out
        if legs is None or any(not leg.segments for leg in legs):
            out.error = "no route between the LRP candidates on the Mapbox map"
            return out
        out.legs = legs
        if not out.checked:
            out.error = "no candidate combination passed the DNP / LFRCNP checks - showing the best guess"
        self._apply_offsets(ref, out)
        return out

    # ------------------------------------------------------------------ candidates
    def _candidates(self, i: int, lrp) -> list[Candidate]:
        data, _ = self.client.tilequery(lrp.lon, lrp.lat, self.radius)
        best: dict[tuple, Candidate] = {}
        for f in data.get("features", []):
            p = f.get("properties", {})
            cls = p.get("class", "")
            if cls not in DRIVABLE:
                continue
            lon, lat = f["geometry"]["coordinates"][:2]
            c = Candidate(i, lon, lat, cls, p.get("structure", "none"), str(p.get("oneway")) == "true",
                          p.get("name", "") or p.get("ref", ""), float(p.get("tilequery", {}).get("distance", 0.0)))
            c.score = self._rate(c, lrp)
            key = (cls, c.structure, c.name, round(lon, 4), round(lat, 4))
            if key not in best or best[key].score < c.score:
                best[key] = c
        ranked = sorted(best.values(), key=lambda c: -c.score)
        return ranked[: self.max_candidates]

    def _rate(self, c: Candidate, lrp) -> float:
        dist_score = max(0.0, 1.0 - c.distance / self.radius)
        frc_score = max(0.0, 1.0 - 0.25 * abs(c.frc - int(lrp.frc)))
        fow_score = 1.0 if class_fow(c.road_class, c.oneway) == lrp.fow else 0.5
        return (dist_score + frc_score + fow_score) / 3

    # ------------------------------------------------------------------ search with backtracking
    def _search(self, ref: LineLocationReference, out: MapboxDecoding) -> list[Leg] | None:
        budget = [MAX_LEG_REQUESTS]
        last = len(ref.points) - 1

        def solve(i: int, start: Candidate) -> list[Leg] | None:
            if i == last:
                return []
            for end in out.candidates[i + 1]:
                if budget[0] <= 0:
                    return None
                leg = self._leg(ref, i, start, end, budget)
                out.attempts.append(leg)
                if leg.ok:
                    rest = solve(i + 1, end)
                    if rest is not None:
                        return [leg, *rest]
            return None

        for start in out.candidates[0]:
            legs = solve(0, start)
            if legs is not None:
                return legs
        return None

    def _best_effort(self, ref: LineLocationReference, out: MapboxDecoding) -> list[Leg] | None:
        legs = []
        for i in range(len(ref.points) - 1):
            done = [a for a in out.attempts if a.index == i and a.segments]
            if done:
                legs.append(max(done, key=lambda a: a.start.score + a.end.score))
                continue
            leg = self._leg(ref, i, out.candidates[i][0], out.candidates[i + 1][0], [MAX_LEG_REQUESTS])
            out.attempts.append(leg)
            legs.append(leg)
        return legs

    def _leg(self, ref: LineLocationReference, i: int, start: Candidate, end: Candidate, budget: list[int]) -> Leg:
        a, b = ref.points[i], ref.points[i + 1]
        last = i + 1 == len(ref.points) - 1
        leg = Leg(i, start, end, "no route", dnp=a.dnp)
        arrive = ((b.bear + 180) % 360, BEARING_RANGE_DEG) if last else None
        route = None
        for radius in SNAP_RADII_M:
            budget[0] -= 1
            data, leg.cached = self.client.directions([(start.lon, start.lat), (end.lon, end.lat)],
                                                      [(a.bear, BEARING_RANGE_DEG), arrive], [radius, radius])
            if data.get("code") == "Ok" and data.get("routes"):
                route = data["routes"][0]
                break
            leg.status = f"Mapbox: {data.get('code')}"
        if route is None:
            return leg
        leg.length = float(route["distance"])
        leg.segments, _, _ = parse_response({"matchings": [route]})
        first = next((x for s in route["legs"][0]["steps"] for x in s.get("intersections", [])), {})
        leg.start_class = first.get("mapbox_streets_v8", {}).get("class")
        if first.get("bearings") and first.get("out") is not None:
            leg.bearing = first["bearings"][first["out"]]
        frcs = [s.frc for s in leg.segments if s.frc is not None]
        leg.lowest_frc = max(frcs) if frcs else None
        tolerance = DNP_TOLERANCE[0] + DNP_TOLERANCE[1] * a.dnp
        if leg.start_class and class_frc(leg.start_class) != start.frc:
            leg.status = f"route starts on {leg.start_class}, not on the candidate ({start.road_class})"
        elif abs(leg.length - a.dnp) > tolerance:
            leg.status = f"length {leg.length:.0f} m does not match DNP {a.dnp} m (±{tolerance:.0f} m)"
        elif leg.lowest_frc is not None and leg.lowest_frc > int(a.lfrcnp) + LFRCNP_SLACK:
            leg.status = f"uses FRC{leg.lowest_frc}, below LFRCNP FRC{int(a.lfrcnp)}"
        else:
            leg.status = "ok"
        return leg

    # ------------------------------------------------------------------ offsets
    def _apply_offsets(self, ref: LineLocationReference, out: MapboxDecoding) -> None:
        segments = [s for leg in out.legs for s in leg.segments]
        first = segments[0].coords[0]
        proj = LocalProjection(*first)
        out.p_off = ref.poffs * out.legs[0].length
        out.n_off = ref.noffs * out.legs[-1].length
        out.segments = _trim(_trim(segments, out.p_off, proj), out.n_off, proj, from_end=True)


def _trim(segments: list[MapboxSegment], metres: float, proj: LocalProjection, from_end: bool = False):
    """Cut `metres` off the start (or end) of a chain of segments."""
    if metres <= 0:
        return segments
    segs = [_reversed(s) for s in reversed(segments)] if from_end else list(segments)
    out, left = [], metres
    for s in segs:
        if left <= 0:
            out.append(s)
            continue
        xy = proj.coords_to_xy(s.coords)
        length = polyline_length(xy)
        if length <= left:
            left -= length
            continue
        acc, keep = 0.0, []
        for p, q in zip(xy, xy[1:]):
            d = ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2) ** 0.5
            if keep:
                keep.append(q)
            elif acc + d >= left:
                t = (left - acc) / d if d else 0.0
                keep = [(p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])), q]
            acc += d
        left = 0
        if len(keep) >= 2:
            out.append(MapboxSegment(proj.coords_to_lonlat(keep), s.road_class, s.tunnel, s.name, s.ref))
    return [_reversed(s) for s in reversed(out)] if from_end else out


def _reversed(s: MapboxSegment) -> MapboxSegment:
    return MapboxSegment(list(reversed(s.coords)), s.road_class, s.tunnel, s.name, s.ref)
