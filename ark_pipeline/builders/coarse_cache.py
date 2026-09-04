#!/usr/bin/env python3
"""Build serving artifacts with DuckDB, then leave DuckDB off the request path.

Outputs:
  * Parquet tables consumed by ``backend/load_postgres.py``
  * one Tippecanoe-built PMTiles archive consumed directly by deck.gl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

import duckdb
import h3
import pyarrow as pa
import pyarrow.ipc as pa_ipc

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ark_pipeline.runtime.progress import monitor_query, tracked_stage
from ark_pipeline.runtime.resources import configure_duckdb, configured_count
from ark_pipeline.spatial.boundaries import JurisdictionIndex, load_jurisdiction_index
from backend.config import get_settings

SYSTEMS = ("all", "Terrestrial", "Freshwater", "Marine")
DEFAULT_WEIGHTS = {
    "cr": 4.0, "en": 3.0, "vu": 2.0, "nt": 1.0, "dd": 2.0, "lc": 0.1,
    "sp": 2.0, "gen": 3.0, "fam": 4.0, "gdd": 4.0, "samp": 0.0,
}
TILE_FEATURE_BATCH_SIZE = 10_000
TILE_ZOOM_RANGES = {
    3: {"min": 0, "max": 6},
    7: {"min": 8, "max": 12},
}
MAX_EXPANDED_CELL_RELATIONSHIPS = 100_000_000
SUMMARY_METRICS = {
    "total_species": "TRUE",
    "crit_endangered_count": "redlist_category = 'Critically Endangered'",
    "endangered_count": "redlist_category = 'Endangered'",
    "vulnerable_count": "redlist_category = 'Vulnerable'",
    "near_threatened_count": "redlist_category = 'Near Threatened'",
    "data_deficient_count": "redlist_category = 'Data Deficient'",
    "least_concern_count": "redlist_category = 'Least Concern'",
    "missing_species_dna": (
        "goat_data_deficient = false AND family_has_dna = true "
        "AND genus_has_dna = true AND has_dna_species_level = false"
    ),
    "missing_genus_dna": (
        "goat_data_deficient = false AND family_has_dna = true "
        "AND genus_has_dna = false"
    ),
    "missing_family_dna": "goat_data_deficient = false AND family_has_dna = false",
    "goat_data_deficient": "goat_data_deficient = true",
}
THREAT_SCORE_PREDICATES = {
    "cr": "redlist_category = 'Critically Endangered'",
    "en": "redlist_category = 'Endangered'",
    "vu": "redlist_category = 'Vulnerable'",
    "nt": "redlist_category = 'Near Threatened'",
    "dd": "redlist_category = 'Data Deficient'",
    "lc": "redlist_category = 'Least Concern'",
}
DNA_SCORE_PREDICATES = {
    # These predicates are mutually exclusive and mirror the priority CASE in
    # backend/app.py: family, then genus, then species, otherwise sampled.
    "gdd": "goat_data_deficient = true",
    "fam": "goat_data_deficient = false AND family_has_dna = false",
    "gen": (
        "goat_data_deficient = false "
        "AND family_has_dna IS DISTINCT FROM false AND genus_has_dna = false"
    ),
    "sp": (
        "goat_data_deficient = false "
        "AND family_has_dna IS DISTINCT FROM false "
        "AND genus_has_dna IS DISTINCT FROM false "
        "AND has_dna_species_level = false"
    ),
    "samp": (
        "goat_data_deficient = false "
        "AND family_has_dna IS DISTINCT FROM false "
        "AND genus_has_dna IS DISTINCT FROM false "
        "AND has_dna_species_level IS DISTINCT FROM false"
    ),
}
JOINT_SCORE_METRICS = {
    f"priority_{threat}_{dna}_count": f"({threat_predicate}) AND ({dna_predicate})"
    for threat, threat_predicate in THREAT_SCORE_PREDICATES.items()
    for dna, dna_predicate in DNA_SCORE_PREDICATES.items()
}
JOINT_SCORE_WEIGHTS = {
    f"priority_{threat}_{dna}_count": DEFAULT_WEIGHTS[threat] * DEFAULT_WEIGHTS[dna]
    for threat in THREAT_SCORE_PREDICATES
    for dna in DNA_SCORE_PREDICATES
}
# Marginal counts support tooltips. Exact joint counts are the sufficient
# statistics for recomputing sum(threat_weight * DNA_weight) for arbitrary
# browser slider values without retaining per-cell species IDs at runtime.
METRICS = {**SUMMARY_METRICS, **JOINT_SCORE_METRICS}
SCORE_WEIGHT_ORDER = tuple(
    JOINT_SCORE_WEIGHTS.get(metric, 0.0)
    for metric in METRICS
)
SYSTEM_PREDICATES = {
    "all": "TRUE",
    "Terrestrial": "is_terrestrial",
    "Freshwater": "is_freshwater",
    "Marine": "is_marine",
}
SYSTEM_TILE_PREFIXES = {
    "all": "a",
    "Terrestrial": "t",
    "Freshwater": "f",
    "Marine": "m",
}
BOUNDARY_TILE_PROPERTIES = {
    "admin0": "j",
    "admin1": "a1",
    "municipality": "mun",
    "eez": "eez",
    "conservation_framework": "eco",
}
COARSE_SNAPSHOT_SCHEMA_VERSION = 1
METRIC_TILE_NAMES = {
    "total_species": "total",
    "crit_endangered_count": "cr",
    "endangered_count": "en",
    "vulnerable_count": "vu",
    "near_threatened_count": "nt",
    "data_deficient_count": "dd",
    "least_concern_count": "lc",
    "missing_species_dna": "ms",
    "missing_genus_dna": "mg",
    "missing_family_dna": "mf",
    "goat_data_deficient": "gdd",
    **{
        metric: metric.removeprefix("priority_").removesuffix("_count")
        for metric in JOINT_SCORE_METRICS
    },
}


def score_expression(column_name=lambda metric: metric) -> str:
    """Return the exact default-weight cell-priority SQL expression."""
    return " + ".join(
        f"{column_name(metric)} * {weight}"
        for metric, weight in JOINT_SCORE_WEIGHTS.items()
    )


def sql_path(path: Path) -> str:
    """Return a safely quoted SQL string literal for DuckDB COPY statements."""
    return "'" + str(path).replace("'", "''") + "'"


def materialize_aggregates(
    connection: duckdb.DuckDBPyConnection,
    resolutions: tuple[int, ...] = (3, 7),
) -> None:
    """Rebuild aggregates one H3 base cell at a time.

    A global res-7 build contains tens of billions of cell/species
    relationships. Base-cell partitioning keeps each GROUP BY bounded while
    still expanding each H3 list only once per partition.
    """
    for table in ("SpecInfo", "SpecSystems"):
        connection.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM source.{table}")

    for resolution in resolutions:
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


def validate_materialized_data(
    connection: duckdb.DuckDBPyConnection,
    resolutions_to_check: tuple[int, ...] = (3, 7),
) -> dict[str, Any]:
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

    for resolution in resolutions_to_check:
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
    resolutions: tuple[int, ...] = (3, 7),
    include_expanded_cell_species: bool = True,
    boundary_indexes: dict[str, JurisdictionIndex] | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    exports = {
        "species.parquet": """
            SELECT gbif_accepted_id, iucn_sis_id, iucn_assessment_id,
                gbif_taxon_id, goat_taxon_id, species_name, family,
                redlist_category, has_dna_species_level, genus_has_dna,
                family_has_dna, goat_data_deficient,
                edge_group_name, has_ebp_criteria_evidence
            FROM SpecInfo ORDER BY gbif_accepted_id
        """,
        "species_systems.parquet": """
            SELECT DISTINCT gbif_accepted_id, system FROM SpecSystems
            WHERE system IN ('Terrestrial', 'Freshwater', 'Marine')
            ORDER BY gbif_accepted_id, system
        """,
        "cell_species_lists.parquet": f"""
            SELECT
                CAST(CAST('0x' || hs.h3_index AS UBIGINT) AS BIGINT) AS h3_index,
                resolution,
                hs.gbif_ids AS species_ids
            FROM (
                {" UNION ALL ".join(
                    f"SELECT h3_index, gbif_ids, {resolution}::SMALLINT AS resolution "
                    f"FROM H3Res{resolution}Species"
                    for resolution in resolutions
                )}
            ) hs
            ORDER BY resolution, h3_index
        """,
    }
    if 3 in resolutions:
        exports["species_cells_res3.parquet"] = """
            SELECT
                ids.gbif_id AS gbif_accepted_id,
                3::SMALLINT AS resolution,
                list(
                    CAST(CAST('0x' || hs.h3_index AS UBIGINT) AS BIGINT)
                    ORDER BY hs.h3_index
                ) AS h3_indexes
            FROM H3Res3Species hs, UNNEST(hs.gbif_ids) AS ids(gbif_id)
            GROUP BY ids.gbif_id
            ORDER BY ids.gbif_id
        """
    if include_expanded_cell_species:
        relationships = int(connection.execute(
            f"""
            SELECT
                {" + ".join(
                    f"(SELECT coalesce(sum(len(gbif_ids)), 0) FROM H3Res{resolution}Species)"
                    for resolution in resolutions
                )}
            """
        ).fetchone()[0])
        if relationships > MAX_EXPANDED_CELL_RELATIONSHIPS:
            raise RuntimeError(
                f"Refusing to expand {relationships:,} cell/species relationships "
                "for PostgreSQL. Re-run with --skip-expanded-cell-species; "
                "cell_species_lists.parquet will preserve the compact lists."
            )
        exports["cell_species.parquet"] = f"""
            SELECT CAST(CAST('0x' || hs.h3_index AS UBIGINT) AS BIGINT) AS h3_index,
                   resolution, ids.gbif_id AS gbif_accepted_id
            FROM (
                {" UNION ALL ".join(
                    f"SELECT h3_index, gbif_ids, {resolution}::SMALLINT AS resolution "
                    f"FROM H3Res{resolution}Species"
                    for resolution in resolutions
                )}
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
    if boundary_indexes is not None and 3 in resolutions:
        export_cell_boundaries(connection, output_dir, boundary_indexes)


def export_cell_boundaries(
    connection: duckdb.DuckDBPyConnection,
    output_dir: Path,
    boundary_indexes: dict[str, JurisdictionIndex],
) -> None:
    """Export every boundary touched by each coarse cell for table filtering."""
    rows = []
    for h3_index, in connection.execute(
        "SELECT h3_index FROM h3_res3_agg_all ORDER BY h3_index"
    ).fetchall():
        codes = {
            framework: list(index.codes_for_cell(h3_index))
            for framework, index in boundary_indexes.items()
        }
        rows.append((
            int(h3_index, 16),
            3,
            *(codes.get(framework, []) for framework in BOUNDARY_TILE_PROPERTIES),
        ))
    boundary_columns = ", ".join(
        f"{framework} VARCHAR[]" for framework in BOUNDARY_TILE_PROPERTIES
    )
    connection.execute(
        "CREATE OR REPLACE TEMP TABLE exported_cell_boundaries ("
        f"h3_index BIGINT, resolution SMALLINT, {boundary_columns})"
    )
    if rows:
        placeholders = ", ".join("?" for _ in range(2 + len(BOUNDARY_TILE_PROPERTIES)))
        connection.executemany(
            f"INSERT INTO exported_cell_boundaries VALUES ({placeholders})", rows
        )
    target = output_dir / "cell_boundaries.parquet"
    connection.execute(
        f"COPY exported_cell_boundaries TO {sql_path(target)} "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    print(f"Exported {target}")


def normalize_boundary_indexes(
    value: dict[str, JurisdictionIndex] | JurisdictionIndex | None,
) -> dict[str, JurisdictionIndex]:
    if value is None:
        return {}
    if isinstance(value, JurisdictionIndex):
        return {"admin0": value}
    return value


def export_map_metadata(
    connection: duckdb.DuckDBPyConnection,
    target: Path,
    resolutions: tuple[int, ...] = (3, 7),
    jurisdiction_index: dict[str, JurisdictionIndex] | JurisdictionIndex | None = None,
    coarse_snapshot: dict[str, Any] | None = None,
) -> None:
    """Export raw and per-species score scales across map resolutions."""
    score = score_expression()
    domains: dict[str, dict[str, float]] = {}
    normalized_domains: dict[str, dict[str, float]] = {}
    for system in SYSTEMS:
        aggregates = " UNION ALL ".join(
            f"SELECT * FROM h3_res{resolution}_agg_{system}"
            for resolution in resolutions
        )
        minimum, maximum = connection.execute(
            f"SELECT MIN({score}), MAX({score}) FROM ({aggregates})"
        ).fetchone()
        normalized_minimum, normalized_maximum = connection.execute(
            f"SELECT MIN(({score}) / NULLIF(total_species, 0)), "
            f"MAX(({score}) / NULLIF(total_species, 0)) "
            f"FROM ({aggregates})"
        ).fetchone()
        domains[system.lower()] = {
            # A benchmark or regional extract can have no species in a system.
            "min": float(minimum or 0), "max": float(maximum or 0),
        }
        normalized_domains[system.lower()] = {
            "min": float(normalized_minimum or 0), "max": float(normalized_maximum or 0),
        }
    boundary_indexes = normalize_boundary_indexes(jurisdiction_index)
    boundary_domains: dict[str, dict[str, dict[str, dict[str, float]]]] = {
        framework: {system.lower(): {} for system in SYSTEMS}
        for framework in boundary_indexes
    }
    normalized_boundary_domains: dict[str, dict[str, dict[str, dict[str, float]]]] = {
        framework: {system.lower(): {} for system in SYSTEMS}
        for framework in boundary_indexes
    }
    if boundary_indexes and 3 in resolutions:
        for row in iter_query_rows(connection, wide_aggregate_query(3)):
            for framework, index in boundary_indexes.items():
                codes = index.codes_for_cell(row[0])
                if not codes:
                    continue
                for system_index, system in enumerate(SYSTEMS):
                    offset = 1 + system_index * len(METRICS)
                    value = sum(
                        float(row[offset + metric_index]) * weight
                        for metric_index, weight in enumerate(SCORE_WEIGHT_ORDER)
                    )
                    total_species = float(row[offset])
                    normalized_value = value / total_species if total_species else 0.0
                    for code in codes:
                        domain = boundary_domains[framework][system.lower()].setdefault(
                            code, {"min": value, "max": value}
                        )
                        domain["min"] = min(domain["min"], value)
                        domain["max"] = max(domain["max"], value)
                        normalized_domain = normalized_boundary_domains[framework][
                            system.lower()
                        ].setdefault(
                            code, {"min": normalized_value, "max": normalized_value}
                        )
                        normalized_domain["min"] = min(
                            normalized_domain["min"], normalized_value
                        )
                        normalized_domain["max"] = max(
                            normalized_domain["max"], normalized_value
                        )

    metadata: dict[str, Any] = {
        "version": 9,
        "tile_schema_version": 9,
        "tile_layout": "wide-v2-joint-priority",
        "resolution_tile_ranges": {
            str(resolution): TILE_ZOOM_RANGES[resolution]
            for resolution in resolutions
        },
        "available_resolutions": list(resolutions),
        "complete_resolutions": list(resolutions),
        "detail_resolutions": list(resolutions),
        "score_domains": domains,
        "species_normalized_score_domains": normalized_domains,
        "reference_weights": DEFAULT_WEIGHTS,
    }
    if coarse_snapshot is not None:
        metadata["coarse_snapshot"] = coarse_snapshot
    if boundary_indexes:
        metadata.update({
            "jurisdiction_assignment": "cell-intersection",
            "jurisdiction_domains_resolution": 3,
            "jurisdiction_score_domains": boundary_domains.get("admin0", {}),
            "boundary_assignment": "cell-intersection",
            "boundary_score_domains": boundary_domains,
            "boundary_species_normalized_score_domains": normalized_boundary_domains,
            "boundary_tile_properties": BOUNDARY_TILE_PROPERTIES,
        })
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metadata, separators=(",", ":")) + "\n")
    print(f"Exported {target}")


