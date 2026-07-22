#!/usr/bin/env python3
"""Build serving artifacts with DuckDB, then leave DuckDB off the request path.

Outputs:
  * Parquet tables consumed by ``backend/load_postgres.py``
  * one Tippecanoe-built PMTiles archive consumed directly by deck.gl
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
import h3

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import get_settings


SYSTEMS = ("all", "Terrestrial", "Freshwater", "Marine")
DEFAULT_WEIGHTS = {
    "cr": 4.0, "en": 3.0, "vu": 2.0, "nt": 1.0, "dd": 2.0, "lc": 0.1,
    "sp": 2.0, "gen": 3.0, "fam": 4.0,
}


def materialize_aggregates(connection: duckdb.DuckDBPyConnection) -> None:
    """Rebuild the build-only aggregate database from Ark-IV.duckdb."""
    for table in ("H3Res3Species", "H3Res7Species", "SpecInfo", "SpecSystems", "H3Centroids"):
        connection.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM source.{table}")

    for resolution in (3, 7):
        for system in SYSTEMS:
            table_name = f'h3_res{resolution}_agg_{system}'
            system_where = ""
            geography_filter = ""
            if system != "all":
                system_where = (
                    "WHERE EXISTS (SELECT 1 FROM SpecSystems ss "
                    f"WHERE ss.gbif_accepted_id = cells.gbif_id AND ss.system = '{system}')"
                )
            if system in {"Terrestrial", "Freshwater"}:
                geography_filter = "AND centroids.is_land = true"
            elif system == "Marine":
                geography_filter = "AND centroids.is_sea = true"

            connection.execute(
                f"""
                CREATE OR REPLACE TABLE {table_name} AS
                WITH cells AS (
                    SELECT hs.h3_index, ids.gbif_id, centroids.latitude, centroids.longitude
                    FROM H3Res{resolution}Species hs,
                    UNNEST(hs.gbif_ids) AS ids(gbif_id)
                    JOIN H3Centroids centroids ON centroids.h3_index = hs.h3_index
                    WHERE true {geography_filter}
                )
                SELECT cells.h3_index, cells.latitude, cells.longitude,
                    COUNT(*)::BIGINT AS total_species,
                    COUNT(*) FILTER (WHERE species.redlist_category = 'Critically Endangered')::BIGINT AS crit_endangered_count,
                    COUNT(*) FILTER (WHERE species.redlist_category = 'Endangered')::BIGINT AS endangered_count,
                    COUNT(*) FILTER (WHERE species.redlist_category = 'Vulnerable')::BIGINT AS vulnerable_count,
                    COUNT(*) FILTER (WHERE species.redlist_category = 'Near Threatened')::BIGINT AS near_threatened_count,
                    COUNT(*) FILTER (WHERE species.redlist_category = 'Data Deficient')::BIGINT AS data_deficient_count,
                    COUNT(*) FILTER (WHERE species.redlist_category = 'Least Concern')::BIGINT AS least_concern_count,
                    COUNT(*) FILTER (WHERE species.has_dna_species_level = false)::BIGINT AS missing_species_dna,
                    COUNT(*) FILTER (WHERE species.genus_has_dna = false)::BIGINT AS missing_genus_dna,
                    COUNT(*) FILTER (WHERE species.family_has_dna = false)::BIGINT AS missing_family_dna
                FROM cells
                JOIN SpecInfo species ON species.gbif_accepted_id = cells.gbif_id
                {system_where}
                GROUP BY cells.h3_index, cells.latitude, cells.longitude
                """
            )


def export_parquet(connection: duckdb.DuckDBPyConnection, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    exports = {
        "species.parquet": """
            SELECT gbif_accepted_id, species_name, family, redlist_category,
                has_dna_species_level, genus_has_dna, family_has_dna,
                edge_group_name, meets_ebp
            FROM SpecInfo ORDER BY gbif_accepted_id
        """,
        "species_systems.parquet": """
            SELECT DISTINCT gbif_accepted_id, system FROM SpecSystems
            WHERE system IN ('Terrestrial', 'Freshwater', 'Marine')
            ORDER BY gbif_accepted_id, system
        """,
        "cell_species.parquet": """
            SELECT CAST(CAST('0x' || hs.h3_index AS UBIGINT) AS BIGINT) AS h3_index,
                   resolution, ids.gbif_id AS gbif_accepted_id
            FROM (
                SELECT h3_index, gbif_ids, 3::SMALLINT AS resolution FROM H3Res3Species
                UNION ALL
                SELECT h3_index, gbif_ids, 7::SMALLINT AS resolution FROM H3Res7Species
            ) hs, UNNEST(hs.gbif_ids) AS ids(gbif_id)
            ORDER BY resolution, h3_index, gbif_accepted_id
        """,
    }
    for filename, query in exports.items():
        target = output_dir / filename
        connection.execute(
            f"COPY ({query}) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [str(target)],
        )
        print(f"Exported {target}")


def export_map_metadata(connection: duckdb.DuckDBPyConnection, target: Path) -> None:
    """Export one fixed score scale per ecosystem across both map resolutions."""
    columns = {
        "cr": "crit_endangered_count", "en": "endangered_count",
        "vu": "vulnerable_count", "nt": "near_threatened_count",
        "dd": "data_deficient_count", "lc": "least_concern_count",
        "sp": "missing_species_dna", "gen": "missing_genus_dna",
        "fam": "missing_family_dna",
    }
    score = " + ".join(f"{columns[key]} * {weight}" for key, weight in DEFAULT_WEIGHTS.items())
    domains: dict[str, dict[str, float]] = {}
    for system in SYSTEMS:
        aggregates = " UNION ALL ".join(
            f"SELECT * FROM h3_res{resolution}_agg_{system}"
            for resolution in (3, 7)
        )
        maximum = float(connection.execute(f"SELECT MAX({score}) FROM ({aggregates})").fetchone()[0])
        domains[system.lower()] = {"min": 0.0, "max": maximum}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": 2,
        "score_domains": domains,
        "reference_weights": DEFAULT_WEIGHTS,
    }, separators=(",", ":")) + "\n")
    print(f"Exported {target}")


def feature(row: tuple, resolution: int, system: str) -> dict:
    h3_index = row[0]
    boundary = h3.cell_to_boundary(h3_index)
    ring = [[longitude, latitude] for latitude, longitude in boundary]
    ring.append(ring[0])
    return {
        "type": "Feature",
        "tippecanoe": {"minzoom": 0 if resolution == 3 else 6, "maxzoom": 7 if resolution == 3 else 12},
        "properties": {
            "h3_index": h3_index,
            "resolution": resolution,
            "system": system,
            "total": row[3],
            "cr": row[4], "en": row[5], "vu": row[6],
            "nt": row[7], "dd": row[8], "lc": row[9],
            "ms": row[10], "mg": row[11], "mf": row[12],
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def build_pmtiles(connection: duckdb.DuckDBPyConnection, target: Path, tippecanoe: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ark-iv-tiles-") as temp:
        inputs: list[str] = []
        for resolution in (3, 7):
            for system in SYSTEMS:
                layer = f"res{resolution}_{system.lower()}"
                geojson = Path(temp) / f"{layer}.geojsonseq"
                rows = connection.execute(f"SELECT * FROM h3_res{resolution}_agg_{system}").fetchall()
                with geojson.open("w") as stream:
                    for row in rows:
                        stream.write(json.dumps(feature(row, resolution, system), separators=(",", ":")) + "\n")
                inputs.extend(["-L", f"{layer}:{geojson}"])

        subprocess.run(
            [
                tippecanoe,
                "--force",
                "--output", str(target),
                "--minimum-zoom", "0",
                "--maximum-zoom", "12",
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--preserve-input-order",
                "--generate-ids",
                *inputs,
            ],
            check=True,
        )
    print(f"Built {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-aggregates", action="store_true")
    parser.add_argument("--skip-tiles", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    settings.tile_dir.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(settings.build_duckdb_path))
    try:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if args.rebuild_aggregates or "h3_res7_agg_all" not in tables:
            connection.execute(f"ATTACH ? AS source (READ_ONLY)", [str(settings.source_duckdb_path)])
            materialize_aggregates(connection)
        export_parquet(connection, settings.export_dir)
        export_map_metadata(connection, settings.map_metadata_path)
        if not args.skip_tiles:
            build_pmtiles(connection, settings.pmtiles_path, settings.tippecanoe_bin)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
