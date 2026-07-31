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
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

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
TILE_FEATURE_BATCH_SIZE = 10_000
MAX_EXPANDED_CELL_RELATIONSHIPS = 100_000_000
METRICS = {
    "total_species": "TRUE",
    "crit_endangered_count": "redlist_category = 'Critically Endangered'",
    "endangered_count": "redlist_category = 'Endangered'",
    "vulnerable_count": "redlist_category = 'Vulnerable'",
    "near_threatened_count": "redlist_category = 'Near Threatened'",
    "data_deficient_count": "redlist_category = 'Data Deficient'",
    "least_concern_count": "redlist_category = 'Least Concern'",
    "missing_species_dna": "has_dna_species_level = false",
    "missing_genus_dna": "genus_has_dna = false",
    "missing_family_dna": "family_has_dna = false",
}
SYSTEM_PREDICATES = {
    "all": "TRUE",
    "Terrestrial": "is_terrestrial",
    "Freshwater": "is_freshwater",
    "Marine": "is_marine",
}


def sql_path(path: Path) -> str:
    """Return a safely quoted SQL string literal for DuckDB COPY statements."""
    return "'" + str(path).replace("'", "''") + "'"


def materialize_aggregates(connection: duckdb.DuckDBPyConnection) -> None:
    """Rebuild aggregates one H3 base cell at a time.

    A global res-7 build contains tens of billions of cell/species
    relationships. Base-cell partitioning keeps each GROUP BY bounded while
    still expanding each H3 list only once per partition.
    """
    for table in ("SpecInfo", "SpecSystems"):
        connection.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM source.{table}")

    for resolution in (3, 7):
        input_table = f"H3Res{resolution}Species"
        connection.execute(
            f"""
            CREATE OR REPLACE TABLE {input_table} AS
            SELECT
                h3_index,
                gbif_ids,
                CAST(
                    (CAST('0x' || h3_index AS UBIGINT) >> 45) & 127
                    AS UTINYINT
                ) AS base_cell
            FROM source.{input_table}
            ORDER BY base_cell, h3_index
            """
        )

        for system in SYSTEMS:
            metric_schema = ", ".join(
                f"{metric} BIGINT" for metric in METRICS
            )
            connection.execute(
                f"""
                CREATE OR REPLACE TABLE h3_res{resolution}_agg_{system} (
                    h3_index VARCHAR,
                    {metric_schema}
                )
                """
            )

        wide_columns = []
        for system, system_predicate in SYSTEM_PREDICATES.items():
            suffix = system.lower()
            for metric, metric_predicate in METRICS.items():
                wide_columns.append(
                    "COUNT(*) FILTER (WHERE "
                    f"({system_predicate}) AND ({metric_predicate}))::BIGINT "
                    f'AS "{metric}__{suffix}"'
                )
        base_cells = [
            int(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT base_cell FROM {input_table} ORDER BY base_cell"
            ).fetchall()
        ]
        for base_cell in base_cells:
            connection.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE aggregate_partition AS
                WITH species AS (
                    SELECT
                        info.*,
                        coalesce(bool_or(systems.system = 'Terrestrial'), false)
                            AS is_terrestrial,
                        coalesce(bool_or(systems.system = 'Freshwater'), false)
                            AS is_freshwater,
                        coalesce(bool_or(systems.system = 'Marine'), false)
                            AS is_marine
                    FROM SpecInfo info
                    LEFT JOIN SpecSystems systems USING (gbif_accepted_id)
                    GROUP BY ALL
                ),
                cells AS (
                    SELECT hs.h3_index, ids.gbif_id
                    FROM {input_table} hs,
                    UNNEST(hs.gbif_ids) AS ids(gbif_id)
                    WHERE hs.base_cell = {base_cell}
                )
                SELECT cells.h3_index, {", ".join(wide_columns)}
                FROM cells
                JOIN species ON species.gbif_accepted_id = cells.gbif_id
                GROUP BY cells.h3_index
                """
            )
            for system in SYSTEMS:
                suffix = system.lower()
                projected = ", ".join(
                    f'"{metric}__{suffix}" AS {metric}' for metric in METRICS
                )
                connection.execute(
                    f"""
                    INSERT INTO h3_res{resolution}_agg_{system}
                    SELECT h3_index, {projected}
                    FROM aggregate_partition
                    WHERE "total_species__{suffix}" > 0
                    """
                )
            connection.execute("DROP TABLE aggregate_partition")

        for system in SYSTEMS:
            connection.execute(
                f"""
                CREATE UNIQUE INDEX h3_res{resolution}_agg_{system.lower()}_h3
                ON h3_res{resolution}_agg_{system}(h3_index)
                """
            )


def validate_materialized_data(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Verify that H3 relationships survive the species join and aggregation."""
    resolutions: dict[str, dict[str, int]] = {}
    failures: list[str] = []

    duplicate_species_ids = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT gbif_accepted_id
            FROM SpecInfo
            GROUP BY gbif_accepted_id
            HAVING count(*) > 1
        )
        """
    ).fetchone()[0]
    if duplicate_species_ids:
        failures.append(f"SpecInfo contains {duplicate_species_ids} duplicate species IDs")

    for resolution in (3, 7):
        table = f"H3Res{resolution}Species"
        aggregate = f"h3_res{resolution}_agg_all"
        row = connection.execute(
            f"""
            WITH source AS (
                SELECT h3_index, gbif_ids
                FROM {table}
            ),
            expanded AS (
                SELECT h3_index, ids.gbif_id AS gbif_accepted_id
                FROM source, UNNEST(gbif_ids) AS ids(gbif_id)
            ),
            matched AS (
                SELECT expanded.h3_index, expanded.gbif_accepted_id
                FROM expanded
                JOIN SpecInfo USING (gbif_accepted_id)
            )
            SELECT
                (SELECT count(*) FROM source) AS source_cells,
                (SELECT coalesce(sum(len(gbif_ids)), 0) FROM source) AS source_relationships,
                (SELECT count(*) FROM source WHERE gbif_ids IS NULL) AS null_lists,
                (SELECT count(*) FROM source WHERE list_unique(gbif_ids) <> len(gbif_ids)) AS cells_with_duplicate_ids,
                (SELECT count(*) FROM expanded WHERE gbif_accepted_id IS NULL) AS null_species_ids,
                (SELECT count(*) FROM matched) AS matched_relationships,
                (SELECT count(DISTINCT h3_index) FROM matched) AS matched_cells,
                (SELECT coalesce(sum(total_species), 0) FROM {aggregate}) AS aggregate_relationships,
                (SELECT count(*) FROM {aggregate}) AS aggregate_cells
            """
        ).fetchone()
        keys = (
            "source_cells", "source_relationships", "null_lists",
            "cells_with_duplicate_ids", "null_species_ids", "matched_relationships",
            "matched_cells", "aggregate_relationships", "aggregate_cells",
        )
        stats = dict(zip(keys, map(int, row), strict=True))
        stats["dropped_relationships"] = (
            stats["source_relationships"] - stats["matched_relationships"]
        )
        stats["dropped_cells"] = stats["source_cells"] - stats["matched_cells"]
        resolutions[str(resolution)] = stats

        if stats["null_lists"]:
            failures.append(f"resolution {resolution} has {stats['null_lists']} null species lists")
        if stats["cells_with_duplicate_ids"]:
            failures.append(
                f"resolution {resolution} has "
                f"{stats['cells_with_duplicate_ids']} cells with duplicate species IDs"
            )
        if stats["null_species_ids"]:
            failures.append(
                f"resolution {resolution} has {stats['null_species_ids']} null species IDs"
            )
        if stats["dropped_relationships"]:
            failures.append(
                f"resolution {resolution} dropped "
                f"{stats['dropped_relationships']} relationships at the SpecInfo join"
            )
        if stats["dropped_cells"]:
            failures.append(
                f"resolution {resolution} dropped {stats['dropped_cells']} cells at the SpecInfo join"
            )
        if stats["aggregate_relationships"] != stats["matched_relationships"]:
            failures.append(
                f"resolution {resolution} aggregate relationship count differs from matched input"
            )
        if stats["aggregate_cells"] != stats["matched_cells"]:
            failures.append(
                f"resolution {resolution} aggregate cell count differs from matched input"
            )

    return {
        "version": 1,
        "status": "ok" if not failures else "failed",
        "duplicate_species_ids": int(duplicate_species_ids),
        "resolutions": resolutions,
        "failures": failures,
    }


def write_validation_report(report: dict[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Exported {target}")


def require_valid(report: dict[str, Any]) -> None:
    if report["failures"]:
        details = "\n".join(f"  - {failure}" for failure in report["failures"])
        raise RuntimeError(f"Serving-data validation failed:\n{details}")


def export_parquet(
    connection: duckdb.DuckDBPyConnection,
    output_dir: Path,
    *,
    include_expanded_cell_species: bool = True,
) -> None:
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
        "cell_species_lists.parquet": """
            SELECT
                CAST(CAST('0x' || hs.h3_index AS UBIGINT) AS BIGINT) AS h3_index,
                resolution,
                hs.gbif_ids AS species_ids
            FROM (
                SELECT h3_index, gbif_ids, 3::SMALLINT AS resolution
                FROM H3Res3Species
                UNION ALL
                SELECT h3_index, gbif_ids, 7::SMALLINT AS resolution
                FROM H3Res7Species
            ) hs
            ORDER BY resolution, h3_index
        """,
    }
    if include_expanded_cell_species:
        relationships = int(connection.execute(
            """
            SELECT
                (SELECT coalesce(sum(len(gbif_ids)), 0) FROM H3Res3Species)
                + (SELECT coalesce(sum(len(gbif_ids)), 0) FROM H3Res7Species)
            """
        ).fetchone()[0])
        if relationships > MAX_EXPANDED_CELL_RELATIONSHIPS:
            raise RuntimeError(
                f"Refusing to expand {relationships:,} cell/species relationships "
                "for PostgreSQL. Re-run with --skip-expanded-cell-species; "
                "cell_species_lists.parquet will preserve the compact lists."
            )
        exports["cell_species.parquet"] = """
            SELECT CAST(CAST('0x' || hs.h3_index AS UBIGINT) AS BIGINT) AS h3_index,
                   resolution, ids.gbif_id AS gbif_accepted_id
            FROM (
                SELECT h3_index, gbif_ids, 3::SMALLINT AS resolution FROM H3Res3Species
                UNION ALL
                SELECT h3_index, gbif_ids, 7::SMALLINT AS resolution FROM H3Res7Species
            ) hs, UNNEST(hs.gbif_ids) AS ids(gbif_id)
            ORDER BY resolution, h3_index, gbif_accepted_id
        """
    for filename, query in exports.items():
        target = output_dir / filename
        connection.execute(
            f"COPY ({query}) TO {sql_path(target)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
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
    layer = f"res{resolution}_{system.lower()}"
    return {
        "type": "Feature",
        "tippecanoe": {
            "layer": layer,
            "minzoom": 0 if resolution == 3 else 6,
            "maxzoom": 7 if resolution == 3 else 12,
        },
        "properties": {
            "h3_index": h3_index,
            "resolution": resolution,
            "system": system,
            "total": row[1],
            "cr": row[2], "en": row[3], "vu": row[4],
            "nt": row[5], "dd": row[6], "lc": row[7],
            "ms": row[8], "mg": row[9], "mf": row[10],
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def iter_query_rows(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    batch_size: int = TILE_FEATURE_BATCH_SIZE,
) -> Iterator[tuple]:
    cursor = connection.execute(query)
    while rows := cursor.fetchmany(batch_size):
        yield from rows


def stream_tile_features(
    connection: duckdb.DuckDBPyConnection,
    stream: TextIO,
    batch_size: int = TILE_FEATURE_BATCH_SIZE,
) -> int:
    """Write all tile features incrementally without materializing them in Python."""
    count = 0
    for resolution in (3, 7):
        for system in SYSTEMS:
            query = f"SELECT * FROM h3_res{resolution}_agg_{system} ORDER BY h3_index"
            for row in iter_query_rows(connection, query, batch_size):
                stream.write(json.dumps(
                    feature(row, resolution, system),
                    separators=(",", ":"),
                ) + "\n")
                count += 1
    return count


def build_pmtiles(connection: duckdb.DuckDBPyConnection, target: Path, tippecanoe: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
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
            "--read-parallel",
            "--quiet",
        ],
        stdin=subprocess.PIPE,
        text=True,
    )
    if process.stdin is None:
        process.kill()
        raise RuntimeError("Tippecanoe did not expose a standard-input stream")
    try:
        feature_count = stream_tile_features(connection, process.stdin)
    except BaseException:
        process.stdin.close()
        process.kill()
        process.wait()
        raise
    process.stdin.close()
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, process.args)
    print(f"Built {target} from {feature_count:,} streamed features")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-aggregates", action="store_true")
    parser.add_argument("--skip-tiles", action="store_true")
    parser.add_argument(
        "--skip-expanded-cell-species",
        action="store_true",
        help=(
            "Preserve compact cell species lists but do not create the relational "
            "cell_species export used by the current PostgreSQL detail endpoint."
        ),
    )
    parser.add_argument(
        "--validation-only",
        action="store_true",
        help="Validate existing materialized tables without exporting or building tiles.",
    )
    args = parser.parse_args()
    settings = get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    settings.tile_dir.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(settings.build_duckdb_path))
    try:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if args.rebuild_aggregates or "h3_res7_agg_all" not in tables:
            connection.execute(
                f"ATTACH {sql_path(settings.source_duckdb_path)} AS source (READ_ONLY)"
            )
            materialize_aggregates(connection)
        report = validate_materialized_data(connection)
        write_validation_report(report, settings.validation_report_path)
        require_valid(report)
        if args.validation_only:
            return
        export_parquet(
            connection,
            settings.export_dir,
            include_expanded_cell_species=not args.skip_expanded_cell_species,
        )
        export_map_metadata(connection, settings.map_metadata_path)
        if not args.skip_tiles:
            build_pmtiles(connection, settings.pmtiles_path, settings.tippecanoe_bin)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