def export_coarse_snapshot(
    connection: duckdb.DuckDBPyConnection,
    tile_dir: Path,
    jurisdiction_index: dict[str, JurisdictionIndex] | JurisdictionIndex | None = None,
) -> dict[str, Any]:
    """Write every resolution-3 cell once as a compact, typed Arrow table.

    The PMTiles archive repeats the same 40k coarse cells across eight web-tile
    zoom levels. This single immutable snapshot is small enough to preload on
    the homepage and lets the map render resolution 3 without any tile swaps.
    """
    prefixes = tuple(SYSTEM_TILE_PREFIXES[system] for system in SYSTEMS)
    projections = ["s0.h3_index AS h3_index"]
    aliases = [f"s{index}" for index in range(len(SYSTEMS))]
    for alias, prefix in zip(aliases, prefixes, strict=True):
        projections.extend(
            f"coalesce({alias}.{metric}, 0)::USMALLINT AS "
            f'"{prefix}_{METRIC_TILE_NAMES[metric]}"'
            for metric in METRICS
        )
    joins = " ".join(
        f"LEFT JOIN h3_res3_agg_{system} {alias} USING (h3_index)"
        for system, alias in zip(SYSTEMS[1:], aliases[1:], strict=True)
    )
    reader = connection.execute(
        f"SELECT {', '.join(projections)} FROM h3_res3_agg_all s0 "
        f"{joins} ORDER BY h3_index"
    ).to_arrow_reader(batch_size=TILE_FEATURE_BATCH_SIZE)
    table = reader.read_all()
    h3_indexes = table.column("h3_index").to_pylist()
    boundary_indexes = normalize_boundary_indexes(jurisdiction_index)
    for framework in BOUNDARY_TILE_PROPERTIES:
        index = boundary_indexes.get(framework)
        memberships = [
            list(index.codes_for_cell(h3_index)) if index is not None else []
            for h3_index in h3_indexes
        ]
        table = table.append_column(
            framework,
            pa.array(memberships, type=pa.list_(pa.string())),
        )

    tile_dir.mkdir(parents=True, exist_ok=True)
    temporary = tile_dir / "res3-priorities.arrow.tmp"
    with temporary.open("wb") as stream:
        with pa_ipc.new_file(stream, table.schema) as writer:
            writer.write_table(table)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()[:16]
    filename = f"res3-priorities-{digest}.arrow"
    target = tile_dir / filename
    temporary.replace(target)
    print(f"Exported {target} ({table.num_rows:,} cells)")
    return {
        "format": "arrow-ipc-v1",
        "schema_version": COARSE_SNAPSHOT_SCHEMA_VERSION,
        "resolution": 3,
        "cells": table.num_rows,
        "url": f"/tiles/{filename}",
        "systems": list(prefixes),
        "metrics": list(METRIC_TILE_NAMES.values()),
        "boundary_columns": list(BOUNDARY_TILE_PROPERTIES),
    }


