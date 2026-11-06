"""Robustness sweep: success rate of every method against increasing map differences."""

from __future__ import annotations

import csv
from pathlib import Path

from .mapbox import MapboxClient
from .network import RoadNetwork
from .pipeline import default_methods, run_scenario
from .scenarios import SCENARIOS, Scenario
from .sender_map import MapDifference, build_sender_map

DEFAULT_SHIFTS = (0, 2, 4, 6, 8, 10, 12, 15, 20)


def run_sweep(receiver: RoadNetwork, shifts=DEFAULT_SHIFTS, directions: int = 12, warp_m: float = 1.5,
              simplify_m: float = 1.0, scenarios: dict[str, Scenario] = SCENARIOS,
              disambiguate: bool = True, mapbox: MapboxClient | None = None, progress=print) -> list[dict]:
    """For every offset magnitude, displace map A in `directions` evenly spaced directions
    (each with a different random warp) and run all scenarios."""
    rows = []
    for shift in shifts:
        for k in range(directions):
            diff = MapDifference(shift_m=shift, shift_bearing=360.0 * k / directions,
                                 warp_m=warp_m, simplify_m=simplify_m, seed=k)
            sender = build_sender_map(receiver, diff)
            for scenario in scenarios.values():
                run = run_scenario(scenario, receiver, sender, diff, default_methods(mapbox),
                                   disambiguate, mapbox)
                for method, m in run.metrics.items():
                    rows.append({
                        "scenario": scenario.key, "shift_m": shift, "shift_bearing": diff.shift_bearing,
                        "seed": k, "method": method, "correct": int(m.correct),
                        "precision": round(m.precision, 4), "recall": round(m.recall, 4),
                        "level_accuracy": round(m.level_accuracy, 4), "lrps": len(run.encoded.lrps),
                    })
        done = [r for r in rows if r["shift_m"] == shift]
        by_method: dict[str, list[int]] = {}
        for r in done:
            by_method.setdefault(r["method"], []).append(r["correct"])
        summary = ", ".join(f"{meth} {100 * sum(ok) / len(ok):.0f}%" for meth, ok in by_method.items())
        progress(f"  offset {shift:>4} m: {summary}")
    return rows


def write_csv(rows: list[dict], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return out
