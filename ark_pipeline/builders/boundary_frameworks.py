#!/usr/bin/env python3
"""Build lightweight catalogues and normalized framework geometries.

The browser never needs to parse every polygon just to populate a selector.
This builder writes a geometry-free catalogue plus optional geometry partitions
for offline H3 membership assignment and compact browser catalogues.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import geopandas
import pandas

ROOT = Path(__file__).resolve().parents[2]
STATIC_DATA = ROOT / "app" / "static" / "data"


def slug(value: object) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    return result or "other"


def feature_collection(
    frame: geopandas.GeoDataFrame,
    *,
    framework: str,
    source: str,
    source_version: str,
    license_name: str,
) -> dict[str, Any]:
    collection = json.loads(frame.to_json(drop_id=True))
    collection.update({
        "framework": framework,
        "source": source,
        "source_version": source_version,
        "license": license_name,
        "assignment": "H3 cell polygon intersection",
    })
    return collection


def write_json(target: Path, value: object) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"Exported {target}")


def write_catalogue(
    frame: geopandas.GeoDataFrame,
    target: Path,
    *,
    framework: str,
    source: str,
    source_version: str,
    license_name: str,
    geometry_urls: dict[str, str] | None = None,
) -> None:
    records = []
    for properties in frame.drop(columns="geometry").to_dict("records"):
        properties = {
            key: (None if pandas.isna(value) else value)
            for key, value in properties.items()
        }
        if geometry_urls is not None:
            properties["geometry_url"] = geometry_urls[str(properties["code"])]
        records.append(properties)
    write_catalogue_records(
        records,
        target,
        framework=framework,
        source=source,
        source_version=source_version,
        license_name=license_name,
    )


def write_catalogue_records(
    records: list[dict[str, Any]],
    target: Path,
    *,
    framework: str,
    source: str,
    source_version: str,
    license_name: str,
) -> None:
    write_json(target, {
        "version": 1,
        "framework": framework,
        "source": source,
        "source_version": source_version,
        "license": license_name,
        "features": records,
    })


def write_catalogue_partitions(
    frame: geopandas.GeoDataFrame,
    partition_field: str,
    partition_dir: Path,
    *,
    framework: str,
    source: str,
    source_version: str,
    license_name: str,
    geometry_urls: dict[str, str] | None = None,
) -> None:
    partition_dir.mkdir(parents=True, exist_ok=True)
    for existing in partition_dir.glob("*.json"):
        existing.unlink()
    for partition_value, partition in frame.groupby(partition_field):
        target = partition_dir / f"{slug(partition_value)}.json"
        write_catalogue(
            partition,
            target,
            framework=framework,
            source=source,
            source_version=source_version,
            license_name=license_name,
            geometry_urls=geometry_urls,
        )


def build_catalogue(
    source: Path,
    target: Path,
    *,
    partition_field: str | None = None,
    partition_dir: Path | None = None,
    partition_url_prefix: str | None = None,
    catalogue_partition_dir: Path | None = None,
) -> None:
    collection = json.loads(source.read_text())
    features = collection.get("features", [])
    records = [dict(feature["properties"]) for feature in features]
    if partition_field:
        if partition_dir is None or partition_url_prefix is None:
            raise ValueError("Partition directory and URL prefix are required")
        if catalogue_partition_dir is not None:
            catalogue_partition_dir.mkdir(parents=True, exist_ok=True)
            for existing in catalogue_partition_dir.glob("*.json"):
                existing.unlink()
        grouped: dict[str, list[dict[str, Any]]] = {}
        grouped_records: dict[str, list[dict[str, Any]]] = {}
        for feature, properties in zip(features, records, strict=True):
            partition = slug(properties.get(partition_field))
            grouped.setdefault(partition, []).append(feature)
            properties["geometry_url"] = f"{partition_url_prefix.rstrip('/')}/{partition}.geojson"
            grouped_records.setdefault(partition, []).append(properties)
        for partition, partition_features in grouped.items():
            write_json(partition_dir / f"{partition}.geojson", {
                **{key: value for key, value in collection.items() if key != "features"},
                "features": partition_features,
            })
            if catalogue_partition_dir is not None:
                write_catalogue_records(
                    grouped_records[partition],
                    catalogue_partition_dir / f"{partition}.json",
                    framework=str(collection.get("framework", "")),
                    source=str(collection.get("source", "")),
                    source_version=str(collection.get("source_version", "")),
                    license_name=str(collection.get("license", "Public domain")),
                )
    write_json(target, {
        "version": 1,
        "framework": collection.get("framework"),
        "source": collection.get("source"),
        "source_version": collection.get("source_version"),
        "license": collection.get("license", "Public domain"),
        "features": records,
    })


def normalize_municipalities(paths: list[Path]) -> geopandas.GeoDataFrame:
    frames = []
    for path in paths:
        frame = geopandas.read_file(path).to_crs(4326)
        required = {"shapeID", "shapeName", "shapeGroup", "shapeType"}
        if not required <= set(frame.columns):
            raise ValueError(f"{path} is not a geoBoundaries ADM2 GeoJSON")
        frame = frame[[*sorted(required), "geometry"]].rename(columns={
            "shapeID": "code",
            "shapeName": "name",
            "shapeGroup": "parent_code",
            "shapeType": "boundary_type",
        })
        frames.append(frame)
    combined = pandas.concat(frames, ignore_index=True)
    result = geopandas.GeoDataFrame(combined, geometry="geometry", crs=4326)
    result["code"] = result["code"].astype(str)
    result["name"] = result["name"].fillna(result["code"]).astype(str)
    if result["code"].duplicated().any():
        raise ValueError("Municipality codes must be unique")
    return result.sort_values(["parent_code", "name", "code"]).reset_index(drop=True)


def build_municipalities(
    paths: list[Path], output: Path, catalogue: Path, partition_dir: Path,
    catalogue_partition_dir: Path,
) -> None:
    frame = normalize_municipalities(paths)
    source = "https://www.geoboundaries.org/"
    version = "current gbOpen"
    license_name = "CC BY 4.0"
    write_json(output, feature_collection(
        frame,
        framework="municipality",
        source=source,
        source_version=version,
        license_name=license_name,
    ))
    geometry_urls: dict[str, str] = {}
    for parent_code, partition in frame.groupby("parent_code"):
        target = partition_dir / f"{str(parent_code).lower()}.geojson"
        write_json(target, feature_collection(
            partition,
            framework="municipality",
            source=source,
            source_version=version,
            license_name=license_name,
        ))
        url = f"/data/boundary-geometry/municipality/{str(parent_code).lower()}.geojson"
        geometry_urls.update({str(code): url for code in partition["code"]})
    write_catalogue(
        frame,
        catalogue,
        framework="municipality",
        source=source,
        source_version=version,
        license_name=license_name,
        geometry_urls=geometry_urls,
    )
    write_catalogue_partitions(
        frame,
        "parent_code",
        catalogue_partition_dir,
        framework="municipality",
        source=source,
        source_version=version,
        license_name=license_name,
        geometry_urls=geometry_urls,
    )


def read_ecoregions(source: Path) -> geopandas.GeoDataFrame:
    with tempfile.TemporaryDirectory() as temporary_directory:
        with zipfile.ZipFile(source) as archive:
            archive.extractall(temporary_directory)
        shapefiles = list(Path(temporary_directory).glob("*.shp"))
        if len(shapefiles) != 1:
            raise ValueError("Expected one ecoregions shapefile")
        frame = geopandas.read_file(shapefiles[0]).to_crs(4326)
    frame = frame[[
        "ECO_ID", "ECO_NAME", "BIOME_NAME", "REALM", "NNH_NAME", "LICENSE", "geometry"
    ]].rename(columns={
        "ECO_ID": "code",
        "ECO_NAME": "name",
        "BIOME_NAME": "biome",
        "REALM": "region",
        "NNH_NAME": "conservation_status",
        "LICENSE": "source_license",
    })
    frame["code"] = frame["code"].astype(int).map(lambda value: f"ECO-{value}")
    frame["boundary_type"] = "Terrestrial ecoregion"
    frame["geometry"] = frame.geometry.make_valid().simplify(0.03, preserve_topology=True)
    if frame["code"].duplicated().any() or frame.geometry.is_empty.any():
        raise ValueError("Ecoregion identifiers and geometries must be valid")
    return frame.sort_values(["region", "name", "code"]).reset_index(drop=True)


def build_ecoregions(source: Path, output: Path, catalogue: Path, partition_dir: Path) -> None:
    frame = read_ecoregions(source)
    source_url = "https://ecoregions.world/"
    version = "2017"
    license_name = "CC BY 4.0"
    partition_dir.mkdir(parents=True, exist_ok=True)
    for existing in partition_dir.glob("*.geojson"):
        existing.unlink()
    geometry_urls: dict[str, str] = {}
    for _, feature in frame.iterrows():
        code = str(feature["code"])
        partition_slug = slug(code)
        partition = geopandas.GeoDataFrame([feature], geometry="geometry", crs=frame.crs)
        target = partition_dir / f"{partition_slug}.geojson"
        write_json(target, feature_collection(
            partition,
            framework="conservation_framework",
            source=source_url,
            source_version=version,
            license_name=license_name,
        ))
        geometry_urls[code] = f"/data/boundary-geometry/conservation-framework/{partition_slug}.geojson"
    write_json(output, feature_collection(
        frame,
        framework="conservation_framework",
        source=source_url,
        source_version=version,
        license_name=license_name,
    ))
    write_catalogue(
        frame,
        catalogue,
        framework="conservation_framework",
        source=source_url,
        source_version=version,
        license_name=license_name,
        geometry_urls=geometry_urls,
    )


def read_eez(source: Path) -> geopandas.GeoDataFrame:
    """Normalize Marine Regions EEZs to ISO-3 country memberships.

    A maritime polygon is assigned to every territory and sovereign ISO code
    supplied by Marine Regions. This makes dependencies discoverable under
    their own country-selector entry while also including them in the parent
    sovereign's scope. Disputed polygons likewise remain available to every
    recorded claimant.
    """
    frame = geopandas.read_file(source).to_crs(4326)
    iso_fields = [
        field for field in (
            "iso_ter1", "iso_ter2", "iso_ter3",
            "iso_sov1", "iso_sov2", "iso_sov3",
        )
        if field in frame.columns
    ]
    if not {"iso_ter1", "iso_sov1"} <= set(iso_fields):
        raise ValueError("Expected a Marine Regions EEZ v12 layer with ISO fields")

    # Simplify each source polygon before claimant/sovereign expansion. Doing
    # this after dissolve creates a few enormous multipolygons and turns the
    # topology-preserving pass into a many-minute serial bottleneck.
    frame["geometry"] = frame.geometry.simplify(
        0.005, preserve_topology=False
    ).make_valid()

    records: list[dict[str, Any]] = []
    for _, feature in frame.iterrows():
        codes = {
            str(feature[field]).strip().upper()
            for field in iso_fields
            if pandas.notna(feature[field])
            and re.fullmatch(r"[A-Za-z]{3}", str(feature[field]).strip())
        }
        for code in sorted(codes):
            records.append({"code": code, "geometry": feature.geometry})
    expanded = geopandas.GeoDataFrame(records, geometry="geometry", crs=4326)
    dissolved = expanded.dissolve(by="code", as_index=False)
    dissolved["geometry"] = dissolved.geometry.make_valid()
    dissolved["name"] = dissolved["code"].map(lambda code: f"{code} maritime zone")
    dissolved["boundary_type"] = "Exclusive economic zone"
    if dissolved["code"].duplicated().any() or dissolved.geometry.is_empty.any():
        raise ValueError("EEZ country codes and geometries must be valid")
    return dissolved[["code", "name", "boundary_type", "geometry"]].sort_values("code")


def build_country_scope(
    admin0_source: Path,
    eez: geopandas.GeoDataFrame,
    output: Path,
) -> None:
    """Union each selectable land boundary with its matching maritime zone."""
    countries = geopandas.read_file(admin0_source).to_crs(4326)
    required = {"code", "name", "geometry"}
    if not required <= set(countries.columns):
        raise ValueError("Expected normalized Admin-0 boundaries")
    eez_by_code = eez.set_index("code").geometry.to_dict()
    countries["geometry"] = countries.apply(
        lambda row: row.geometry.union(eez_by_code[row["code"]])
        if row["code"] in eez_by_code else row.geometry,
        axis=1,
    )
    write_json(output, feature_collection(
        countries,
        framework="admin0",
        source="Natural Earth 10m Admin-0 + Marine Regions World EEZ v12",
        source_version="Natural Earth 5.1.2 / EEZ v12 (2023-10-25)",
        license_name="Public domain / CC BY 4.0",
    ))


def build_eez(
    source: Path,
    output: Path,
    catalogue: Path,
    country_scope: Path,
    admin0_source: Path,
) -> None:
    frame = read_eez(source)
    admin0 = geopandas.read_file(admin0_source)
    country_names = dict(zip(admin0["code"].astype(str), admin0["name"].astype(str)))
    frame["name"] = frame["code"].map(
        lambda code: f"{country_names.get(code, code)} EEZ"
    )
    source_url = "https://www.marineregions.org/downloads.php"
    version = "World EEZ v12 (2023-10-25)"
    license_name = "CC BY 4.0"
    write_json(output, feature_collection(
        frame,
        framework="eez",
        source=source_url,
        source_version=version,
        license_name=license_name,
    ))
    write_catalogue(
        frame,
        catalogue,
        framework="eez",
        source=source_url,
        source_version=version,
        license_name=license_name,
    )
    build_country_scope(admin0_source, frame, country_scope)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalogue_parser = subparsers.add_parser("catalogue")
    catalogue_parser.add_argument("source", type=Path)
    catalogue_parser.add_argument("--output", type=Path, required=True)
    catalogue_parser.add_argument("--partition-field")
    catalogue_parser.add_argument("--partition-dir", type=Path)
    catalogue_parser.add_argument("--partition-url-prefix")
    catalogue_parser.add_argument("--catalogue-partition-dir", type=Path)

    municipality_parser = subparsers.add_parser("municipalities")
    municipality_parser.add_argument("sources", nargs="+", type=Path)
    municipality_parser.add_argument(
        "--output", type=Path,
        default=STATIC_DATA / "boundaries" / "municipality.geojson",
    )
    municipality_parser.add_argument(
        "--catalogue", type=Path,
        default=STATIC_DATA / "boundary-catalogs" / "municipality.json",
    )
    municipality_parser.add_argument(
        "--partition-dir", type=Path,
        default=STATIC_DATA / "boundary-geometry" / "municipality",
    )
    municipality_parser.add_argument(
        "--catalogue-partition-dir", type=Path,
        default=STATIC_DATA / "boundary-catalogs" / "municipality",
    )

    ecoregion_parser = subparsers.add_parser("ecoregions")
    ecoregion_parser.add_argument("source", type=Path)
    ecoregion_parser.add_argument(
        "--output", type=Path,
        default=STATIC_DATA / "boundaries" / "conservation-framework.geojson",
    )
    ecoregion_parser.add_argument(
        "--catalogue", type=Path,
        default=STATIC_DATA / "boundary-catalogs" / "conservation-framework.json",
    )
    ecoregion_parser.add_argument(
        "--partition-dir", type=Path,
        default=STATIC_DATA / "boundary-geometry" / "conservation-framework",
    )

    eez_parser = subparsers.add_parser("eez")
    eez_parser.add_argument("source", type=Path)
    eez_parser.add_argument(
        "--output", type=Path,
        default=ROOT / "data" / "boundaries" / "eez.geojson",
    )
    eez_parser.add_argument(
        "--catalogue", type=Path,
        default=STATIC_DATA / "boundary-catalogs" / "eez.json",
    )
    eez_parser.add_argument(
        "--country-scope", type=Path,
        default=ROOT / "data" / "boundaries" / "country-scope.geojson",
    )
    eez_parser.add_argument(
        "--admin0-source", type=Path,
        default=STATIC_DATA / "boundaries" / "admin0.geojson",
    )

    arguments = parser.parse_args()
    if arguments.command == "catalogue":
        build_catalogue(
            arguments.source,
            arguments.output,
            partition_field=arguments.partition_field,
            partition_dir=arguments.partition_dir,
            partition_url_prefix=arguments.partition_url_prefix,
            catalogue_partition_dir=arguments.catalogue_partition_dir,
        )
    elif arguments.command == "municipalities":
        build_municipalities(
            arguments.sources,
            arguments.output,
            arguments.catalogue,
            arguments.partition_dir,
            arguments.catalogue_partition_dir,
        )
    elif arguments.command == "ecoregions":
        build_ecoregions(
            arguments.source, arguments.output, arguments.catalogue, arguments.partition_dir
        )
    else:
        build_eez(
            arguments.source,
            arguments.output,
            arguments.catalogue,
            arguments.country_scope,
            arguments.admin0_source,
        )


if __name__ == "__main__":
    main()