def feature(row: tuple, resolution: int, system: str) -> dict:
    h3_index = row[0]
    boundary = h3.cell_to_boundary(h3_index)
    ring = [[longitude, latitude] for latitude, longitude in boundary]
    geometry = antimeridian_safe_polygon(ring)
    layer = f"res{resolution}_{system.lower()}"
    zoom_range = TILE_ZOOM_RANGES[resolution]
    return {
        "type": "Feature",
        "tippecanoe": {
            "layer": layer,
            "minzoom": zoom_range["min"],
            "maxzoom": zoom_range["max"],
        },
        "properties": {
            "h3_index": h3_index,
            "resolution": resolution,
            "system": system,
            "total": row[1],
            "cr": row[2], "en": row[3], "vu": row[4],
            "nt": row[5], "dd": row[6], "lc": row[7],
            "ms": row[8], "mg": row[9], "mf": row[10],
            "gdd": row[11],
        },
        "geometry": geometry,
    }


def wide_feature(
    row: tuple, resolution: int,
    jurisdiction_code: str | dict[str, str | tuple[str, ...]] = "",
) -> dict:
    """Encode one geometry with metrics for every ecosystem system.

    Vector-tile geometry dominates a global resolution-7 archive. Keeping all
    four system projections on one feature avoids duplicating the same H3
    polygon four times and lets the browser recolour without fetching a new
    layer when the ecosystem filter changes.
    """
    h3_index = row[0]
    boundary = h3.cell_to_boundary(h3_index)
    ring = [[longitude, latitude] for latitude, longitude in boundary]
    boundary_codes = (
        {"admin0": jurisdiction_code}
        if isinstance(jurisdiction_code, str) else jurisdiction_code
    )
    properties: dict[str, str | int] = {
        "h3_index": h3_index,
        "resolution": resolution,
    }
    for framework, property_name in BOUNDARY_TILE_PROPERTIES.items():
        value = boundary_codes.get(framework, "")
        properties[property_name] = (
            "|".join(value) if isinstance(value, tuple) else value
        )
    offset = 1
    for system in SYSTEMS:
        prefix = SYSTEM_TILE_PREFIXES[system]
        for metric in METRICS:
            properties[f"{prefix}_{METRIC_TILE_NAMES[metric]}"] = int(row[offset])
            offset += 1
    zoom_range = TILE_ZOOM_RANGES[resolution]
    return {
        "type": "Feature",
        "tippecanoe": {
            "layer": f"res{resolution}",
            "minzoom": zoom_range["min"],
            "maxzoom": zoom_range["max"],
        },
        "properties": properties,
        "geometry": antimeridian_safe_polygon(ring),
    }


