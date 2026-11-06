"""Download an OSM extract and cut out the drivable road network of the study area.

The result is a small gzipped JSON file under ``data/`` that is committed to the repository,
so the demonstrator runs fully offline. Re-running this step is only necessary to refresh
the data or to change the study area.

Overpass is frequently rate limited / blocked in corporate networks, so the default source
is the BBBike "Tokyo" PBF extract (~90 MB), which is processed locally with pyosmium.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import shutil
import urllib.request
from pathlib import Path

from . import DATA_DIR

DEFAULT_PBF_URL = "https://download.bbbike.org/osm/bbbike/Tokyo/Tokyo.osm.pbf"

# Shibuya - Ikejiri-Ohashi - Sangenjaya - Tomigaya.
# Contains Shuto Expressway No.3 (elevated) above National Route 246 and the
# Shuto C2 Yamate Tunnel below Yamate-dori.
DEFAULT_BBOX = (139.655, 35.633, 139.712, 35.672)  # lon_min, lat_min, lon_max, lat_max
DEFAULT_OUTPUT = DATA_DIR / "shibuya_osm.json.gz"

CAR_HIGHWAYS = {
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "unclassified", "residential", "living_street",
}
KEEP_TAGS = {
    "highway", "name", "name:en", "ref", "oneway", "junction", "layer", "bridge",
    "tunnel", "toll", "lanes", "maxspeed", "access", "motor_vehicle", "motorcar",
}
_ALLOWED_ACCESS = {"yes", "designated", "permissive", "destination"}


def _is_drivable(tags: dict[str, str]) -> bool:
    if tags.get("highway") not in CAR_HIGHWAYS or tags.get("area") == "yes":
        return False
    for key in ("motorcar", "motor_vehicle"):
        if key in tags:
            return tags[key] in _ALLOWED_ACCESS
    return tags.get("access", "yes") in _ALLOWED_ACCESS


def download(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        print(f"Using cached {target}")
        return target
    print(f"Downloading {url} ...")
    tmp = target.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "openlr-demo/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.rename(target)
    return target


def extract(pbf: Path, bbox: tuple[float, float, float, float]) -> dict:
    """Return drivable ways clipped to `bbox` (runs of consecutive inside nodes)."""
    import osmium

    lon_min, lat_min, lon_max, lat_max = bbox
    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []

    processor = osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY).with_locations()
    for obj in processor:
        if not obj.is_way():
            continue
        tags = dict(obj.tags)
        if not _is_drivable(tags):
            continue
        runs: list[list[int]] = [[]]
        for ref in obj.nodes:
            loc = ref.location
            inside = loc.valid() and lon_min <= loc.lon <= lon_max and lat_min <= loc.lat <= lat_max
            if inside:
                nodes[ref.ref] = (round(loc.lon, 7), round(loc.lat, 7))
                runs[-1].append(ref.ref)
            elif runs[-1]:
                runs.append([])
        kept = {k: v for k, v in tags.items() if k in KEEP_TAGS}
        for part, run in enumerate(r for r in runs if len(r) >= 2):
            ways.append({"id": obj.id, "part": part, "nodes": run, "tags": kept})

    used = {n for w in ways for n in w["nodes"]}
    header = osmium.io.Reader(str(pbf), osmium.osm.NOTHING).header()
    return {
        "meta": {
            "source": "OpenStreetMap contributors (ODbL 1.0), https://www.openstreetmap.org/copyright",
            "extract": pbf.name,
            "replication_timestamp": header.get("osmosis_replication_timestamp", ""),
            "extracted_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "bbox": list(bbox),
        },
        "nodes": {str(n): nodes[n] for n in sorted(used)},
        "ways": ways,
    }


def fetch(url: str = DEFAULT_PBF_URL, bbox=DEFAULT_BBOX, output: Path = DEFAULT_OUTPUT) -> Path:
    pbf = download(url, DATA_DIR / "raw" / Path(url).name)
    print(f"Extracting drivable roads in bbox {bbox} ...")
    data = extract(pbf, bbox)
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(data['ways'])} ways / {len(data['nodes'])} nodes to {output}")
    return output


def load(path: Path = DEFAULT_OUTPUT) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run `openlr-demo fetch-data` first.")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)
