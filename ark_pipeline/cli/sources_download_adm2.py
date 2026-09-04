"""Acquire the pinned global geoBoundaries gbOpen ADM2 snapshot, resumably."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import tomllib
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ark_pipeline.cli.sources_acquire import atomic_json, sha256

INVENTORY = Path(__file__).resolve().parents[2] / "config/geoboundaries_adm2.toml"


def validate_country(path: Path, country: dict, *, expected_units: int | None = None) -> dict:
    collection = json.loads(path.read_text())
    if collection.get("type") != "FeatureCollection":
        raise ValueError(f"{country['iso']}: expected a GeoJSON FeatureCollection")
    features = collection["features"]
    expected_units = country["units"] if expected_units is None else expected_units
    if len(features) != expected_units:
        raise ValueError(f"{country['iso']}: expected {expected_units} areas, found {len(features)}")
    identifiers = set()
    for feature in features:
        properties = feature["properties"]
        if properties.get("shapeGroup") != country["iso"] or properties.get("shapeType") != "ADM2":
            raise ValueError(f"{country['iso']}: wrong country or administrative level")
        identifier = properties.get("shapeID")
        if not identifier or identifier in identifiers:
            raise ValueError(f"{country['iso']}: missing or duplicate shape ID")
        identifiers.add(identifier)
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in {"Polygon", "MultiPolygon"} or not geometry.get("coordinates"):
            raise ValueError(f"{country['iso']}: missing polygon for {identifier}")
    return {"bytes": path.stat().st_size, "sha256": sha256(path), "units": len(features)}


def fetch(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Ark-IV-boundaries/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def audit_count(temporary: Path, country: dict, directory: Path) -> dict:
    """Resolve stale API counts against the pinned full file's exact ID set."""
    reference = directory / f"{country['iso']}.full.geojson"
    if not reference.is_file():
        pending = reference.with_suffix(".download")
        fetch(country["full_geometry_url"], pending)
        os.replace(pending, reference)
    full = json.loads(reference.read_text())
    full_features = full["features"]
    reference_record = validate_country(reference, country, expected_units=len(full_features))
    simplified = json.loads(temporary.read_text())
    validate_country(temporary, country, expected_units=len(simplified["features"]))
    full_ids = {feature["properties"]["shapeID"] for feature in full_features}
    simplified_ids = {feature["properties"]["shapeID"] for feature in simplified["features"]}
    incompatible = not simplified_ids <= full_ids
    missing = full_ids - simplified_ids
    if incompatible:
        # A simplified file with different identifiers is not combined with the
        # canonical file. Use the independently validated full dataset intact.
        temporary.write_bytes(reference.read_bytes())
    elif missing:
        simplified["features"].extend(feature for feature in full_features if feature["properties"]["shapeID"] in missing)
        temporary.write_text(json.dumps(simplified, ensure_ascii=False, separators=(",", ":")))
    return {"full_url": country["full_geometry_url"], "full_sha256": reference_record["sha256"],
            "full_units": len(full_ids), "provider_reported_units": country["units"],
            "restored_ids": sorted(missing), "selected_full_dataset": incompatible}


def acquire_country(country: dict, directory: Path) -> dict:
    target = directory / f"{country['iso']}.geojson"
    receipt = target.with_suffix(".receipt.json")
    try:
        saved = json.loads(receipt.read_text())
        if saved["country"] == country and target.stat().st_size == saved["bytes"] and sha256(target) == saved["sha256"] and (not saved.get("full_geometry_fallback") or saved.get("count_audit")):
            return {**saved, "reused": True}
    except (OSError, ValueError, KeyError):
        pass
    temporary = target.with_suffix(".download")
    for attempt in range(4):
        try:
            fetch(country["download_url"], temporary)
            audit = None
            try:
                validated = validate_country(temporary, country)
            except ValueError:
                audit = audit_count(temporary, country, directory)
                validated = validate_country(temporary, country, expected_units=audit["full_units"])
                print(f"{country['iso']}: verified {validated['units']} IDs against full source; API reports {country['units']}; restored {len(audit['restored_ids'])}", flush=True)
            record = {"country": country, "download_url": country["download_url"],
                      "count_audit": audit,
                      "full_geometry_fallback": bool(audit and (audit["restored_ids"] or audit["selected_full_dataset"])),
                      **validated}
            os.replace(temporary, target)
            atomic_json(receipt, record)
            return {**record, "reused": False}
        except (OSError, ValueError):
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def seed_cache(previous: Path, countries: list[dict], directory: Path) -> None:
    """Reuse unchanged countries across source releases without downloading again."""
    with zipfile.ZipFile(previous) as archive:
        records = json.loads(archive.read("metadata.json"))["countries"]
        for country in countries:
            record = records.get(country["iso"])
            if not record or record["country"] != country:
                continue
            raw = archive.read(f"{country['iso']}.geojson")
            if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                continue
            target = directory / f"{country['iso']}.geojson"
            if target.is_file():
                continue
            target.write_bytes(raw)
            audit = record.get("count_audit") or {}
            record["full_geometry_fallback"] = bool(record.get("full_geometry_fallback") or audit.get("selected_full_dataset"))
            atomic_json(target.with_suffix(".receipt.json"), record)


def download(output: Path, inventory_path: Path = INVENTORY, workers: int = 4, previous: Path | None = None) -> dict:
    if not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    inventory = tomllib.loads(inventory_path.read_text())
    countries = inventory["countries"]
    if len({country["iso"] for country in countries}) != len(countries):
        raise ValueError("Duplicate country in ADM2 inventory")
    directory = output.parent / "adm2-downloads" / inventory["release"]
    directory.mkdir(parents=True, exist_ok=True)
    if previous and previous.is_file():
        seed_cache(previous, countries, directory)
    records = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(acquire_country, country, directory): country for country in countries}
        for future in as_completed(jobs):
            country = jobs[future]
            record = future.result()
            records[country["iso"]] = record
            print(f"ADM2 {len(records)}/{len(countries)}: {country['iso']} ({record['units']:,} areas; {'cached' if record['reused'] else 'downloaded'})", flush=True)
    metadata = {"schema_version": 1, "release": inventory["release"],
                "inventory_sha256": sha256(inventory_path), "geometry": inventory["geometry"],
                "full_geometry_fallbacks": sorted(iso for iso, record in records.items() if record.get("full_geometry_fallback")),
                "source": inventory["official_url"], "countries": dict(sorted(records.items())),
                "units": sum(record["units"] for record in records.values())}
    temporary = output.with_suffix(".tmp.zip")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        for iso in sorted(records):
            archive.write(directory / f"{iso}.geojson", f"{iso}.geojson")
    os.replace(temporary, output)
    atomic_json(output.with_suffix(".metadata.json"), metadata)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--inventory", type=Path, default=INVENTORY)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    metadata = download(args.output, args.inventory, args.workers, args.previous)
    print(f"Verified {len(metadata['countries'])} countries and {metadata['units']:,} ADM2 areas")


if __name__ == "__main__":
    main()