def _clip_vertical(
    ring: list[list[float]], boundary: float, *, keep_less: bool
) -> list[list[float]]:
    """Clip a convex longitude/latitude ring against one meridian."""
    if not ring:
        return []

    def inside(point: list[float]) -> bool:
        return point[0] <= boundary if keep_less else point[0] >= boundary

    def intersection(start: list[float], end: list[float]) -> list[float]:
        fraction = (boundary - start[0]) / (end[0] - start[0])
        return [boundary, start[1] + fraction * (end[1] - start[1])]

    output: list[list[float]] = []
    previous = ring[-1]
    previous_inside = inside(previous)
    for current in ring:
        current_inside = inside(current)
        if current_inside:
            if not previous_inside:
                output.append(intersection(previous, current))
            output.append(current)
        elif previous_inside:
            output.append(intersection(previous, current))
        previous = current
        previous_inside = current_inside
    return output


def _closed(ring: list[list[float]]) -> list[list[float]]:
    return ring + [ring[0]] if ring and ring[0] != ring[-1] else ring


def antimeridian_safe_polygon(ring: list[list[float]]) -> dict[str, Any]:
    """Split an H3 polygon at the antimeridian instead of spanning the globe."""
    if not ring:
        return {"type": "Polygon", "coordinates": []}
    unwrapped = [ring[0][:]]
    for longitude, latitude in ring[1:]:
        previous_longitude = unwrapped[-1][0]
        while longitude - previous_longitude > 180:
            longitude -= 360
        while longitude - previous_longitude < -180:
            longitude += 360
        unwrapped.append([longitude, latitude])

    minimum = min(point[0] for point in unwrapped)
    maximum = max(point[0] for point in unwrapped)
    if maximum > 180:
        west = _clip_vertical(unwrapped, 180, keep_less=True)
        east = _clip_vertical(unwrapped, 180, keep_less=False)
        east = [[longitude - 360, latitude] for longitude, latitude in east]
        return {
            "type": "MultiPolygon",
            "coordinates": [[_closed(west)], [_closed(east)]],
        }
    if minimum < -180:
        east = _clip_vertical(unwrapped, -180, keep_less=False)
        west = _clip_vertical(unwrapped, -180, keep_less=True)
        west = [[longitude + 360, latitude] for longitude, latitude in west]
        return {
            "type": "MultiPolygon",
            "coordinates": [[_closed(east)], [_closed(west)]],
        }
    return {"type": "Polygon", "coordinates": [_closed(unwrapped)]}


