"""Install a complete global ADM2 generation from the registered source snapshot."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

import shapely
from shapely.geometry import mapping, shape

from ark_pipeline.cli.sources_acquire import load_manifest, resolve_stored_path
from ark_pipeline.runtime.progress import emit, tracked_stage
from ark_pipeline.runtime.provenance import atomic_json, code_fingerprint, identity_digest, sha256
from ark_pipeline.spatial.paths import ADM2_ROOT, ROOT

# The bundled Natural Earth catalogue uses these codes instead of the source ISO codes.
COUNTRY_PARENT_ALIASES = {"PSE": "PSX", "SSD": "SDS", "XKX": "KOS"}


def collection(features, **extra):
    return {"type": "FeatureCollection", "framework": "municipality", "features": features, **extra}


def normalize(features: list[dict], country: dict) -> tuple[list[dict], int]:
    normalized = []
    repairs = 0
    identifiers = set()
    for feature in features:
        properties = feature["properties"]
        if properties.get("shapeGroup") != country["iso"] or properties.get("shapeType") != "ADM2":
            raise ValueError(f"Wrong country or level in {country['iso']}")
        code = properties.get("shapeID")
        if not code or code in identifiers:
            raise ValueError(f"Missing or duplicate ADM2 identifier in {country['iso']}")
        identifiers.add(code)
        geometry = shape(feature["geometry"])
        if not geometry.is_valid:
            geometry = shapely.make_valid(geometry)
            if geometry.geom_type == "GeometryCollection":
                geometry = shapely.union_all([part for part in geometry.geoms if part.geom_type in {"Polygon", "MultiPolygon"}])
            repairs += 1
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError(f"No usable polygon for {country['iso']}/{code}")
        normalized.append({"type": "Feature", "properties": {
            "code": code, "name": properties.get("shapeName") or code,
            "parent_code": country.get("parent_code", country["iso"]), "boundary_type": "ADM2",
            "area_type": country["area_type"], "source_year": country["year"],
        }, "geometry": mapping(geometry)})
    if len(normalized) != country["units"]:
        raise ValueError(f"ADM2 area count changed for {country['iso']}")
    return sorted(normalized, key=lambda feature: (feature["properties"]["name"], feature["properties"]["code"])), repairs


def source_archive(root: Path) -> tuple[Path, dict]:
    source = load_manifest(root).get("sources", {}).get("geoboundaries-adm2")
    if not source or source.get("validation_status") != "passed":
        raise ValueError("Global ADM2 source is not registered; run just data-boundaries")
    records = {item["logical_name"]: item for item in source["files"]}
    record = records["geoboundaries-adm2.zip"]
    path = resolve_stored_path(root, record["path"])
    if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
        raise ValueError("Global ADM2 source checksum changed")
    return path, record


def valid_generation(directory: Path, identity: dict) -> bool:
    try:
        receipt = json.loads((directory / "receipt.json").read_text())
        if receipt["identity"] != identity or receipt["status"] != "passed":
            return False
        expected = set(receipt["outputs"])
        actual = {str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file() and path.name != "receipt.json"}
        if actual != expected:
            return False
        return all((directory / name).stat().st_size == record["bytes"] and sha256(directory / name) == record["sha256"]
                   for name, record in receipt["outputs"].items())
    except (OSError, ValueError, KeyError):
        return False


def install(output: Path, directory: Path, static_data: Path) -> None:
    asset = static_data / "adm2-catalogs"
    if asset.exists() and not asset.is_symlink():
        raise ValueError(f"Refusing to replace an unmanaged catalogue directory: {asset}")
    link = output / "current.tmp"
    link.unlink(missing_ok=True)
    link.symlink_to(directory.relative_to(output), target_is_directory=True)
    os.replace(link, output / "current")
    # Only geometry-free catalogues enter frontend assets; runtime polygons stay local.
    temporary = asset.with_name(asset.name + ".tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(os.path.relpath(output / "current/catalogues", static_data), target_is_directory=True)
    os.replace(temporary, asset)


def build(root: Path, output: Path = ADM2_ROOT, static_data: Path = ROOT / "app/static/data") -> dict:
    output = output.resolve()
    static_data = static_data.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another global ADM2 build is active") from exc
        archive_path, record = source_archive(root)
        admin0 = static_data / "boundary-catalogs/admin0.json"
        identity = {"source": record["sha256"], "code": code_fingerprint([Path(__file__)]),
                    "shapely": shapely.__version__, "admin0": sha256(admin0)}
        directory = output / "generations" / identity_digest(identity)
        current = output / "current"
        if current.is_symlink() and valid_generation(current.resolve(), identity):
            install(output, current.resolve(), static_data)
            return {**json.loads((current / "coverage.json").read_text()), "reused": True}
        if valid_generation(directory, identity):
            install(output, directory, static_data)
            return {**json.loads((directory / "coverage.json").read_text()), "reused": True}
        directory.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="building-", dir=directory.parent))
        try:
            report = build_generation(archive_path, staging, admin0)
            records = {str(path.relative_to(staging)): {"bytes": path.stat().st_size, "sha256": sha256(path)}
                       for path in staging.rglob("*") if path.is_file()}
            atomic_json(staging / "receipt.json", {"status": "passed", "identity": identity, "outputs": records})
            # Keep a damaged or older published generation available for inspection.
            if directory.exists():
                directory = directory.with_name(directory.name + "-" + staging.name)
            os.replace(staging, directory)
            install(output, directory, static_data)
            return {**report, "reused": False, "geometry_path": str(output / "current/municipality.geojson")}
        except BaseException:
            # A failed unpublished generation is retained for diagnostics.
            raise


def build_generation(archive_path: Path, output: Path, admin0: Path) -> dict:
    catalogues = output / "catalogues"
    catalogues.mkdir()
    countries = {}
    all_records = []
    identifiers = set()
    repairs = 0
    known_countries = {feature["code"] for feature in json.loads(admin0.read_text())["features"]}
    available_parents = set()
    with zipfile.ZipFile(archive_path) as archive, (output / "municipality.geojson").open("w") as merged:
        metadata = json.loads(archive.read("metadata.json"))
        expected = {"metadata.json"} | {f"{iso}.geojson" for iso in metadata["countries"]}
        if set(archive.namelist()) != expected:
            raise ValueError("Unexpected or missing ADM2 archive members")
        merged.write('{"type":"FeatureCollection","framework":"municipality","features":[')
        first = True
        for position, (iso, record) in enumerate(sorted(metadata["countries"].items())):
            emit("work", phase=f"ADM2 · {iso}", completed=position, total=len(metadata["countries"]),
                 unit="countries", force=True)
            raw = archive.read(f"{iso}.geojson")
            if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                raise ValueError(f"ADM2 archive checksum mismatch for {iso}")
            country = record["country"]
            parent = iso if iso in known_countries else COUNTRY_PARENT_ALIASES.get(iso)
            if parent not in known_countries or parent in available_parents:
                raise ValueError(f"Missing or ambiguous country catalogue mapping for {iso}")
            available_parents.add(parent)
            features, repaired = normalize(json.loads(raw)["features"], {
                **country, "units": record["units"], "parent_code": parent,
            })
            repairs += repaired
            records = []
            for feature in features:
                emit("work", task="adm2-areas", phase=f"ADM2 · {iso}", completed=len(all_records) + len(records),
                     total=metadata["units"], overall=True, unit="areas")
                code = feature["properties"]["code"]
                if code in identifiers:
                    raise ValueError(f"Duplicate global ADM2 identifier: {code}")
                identifiers.add(code)
                if not first:
                    merged.write(",")
                merged.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")))
                first = False
                records.append(feature["properties"])
            all_records.extend(records)
            countries[iso] = {**country, "parent_code": parent, "units": len(records), "repaired_geometries": repaired,
                              "full_geometry_fallback": record.get("full_geometry_fallback", False),
                              "count_audit": record.get("count_audit")}
            atomic_json(catalogues / f"{parent.lower()}.json", collection(records,
                        source="https://www.geoboundaries.org/", source_version=country["boundary_id"],
                        license=country["source_license"], coverage_status="available"))
        merged.write("]}\n")
    missing = sorted(known_countries - available_parents)
    for iso in missing:
        atomic_json(catalogues / f"{iso.lower()}.json", collection([], coverage_status="unavailable",
                    coverage_note="No ADM2 dataset in this geoBoundaries release."))
    all_records.sort(key=lambda record: (record["name"], record["parent_code"], record["code"]))
    atomic_json(catalogues / "all.json", collection(all_records, source="https://www.geoboundaries.org/",
                source_version=metadata["release"], geometry=metadata["geometry"]))
    report = {"status": "passed", "source_release": metadata["release"], "geometry": metadata["geometry"],
              "countries": countries, "country_count": len(countries), "area_count": len(identifiers),
              "unavailable_country_codes": missing, "repaired_geometries": repairs}
    atomic_json(output / "coverage.json", report)
    atomic_json(catalogues / "framework.json", {
        "id": "municipality", "name": "Municipalities & local areas", "short_name": "Local areas",
        "description": "Global geoBoundaries ADM2 areas: municipalities, counties and districts, depending on the country.",
        "group": "Political", "relationship": "single", "status": "ready", "tile_property": "mun",
        "parent_framework": "admin0", "source_url": "https://www.geoboundaries.org/api.html",
        "license": "geoBoundaries gbOpen; source licences retained in country catalogues",
        "catalog_url": "/data/adm2-catalogs/all.json", "catalog_partition_url": "/data/adm2-catalogs/{parent}.json",
        "available_parent_codes": sorted(available_parents), "color": [117, 112, 104],
        "coverage_note": f"{len(identifiers):,} ADM2 areas in {len(countries)} countries. Countries without source coverage are marked unavailable.",
    })
    return report


@tracked_stage("boundaries")
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=ADM2_ROOT)
    parser.add_argument("--static-data", type=Path, default=ROOT / "app/static/data")
    args = parser.parse_args()
    report = build(args.root, args.output_root, args.static_data)
    print(json.dumps({key: value for key, value in report.items() if key != "countries"}, indent=2))


if __name__ == "__main__":
    main()
