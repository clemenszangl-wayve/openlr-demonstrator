"""Command line interface: ``openlr-demo [run|sweep|serve|crossmap|scenarios|fetch-data]``."""

from __future__ import annotations

import argparse
import dataclasses
import time
from pathlib import Path

from . import OUTPUT_DIR, osm_fetch
from .mapbox import MapboxClient, MapboxConfig
from .network import RoadNetwork
from .pipeline import default_methods, run_scenario
from .scenarios import SCENARIOS
from .sender_map import MapDifference, build_sender_map

DEFAULT_DIFFERENCE = MapDifference(shift_m=5.0, shift_bearing=150.0, warp_m=1.5, simplify_m=1.0)


def _load_network(path: Path | None) -> RoadNetwork:
    data = osm_fetch.load(path or osm_fetch.DEFAULT_OUTPUT)
    net = RoadNetwork.from_osm(data)
    meta = data["meta"]
    print(f"OSM network: {len(net.nodes)} nodes, {len(net.lines)} directed lines "
          f"({meta['extract']}, {meta.get('replication_timestamp') or meta['extracted_at']})")
    return net


def _mapbox(enabled: bool, radius: float | None) -> MapboxClient | None:
    config = MapboxConfig.from_env() if enabled else None
    if config is None:
        print("Mapbox: off" + ("" if not enabled else " (set MAPBOX_ACCESS_TOKEN in .env to compare with Mapbox)"))
        return None
    if radius is not None:
        config = dataclasses.replace(config, radius_m=radius)
    print(f"Mapbox: {config.profile}, radius {config.radius_m:g} m (responses cached in {config.cache_dir})")
    return MapboxClient(config)


def cmd_run(args) -> None:
    from . import report, viz

    receiver = _load_network(args.data)
    mapbox = _mapbox(not args.no_mapbox, args.mapbox_radius)
    diff = MapDifference(args.shift, args.bearing, args.warp, args.wavelength, args.simplify, args.seed)
    sender = build_sender_map(receiver, diff)
    keys = args.scenario or list(SCENARIOS)
    runs = []
    for key in keys:
        run = run_scenario(SCENARIOS[key], receiver, sender, diff, default_methods(mapbox),
                           not args.no_disambiguation, mapbox)
        runs.append(run)
        print(report.console(run, receiver))
        if not args.no_html:
            folder = args.out / key
            viz.folium_map(run, receiver, sender, folder / "map.html")
            viz.deck_3d(run, receiver, folder / "map3d.html")
            viz.level_profile_png(run, folder / "level_profile.png")
    sweep_png = args.out / "sweep.png"
    md = report.markdown(runs, args.out, receiver, sweep_png if sweep_png.exists() else None)
    print(f"\nReport: {md}")
    if not args.no_html:
        print(f"Maps:   {args.out}/<scenario>/map.html (2D) and map3d.html (3D)")


def cmd_sweep(args) -> None:
    from . import sweep, viz

    receiver = _load_network(args.data)
    mapbox = _mapbox(args.mapbox, args.mapbox_radius)
    t0 = time.perf_counter()
    print(f"Sweeping offsets {args.shifts} m in {args.directions} directions x {len(SCENARIOS)} scenarios ...")
    rows = sweep.run_sweep(receiver, args.shifts, args.directions, args.warp, args.simplify,
                           disambiguate=not args.no_disambiguation, mapbox=mapbox)
    csv = sweep.write_csv(rows, args.out / "sweep.csv")
    png = viz.sweep_png(rows, args.out / "sweep.png")
    print(f"done in {time.perf_counter() - t0:.0f} s -> {csv}, {png}")


def _valhalla():
    from .valhalla import ValhallaClient

    client = ValhallaClient.from_env()
    print(f"Valhalla: {client.url} (responses cached in {client.cache_dir})" if client else "Valhalla: off")
    return client


def cmd_serve(args) -> None:
    from . import webapp

    receiver = _load_network(args.data)
    mapbox = _mapbox(not args.no_mapbox, None)
    app = webapp.App(receiver, mapbox, _valhalla())
    webapp.serve(app, args.host, args.port, open_browser=not args.no_browser)


def cmd_crossmap(args) -> None:
    from . import crossmap

    receiver = _load_network(args.data)
    mapbox = _mapbox(True, None)
    if mapbox is None:
        raise SystemExit("crossmap needs Mapbox: set MAPBOX_ACCESS_TOKEN in .env")
    rows = crossmap.run_scenarios(receiver, mapbox, args.scenario or list(SCENARIOS), args.lrp_distance,
                                  not args.no_disambiguation)
    print(crossmap.console(rows))
    md = crossmap.write_markdown(rows, args.out / "crossmap.md", mapbox.config.profile)
    print(f"\nReport: {md}  ({mapbox.requests} Mapbox API requests, the rest from the cache)")