def iter_query_rows(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    batch_size: int = TILE_FEATURE_BATCH_SIZE,
) -> Iterator[tuple]:
    cursor = connection.execute(query)
    while rows := cursor.fetchmany(batch_size):
        yield from rows


def wide_aggregate_query(resolution: int) -> str:
    aliases = [f"s{index}" for index in range(len(SYSTEMS))]
    projections = ["s0.h3_index"]
    for alias in aliases:
        projections.extend(
            f"coalesce({alias}.{metric}, 0)" for metric in METRICS
        )
    joins = " ".join(
        f"LEFT JOIN h3_res{resolution}_agg_{system} {alias} USING (h3_index)"
        for system, alias in zip(SYSTEMS[1:], aliases[1:], strict=True)
    )
    return (
        f"SELECT {', '.join(projections)} "
        f"FROM h3_res{resolution}_agg_all s0 {joins} ORDER BY h3_index"
    )


def stream_tile_features(
    connection: duckdb.DuckDBPyConnection,
    stream: TextIO,
    batch_size: int = TILE_FEATURE_BATCH_SIZE,
    resolutions: tuple[int, ...] = (3, 7),
    jurisdiction_index: dict[str, JurisdictionIndex] | JurisdictionIndex | None = None,
) -> int:
    """Write all tile features incrementally without materializing them in Python."""
    count = 0
    boundary_indexes = normalize_boundary_indexes(jurisdiction_index)
    for resolution in resolutions:
        for row in iter_query_rows(
            connection, wide_aggregate_query(resolution), batch_size
        ):
            jurisdiction_code = {
                framework: index.codes_for_cell(row[0])
                for framework, index in boundary_indexes.items()
            }
            stream.write(json.dumps(
                wide_feature(row, resolution, jurisdiction_code),
                separators=(",", ":")
            ) + "\n")
            count += 1
    return count


