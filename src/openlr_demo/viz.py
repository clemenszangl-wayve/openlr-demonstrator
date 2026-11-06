"""Visual output: interactive 2D map (folium), 3D view (deck.gl), level profile and sweep charts."""

from __future__ import annotations

import html
import json
import math
from collections import defaultdict
from pathlib import Path

import folium
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from .evaluation import PathResult, level_profile  # noqa: E402
from .network import Line, RoadNetwork  # noqa: E402
from .pipeline import METHODS, ScenarioRun  # noqa: E402

COLORS = {"truth": "#222222", "polyline": "#2166ac", "openlr": "#1a9850", "nearest": "#d73027", "hmm": "#8e44ad",
          "mapbox": "#e7298a"}
SHORT = {"truth": "Ground truth", "openlr": "OpenLR", "nearest": "Nearest-road", "hmm": "HMM", "mapbox": "Mapbox"}
LEVEL_HEIGHT_M = 15.0  # vertical exaggeration in the 3D view (metres per OSM layer)
_BLEND_M = 60.0
_OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services"
_BASEMAPS = [  # (name, url, attribution, max native zoom); all usable without an API key
    ("Esri Light Gray", _ESRI + "/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
     "Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors", 16),
    ("OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", _OSM_ATTR, 19),
    ("Esri World Imagery (see the viaduct)", _ESRI + "/World_Imagery/MapServer/tile/{z}/{y}/{x}",
     "Tiles &copy; Esri, Maxar, Earthstar Geographics", 19),
]


def level_color(level: int) -> str:
    return "#8073ac" if level < 0 else ("#9e9e9e" if level == 0 else "#f39c12")


def _level_name(level: int) -> str:
    return "tunnel" if level < 0 else ("ground" if level == 0 else "elevated")


def context_lines(run: ScenarioRun, net: RoadNetwork, radius: float = 250.0) -> list[Line]:
    area = unary_union([LineString(p) for p in run.truth.pieces_xy() if len(p) > 1]).buffer(radius)
    seen, out = set(), []
    for line in net.lines.values():
        key = line.id.rstrip("+-")
        if key not in seen and line.geom_xy.intersects(area):
            seen.add(key)
            out.append(line)
    return out


def _latlon(net: RoadNetwork, piece) -> list[tuple[float, float]]:
    return [(lat, lon) for lon, lat in (net.proj.to_lonlat(x, y) for x, y in piece)]


def _status(run: ScenarioRun, method: str) -> str:
    m = run.metrics[method]
    if run.results.get(method) is None:
        return "FAILED (no result)"
    verdict = "correct" if m.correct else "WRONG"
    return f"{verdict} - precision {m.precision:.0%}, recall {m.recall:.0%}, right level {m.level_accuracy:.0%}"


# ---------------------------------------------------------------------- folium (2D)
def _lrp_popup(run: ScenarioRun, i: int) -> str:
    lrp = run.encoded.lrps[i]
    point = run.decoded.reference.points[i]
    rows = [
        ("LRP", f"{i + 1} of {len(run.encoded.lrps)}"),
        ("COORD", f"{point.lon:.5f}, {point.lat:.5f}"),
        ("FRC", lrp.frc.name),
        ("FOW", lrp.fow.name),
        ("BEAR", f"{lrp.bear} deg (sector {point.bear} deg)"),
        ("LFRCNP", "-" if lrp.is_last else lrp.lfrcnp.name),
        ("DNP", "-" if lrp.is_last else f"{lrp.dnp} m (binary: {point.dnp} m)"),
        ("encoder line", html.escape(lrp.line.label)),
        ("node valid (Rule 4)", "yes" if lrp.node_valid else "no (no alternative)"),
    ]
    table = "".join(f"<tr><th style='text-align:left;padding-right:8px'>{k}</th><td>{v}</td></tr>" for k, v in rows)
    cands = run.decoded.candidates.get(i, [])[:5]
    ctab = "".join(
        f"<tr><td>{c.score:.3f}</td><td>{html.escape(c.line.label)}</td><td>{c.line.frc.name}/{c.line.fow.name}</td></tr>"
        for c in cands
    )
    return (f"<div style='font-size:12px'><table>{table}</table><br><b>Decoder candidates (score)</b>"
            f"<table>{ctab}</table></div>")