def cmd_scenarios(_args) -> None:
    for s in SCENARIOS.values():
        print(f"{s.key:18s} {s.title}\n{'':18s} {s.description}\n")


def cmd_fetch(args) -> None:
    osm_fetch.fetch(args.url, tuple(args.bbox), args.data or osm_fetch.DEFAULT_OUTPUT)


def main(argv=None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", type=Path, default=None, help="OSM extract (default: data/shibuya_osm.json.gz)")
    common.add_argument("--out", type=Path, default=OUTPUT_DIR, help="output directory (default: output/)")
    parser = argparse.ArgumentParser(prog="openlr-demo", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    d = DEFAULT_DIFFERENCE
    p_run = sub.add_parser("run", parents=[common], help="encode/decode/match all scenarios and write maps + report")
    p_run.add_argument("-s", "--scenario", action="append", choices=list(SCENARIOS), help="scenario key (repeatable)")
    p_run.add_argument("--shift", type=float, default=d.shift_m, help="systematic offset of map A [m]")
    p_run.add_argument("--bearing", type=float, default=d.shift_bearing, help="direction of the offset [deg]")
    p_run.add_argument("--warp", type=float, default=d.warp_m, help="amplitude of the smooth warp [m]")
    p_run.add_argument("--wavelength", type=float, default=d.warp_wavelength_m, help="warp wavelength [m]")
    p_run.add_argument("--simplify", type=float, default=d.simplify_m, help="Douglas-Peucker tolerance [m]")
    p_run.add_argument("--seed", type=int, default=d.seed, help="random seed of the warp")
    p_run.add_argument("--no-disambiguation", action="store_true",
                       help="disable the encoder's extra LRPs at ambiguous Y-splits")
    p_run.add_argument("--no-mapbox", action="store_true", help="skip the Mapbox comparison")
    p_run.add_argument("--mapbox-radius", type=float, default=None, help="Mapbox search radius per point [m]")
    p_run.add_argument("--no-html", action="store_true", help="console output only")
    p_run.set_defaults(func=cmd_run)

    p_sweep = sub.add_parser("sweep", parents=[common], help="success rate vs. map offset (all directions)")
    p_sweep.add_argument("--shifts", type=float, nargs="+", default=[0, 2, 4, 6, 8, 10, 12, 15, 20])
    p_sweep.add_argument("--directions", type=int, default=12)
    p_sweep.add_argument("--warp", type=float, default=d.warp_m)
    p_sweep.add_argument("--simplify", type=float, default=d.simplify_m)
    p_sweep.add_argument("--no-disambiguation", action="store_true")
    p_sweep.add_argument("--mapbox", action="store_true",
                         help="include Mapbox (one API request per run, 540 with the defaults; cached)")
    p_sweep.add_argument("--mapbox-radius", type=float, default=None)
    p_sweep.set_defaults(func=cmd_sweep)

    p_serve = sub.add_parser("serve", parents=[common], help="browser app: split view OSM -> OpenLR -> Mapbox")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p_serve.add_argument("--no-mapbox", action="store_true", help="hide the Mapbox comparison")
    p_serve.set_defaults(func=cmd_serve)

    p_cross = sub.add_parser("crossmap", parents=[common],
                             help="encode the scenarios on OSM, decode them on the Mapbox map, compare")
    p_cross.add_argument("-s", "--scenario", action="append", choices=list(SCENARIOS), help="scenario key (repeatable)")
    p_cross.add_argument("--lrp-distance", type=float, nargs="+", default=[15_000.0, 2_000.0],
                         help="maximum distance between LRPs [m], one run per value (default: 15000 2000)")
    p_cross.add_argument("--no-disambiguation", action="store_true")
    p_cross.set_defaults(func=cmd_crossmap)

    p_sc = sub.add_parser("scenarios", help="list the test routes")
    p_sc.set_defaults(func=cmd_scenarios)

    p_fetch = sub.add_parser("fetch-data", parents=[common], help="(re)build the OSM extract from a PBF download")
    p_fetch.add_argument("--url", default=osm_fetch.DEFAULT_PBF_URL)
    p_fetch.add_argument("--bbox", type=float, nargs=4, default=osm_fetch.DEFAULT_BBOX,
                         metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    p_fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["run"])
    args.func(args)


if __name__ == "__main__":
    main()