def build_pmtiles(
    connection: duckdb.DuckDBPyConnection,
    target: Path,
    tippecanoe: str,
    resolutions: tuple[int, ...] = (3, 7),
    jurisdiction_index: dict[str, JurisdictionIndex] | JurisdictionIndex | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            tippecanoe,
            "--force",
            "--output", str(target),
            "--minimum-zoom", "0",
            "--maximum-zoom", "12" if 7 in resolutions else "7",
            "--no-feature-limit",
            "--no-tile-size-limit",
            "--preserve-input-order",
            "--generate-ids",
            "--read-parallel",
            "--quiet",
        ],
        stdin=subprocess.PIPE,
        text=True,
        env={**os.environ, "TIPPECANOE_MAX_THREADS": str(configured_count("TIPPECANOE_MAX_THREADS"))},
    )
    if process.stdin is None:
        process.kill()
        raise RuntimeError("Tippecanoe did not expose a standard-input stream")
    try:
        feature_count = stream_tile_features(
            connection, process.stdin, resolutions=resolutions,
            jurisdiction_index=jurisdiction_index,
        )
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


@tracked_stage("coarse_cache")
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-aggregates", action="store_true")
    parser.add_argument(
        "--resolutions", type=int, nargs="+", choices=(3, 7), default=[3, 7]
    )
    parser.add_argument("--skip-tiles", action="store_true")
    parser.add_argument(
        "--skip-exports",
        action="store_true",
        help="Rebuild map metadata/tiles without rewriting PostgreSQL Parquet exports.",
    )
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
    parser.add_argument(
        "--defer-lossless-validation",
        action="store_true",
        help="Skip the expensive full relationship audit for a preview build.",
    )
    args = parser.parse_args()
    resolutions = tuple(sorted(set(args.resolutions)))
    settings = get_settings()
    jurisdiction_index = {
        framework: load_jurisdiction_index(str(path))
        for framework, path in {
            "admin0": settings.jurisdictions_path,
            "admin1": settings.admin1_boundaries_path,
            "municipality": settings.municipality_boundaries_path,
            "eez": settings.eez_boundaries_path,
            "conservation_framework": settings.conservation_boundaries_path,
        }.items()
        if path.is_file()
    }
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    settings.tile_dir.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(settings.build_duckdb_path))
    configure_duckdb(connection)
    stop_query_monitor = monitor_query(connection)
    try:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        expected_aggregates = {
            f"h3_res{resolution}_agg_all" for resolution in resolutions
        }
        if args.rebuild_aggregates or not expected_aggregates <= tables:
            connection.execute(
                f"ATTACH {sql_path(settings.source_duckdb_path)} AS source (READ_ONLY)"
            )
            materialize_aggregates(connection, resolutions)
        if args.defer_lossless_validation:
            report = {
                "version": 1,
                "status": "deferred",
                "validation": "Full relationship audit deferred for preview build",
                "resolutions": {
                    str(resolution): {
                        "aggregate_cells": int(connection.execute(
                            f"SELECT count(*) FROM h3_res{resolution}_agg_all"
                        ).fetchone()[0])
                    }
                    for resolution in resolutions
                },
                "failures": [],
            }
        else:
            report = validate_materialized_data(connection, resolutions)
        write_validation_report(report, settings.validation_report_path)
        require_valid(report)
        if args.validation_only:
            return
        if not args.skip_exports:
            export_parquet(
                connection,
                settings.export_dir,
                resolutions=resolutions,
                include_expanded_cell_species=not args.skip_expanded_cell_species,
                boundary_indexes=jurisdiction_index,
            )
        coarse_snapshot = export_coarse_snapshot(
            connection, settings.tile_dir, jurisdiction_index
        ) if 3 in resolutions else None
        export_map_metadata(
            connection,
            settings.map_metadata_path,
            resolutions,
            jurisdiction_index,
            coarse_snapshot,
        )
        if not args.skip_tiles:
            build_pmtiles(
                connection,
                settings.pmtiles_path,
                settings.tippecanoe_bin,
                resolutions,
                jurisdiction_index,
            )
    finally:
        stop_query_monitor()
        connection.close()


if __name__ == "__main__":
    main()