def folium_map(run: ScenarioRun, receiver: RoadNetwork, sender: RoadNetwork, out: Path) -> Path:
    pieces = run.truth.pieces_xy()
    center = _latlon(receiver, [pieces[len(pieces) // 2][0]])[0]
    fmap = folium.Map(location=center, zoom_start=16, tiles=None, control_scale=True, max_zoom=21)
    for i, (name, url, attr, native) in enumerate(_BASEMAPS):
        folium.TileLayer(url, name=name, attr=attr, max_zoom=21, max_native_zoom=native, show=i == 0).add_to(fmap)
    route_latlon = [p for piece in pieces for p in _latlon(receiver, piece)]
    fmap.fit_bounds([[min(p[0] for p in route_latlon), min(p[1] for p in route_latlon)],
                     [max(p[0] for p in route_latlon), max(p[1] for p in route_latlon)]], padding=(40, 40))

    fg = folium.FeatureGroup("OSM road network by level (orange = elevated, grey = ground, purple = tunnel)")
    for line in sorted(context_lines(run, receiver), key=lambda ln: ln.level):
        folium.PolyLine(_latlon(receiver, line.xy), color=level_color(line.level), weight=3, opacity=0.8,
                        dash_array="6 5" if line.level < 0 else None,
                        tooltip=f"{html.escape(line.label)}<br>{line.frc.name} / {line.fow.name}").add_to(fg)
    fg.add_to(fmap)

    fg = folium.FeatureGroup("Ground truth (route on OSM)")
    for piece in pieces:
        folium.PolyLine(_latlon(receiver, piece), color=COLORS["truth"], weight=14, opacity=0.22,
                        tooltip="Ground truth").add_to(fg)
    fg.add_to(fmap)

    fg = folium.FeatureGroup("Transmitted linestring (route geometry from map A)", show=True)
    folium.PolyLine([(lat, lon) for lon, lat in run.polyline], color=COLORS["polyline"], weight=2,
                    dash_array="4 4", tooltip="Linestring sent to the matchers (map A geometry)").add_to(fg)
    fg.add_to(fmap)

    widths = {"openlr": 7, "hmm": 5, "mapbox": 4, "nearest": 3}
    for method, result in run.results.items():
        fg = folium.FeatureGroup(f"{METHODS[method]}: {_status(run, method)}")
        if result is not None:
            for line, piece in result.line_pieces():
                folium.PolyLine(_latlon(receiver, piece), color=COLORS[method], weight=widths[method], opacity=0.85,
                                tooltip=f"{SHORT[method]}: {html.escape(line.label)}").add_to(fg)
        fg.add_to(fmap)

    if run.mapbox and run.mapbox.segments:
        fg = folium.FeatureGroup(f"Mapbox raw answer ({run.mapbox.profile} geometry, class per segment)", show=False)
        for seg in run.mapbox.segments:
            tip = f"Mapbox: {seg.road_class or '?'}{' tunnel' if seg.tunnel else ''} - {html.escape(seg.name or '-')}"
            folium.PolyLine([(lat, lon) for lon, lat in seg.coords], color=COLORS["mapbox"], weight=2,
                            dash_array="2 4", tooltip=tip).add_to(fg)
        fg.add_to(fmap)

    fg = folium.FeatureGroup("OpenLR location reference points (click)")
    for i, point in enumerate(run.decoded.reference.points):
        folium.CircleMarker((point.lat, point.lon), radius=8, color="#000", weight=2, fill=True,
                            fill_color=COLORS["openlr"], fill_opacity=1,
                            popup=folium.Popup(_lrp_popup(run, i), max_width=520),
                            tooltip=f"LRP {i + 1}: {run.encoded.lrps[i].frc.name} / {run.encoded.lrps[i].fow.name}").add_to(fg)
    fg.add_to(fmap)

    rows = "".join(
        f"<tr><td><span style='display:inline-block;width:18px;height:4px;background:{COLORS[m]}'></span> "
        f"{SHORT[m]}</td><td>{_status(run, m)}</td></tr>" for m in run.results)
    panel = f"""
    <div style="position:fixed;top:10px;left:50px;z-index:9999;background:white;padding:10px 12px;
                border-radius:6px;box-shadow:0 1px 6px rgba(0,0,0,.3);font:12px sans-serif;max-width:560px">
      <b style="font-size:14px">{html.escape(run.scenario.title)}</b><br>
      <span>{html.escape(run.scenario.description)}</span><br>
      <span>Map A vs. OSM: {html.escape(run.difference.describe())}</span><br>
      <span>OpenLR ({len(run.encoded.lrps)} LRPs): <code>{run.encoded.binary}</code></span>
      <table style="margin-top:6px">{rows}</table>
    </div>"""
    fmap.get_root().html.add_child(folium.Element(panel))
    folium.LayerControl(collapsed=True).add_to(fmap)
    out.parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(out))
    return out


# ---------------------------------------------------------------------- deck.gl (3D)
def node_heights(net: RoadNetwork) -> dict[int, float]:
    acc: dict[int, list[int]] = defaultdict(list)
    for line in net.lines.values():
        acc[line.start].append(line.level)
        acc[line.end].append(line.level)
    return {n: sum(v) / len(v) * LEVEL_HEIGHT_M for n, v in acc.items()}


def path3d(net: RoadNetwork, line: Line, piece, heights, lift: float = 0.0):
    z0, z1, zl = heights[line.start], heights[line.end], line.level * LEVEL_HEIGHT_M
    out = []
    for x, y in piece:
        d = line.geom_xy.project(Point(x, y))
        if line.length <= 2 * _BLEND_M:
            z = z0 + (z1 - z0) * d / line.length
        else:  # own level in the middle, blend towards the (shared) node heights at the ends
            w0 = max(0.0, 1 - d / _BLEND_M)
            w1 = max(0.0, 1 - (line.length - d) / _BLEND_M)
            z = zl * (1 - w0 - w1) + z0 * w0 + z1 * w1
        lon, lat = net.proj.to_lonlat(x, y)
        out.append([round(lon, 7), round(lat, 7), round(z + lift, 1)])
    return out


def hex_rgb(color: str) -> list[int]:
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


def deck_3d(run: ScenarioRun, receiver: RoadNetwork, out: Path) -> Path:
    heights = node_heights(receiver)
    network = [{"path": path3d(receiver, ln, ln.xy, heights), "color": hex_rgb(level_color(ln.level)),
                "label": f"{ln.label} - {ln.frc.name}/{ln.fow.name}"}
               for ln in context_lines(run, receiver, 400.0)]
    routes = []
    lifts = {"truth": 0.0, "openlr": 3.0, "hmm": 6.0, "nearest": 9.0, "mapbox": 12.0}
    widths = {"truth": 16, "openlr": 9, "hmm": 7, "nearest": 5, "mapbox": 5}
    for method, result in [("truth", run.truth), *run.results.items()]:
        if result is None:
            continue
        for line, piece in result.line_pieces():
            routes.append({"path": path3d(receiver, line, piece, heights, lifts[method]),
                           "color": hex_rgb(COLORS[method]) + ([90] if method == "truth" else [235]),
                           "width": widths[method], "label": f"{SHORT[method]}: {line.label}"})
    lrps = []
    for i, (p, lrp) in enumerate(zip(run.decoded.reference.points, run.encoded.lrps)):
        z = heights.get(lrp.node, 0.0)
        lrps.append({"position": [p.lon, p.lat, z + 12], "base": [p.lon, p.lat, z],
                     "label": f"LRP {i + 1}: {lrp.frc.name} / {lrp.fow.name} / bear {lrp.bear} / "
                              f"LFRCNP {lrp.lfrcnp.name} / DNP {lrp.dnp} m"})
    lon, lat = run.polyline[len(run.polyline) // 2]
    legend = "".join(
        f"<div><span style='display:inline-block;width:20px;height:5px;background:{COLORS[m]};margin-right:6px'></span>"
        f"{SHORT[m]}{'' if m == 'truth' else ': ' + _status(run, m)}</div>" for m in ["truth", *run.results])
    page = _DECK_TEMPLATE.format(
        title=html.escape(run.scenario.title), legend=legend, diff=html.escape(run.difference.describe()),
        data=json.dumps({"network": network, "routes": routes, "lrps": lrps}),
        lon=lon, lat=lat, level=LEVEL_HEIGHT_M,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


_DECK_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title} - 3D</title>
<script src="https://unpkg.com/deck.gl@9.1.14/dist.min.js"></script>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet"/>
<style>body{{margin:0}}#map{{position:absolute;inset:0}}#panel{{position:absolute;top:10px;left:10px;z-index:2;
background:#fff;padding:10px 12px;border-radius:6px;box-shadow:0 1px 6px rgba(0,0,0,.3);font:12px sans-serif;max-width:520px}}</style>
</head><body>
<div id="map"></div>
<div id="panel"><b style="font-size:14px">{title}</b><br>Map A vs. OSM: {diff}<br>
Heights: OSM <code>layer</code> x {level:g} m (exaggerated). Right-drag or Ctrl+drag to rotate / tilt.
<div style="margin-top:6px">{legend}</div>
<div style="margin-top:6px;color:#555">Road network: <span style="color:#f39c12">elevated</span>,
<span style="color:#777">ground</span>, <span style="color:#8073ac">tunnel</span></div></div>
<script>
const DATA = {data};
const map = new maplibregl.Map({{
  container: 'map', style: 'https://tiles.openfreemap.org/styles/positron',
  center: [{lon}, {lat}], zoom: 16.2, pitch: 62, bearing: -30, maxPitch: 85,
  preserveDrawingBuffer: true, attributionControl: {{compact: true}},
}});
map.addControl(new maplibregl.NavigationControl({{visualizePitch: true}}), 'top-right');
map.addControl(new deck.MapboxOverlay({{
  interleaved: true,
  getTooltip: ({{object}}) => object && object.label,
  layers: [
    new deck.PathLayer({{id: 'network', data: DATA.network, getPath: d => d.path, getColor: d => d.color,
      getWidth: 3, widthUnits: 'pixels', pickable: true, opacity: 0.9}}),
    new deck.PathLayer({{id: 'routes', data: DATA.routes, getPath: d => d.path, getColor: d => d.color,
      getWidth: d => d.width, widthUnits: 'pixels', pickable: true, capRounded: true, jointRounded: true}}),
    new deck.LineLayer({{id: 'lrp-stems', data: DATA.lrps, getSourcePosition: d => d.base,
      getTargetPosition: d => d.position, getColor: [0, 0, 0], getWidth: 2}}),
    new deck.ScatterplotLayer({{id: 'lrps', data: DATA.lrps, getPosition: d => d.position, getRadius: 9,
      radiusUnits: 'pixels', getFillColor: [26, 152, 80], getLineColor: [0, 0, 0], stroked: true,
      lineWidthMinPixels: 2, pickable: true, billboard: true}}),
  ]
}}));
</script></body></html>
"""


# ---------------------------------------------------------------------- matplotlib charts
def level_profile_png(run: ScenarioRun, out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(11, 3.8))
    truth = level_profile(run.truth, run.truth)
    xs = [d / 1000 for d, _, _ in truth]
    ax.plot(xs, [t for _, t, _ in truth], color=COLORS["truth"], lw=9, alpha=0.18,
            solid_capstyle="butt", label="Ground truth")
    offsets = {"openlr": 0.0, "hmm": -0.12, "nearest": 0.12, "mapbox": -0.24}
    for method, result in run.results.items():
        prof = level_profile(run.truth, result)
        ys = [math.nan if r is None else r + offsets[method] for _, _, r in prof]
        ok = "correct" if run.metrics[method].correct else "wrong"
        ax.plot(xs, ys, color=COLORS[method], lw=2.2, label=f"{SHORT[method]} ({ok}, "
                f"{run.metrics[method].level_accuracy:.0%} on right level)")
    levels = sorted({t for _, t, _ in truth} | {0}
                    | {r for m in run.results.values() for _, _, r in level_profile(run.truth, m) if r is not None})
    ax.set_yticks(levels, [f"{lv:+d} ({_level_name(lv)})" for lv in levels])
    ax.set_ylim(min(levels) - 0.6, max(levels) + 0.6)
    ax.set_xlabel("distance along the route [km]")
    ax.set_ylabel("OSM layer of matched road")
    ax.set_title(f"{run.scenario.key}: which level did each method choose?\n"
                 f"map A vs. OSM: {run.difference.describe()}", fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2, fontsize=8, frameon=False)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def sweep_png(rows: list[dict], out: Path) -> Path:
    scenarios = sorted({r["scenario"] for r in rows}, key=lambda s: [r["scenario"] for r in rows].index(s))
    methods = [m for m in METHODS if any(r["method"] == m for r in rows)]
    panels = ["all scenarios", *scenarios]
    ncols = 3
    nrows = math.ceil(len(panels) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 3.4 * nrows), sharey=True, squeeze=False)
    for ax, panel in zip(axes.flat, panels):
        subset = rows if panel == "all scenarios" else [r for r in rows if r["scenario"] == panel]
        shifts = sorted({r["shift_m"] for r in subset})
        for method in methods:
            rate = []
            for s in shifts:
                sel = [r for r in subset if r["shift_m"] == s and r["method"] == method]
                rate.append(100 * sum(r["correct"] for r in sel) / len(sel))
            ax.plot(shifts, rate, marker="o", ms=4, lw=2, color=COLORS[method], label=SHORT[method])
        ax.set_title(panel, fontsize=10)
        ax.set_xlabel("systematic offset map A vs. OSM [m]")
        ax.set_ylim(-5, 105)
        ax.grid(alpha=0.3)
    for ax in axes.flat[len(panels):]:
        ax.axis("off")
    for ax in axes[:, 0]:
        ax.set_ylabel("correctly matched routes [%]")
    axes.flat[0].legend(fontsize=9)
    fig.suptitle("Robustness against map differences (all offset directions, +/-1.5 m warp, 1 m generalisation)",
                 fontsize=11)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def short_name(line: Line) -> str:
    t = line.tags
    name = t.get("name:en") or t.get("name") or (f"ref {t['ref']}" if "ref" in t else t.get("highway", "?"))
    return f"{name} (L{line.level:+d})"


def roads_used(result: PathResult | None) -> list[str]:
    """Consecutive distinct road names (with level) along a result path."""
    out: list[str] = []
    for line in result.lines if result else []:
        name = short_name(line)
        if not out or out[-1] != name:
            out.append(name)
    return out
