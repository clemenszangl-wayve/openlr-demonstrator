"""Console and Markdown reporting."""

from __future__ import annotations

from pathlib import Path

from .network import RoadNetwork
from .pipeline import METHODS, ScenarioRun
from .viz import roads_used

MAPBOX_NOTE = ("Mapbox matches on its own map and returns geometry plus a road class per segment, not OSM IDs. "
               "For scoring, its geometry is transferred onto OSM lines of the road class Mapbox reported "
               "(motorway vs. trunk is what separates the stacked roads).")


def _levels(run: ScenarioRun) -> str:
    return ", ".join(f"{lv:+d}" for lv in sorted({ln.level for ln in run.truth.lines}))


def lrp_table(run: ScenarioRun) -> list[str]:
    rows = ["| # | lon, lat (as transmitted) | FRC | FOW | BEAR | LFRCNP | DNP | node |",
            "|---|---|---|---|---|---|---|---|"]
    for lrp, point in zip(run.encoded.lrps, run.decoded.reference.points):
        rows.append(
            f"| {lrp.index + 1} | {point.lon:.5f}, {point.lat:.5f} | {lrp.frc.name} | {lrp.fow.name} | "
            f"{lrp.bear} deg | {'-' if lrp.is_last else lrp.lfrcnp.name} | {'-' if lrp.is_last else f'{lrp.dnp} m'} | "
            f"{'valid' if lrp.node_valid else 'invalid'} - {lrp.line.label} |")
    return rows


def results_table(run: ScenarioRun) -> list[str]:
    rows = ["| method | result | precision | recall | on right level | roads used |",
            "|---|---|---|---|---|---|"]
    for method, m in run.metrics.items():
        roads = " -> ".join(roads_used(run.results[method])) or "-"
        verdict = "**correct**" if m.correct else "**wrong**"
        rows.append(f"| {METHODS[method]} | {verdict} | {m.precision:.0%} | {m.recall:.0%} | "
                    f"{m.level_accuracy:.0%} | {roads} |")
    return rows


def console(run: ScenarioRun, net: RoadNetwork) -> str:
    e = run.encoded
    out = [
        "",
        "=" * 100,
        run.scenario.title,
        "=" * 100,
        f"route: {run.truth.length:.0f} m on OSM levels {_levels(run)}; "
        f"linestring: {len(run.polyline)} points; map A vs. OSM: {run.difference.describe()}",
        f"OpenLR: {e.binary}  ({len(e.lrps)} LRPs, {len(e.binary) * 3 // 4} bytes, "
        f"offsets +{e.p_off} m / -{e.n_off} m)",
        *[f"  {s}" for s in e.steps],
        *[f"  {row}" for row in lrp_table(run)],
        "",
    ]
    for method, m in run.metrics.items():
        verdict = "CORRECT" if m.correct else "WRONG  "
        roads = " -> ".join(roads_used(run.results[method]))
        out.append(f"  {verdict} {METHODS[method]:<50} P={m.precision:4.0%} R={m.recall:4.0%} "
                   f"level={m.level_accuracy:4.0%}  {roads[:140]}")
    if run.decoded.error:
        out.append(f"  OpenLR decoder error: {run.decoded.error}")
    if run.mapbox:
        out.append(f"  Mapbox ({run.mapbox.profile}) answered: {run.mapbox.describe(net)[:300]}")
    return "\n".join(out)


def markdown(runs: list[ScenarioRun], out_dir: Path, net: RoadNetwork, sweep_png: Path | None = None) -> Path:
    lines = ["# OpenLR vs. linestring map matching on stacked roads (Tokyo, OSM)", ""]
    if runs:
        lines += [f"Map A (encoder side) vs. OSM (decoder side): **{runs[0].difference.describe()}**.", ""]
        lines += ["| scenario | " + " | ".join(METHODS[m] for m in runs[0].metrics) + " |",
                  "|---|" + "---|" * len(runs[0].metrics)]
        for run in runs:
            cells = [("correct" if m.correct else "**wrong**") + f" ({m.level_accuracy:.0%} right level)"
                     for m in run.metrics.values()]
            lines.append(f"| [{run.scenario.key}](#{run.scenario.key}) | " + " | ".join(cells) + " |")
        lines.append("")
        if any(run.mapbox for run in runs):
            lines += [f"Mapbox profile: `{next(r.mapbox.profile for r in runs if r.mapbox)}`. {MAPBOX_NOTE}", ""]
    if sweep_png:
        lines += ["## Robustness sweep", "", f"![sweep]({sweep_png.relative_to(out_dir)})", ""]
    for run in runs:
        key = run.scenario.key
        lines += [
            f"<a id='{key}'></a>",
            f"## {run.scenario.title}",
            "",
            run.scenario.description,
            "",
            f"* route length {run.truth.length:.0f} m, OSM levels {_levels(run)}",
            f"* interactive map: [{key}/map.html]({key}/map.html), 3D view: [{key}/map3d.html]({key}/map3d.html)",
            f"* OpenLR binary (base64): `{run.encoded.binary}` - {len(run.encoded.lrps)} LRPs, "
            f"positive offset {run.encoded.p_off} m, negative offset {run.encoded.n_off} m",
            f"* the linestring approach transmits {len(run.polyline)} coordinates",
            *([f"* Mapbox answered: {run.mapbox.describe(net)}"] if run.mapbox else []),
            "",
            "Encoder log (whitepaper section 11.1):",
            "",
            *[f"* {s}" for s in run.encoded.steps],
            "",
            *lrp_table(run),
            "",
            *results_table(run),
            "",
            f"![level profile]({key}/level_profile.png)",
            "",
        ]
    out = out_dir / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
