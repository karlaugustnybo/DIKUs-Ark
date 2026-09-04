#!/usr/bin/env python3
"""Build the normalized DuckDB source used by the serving-artifact pipeline.

Two H3 input contracts are supported:

* Denmark/prototype: ``(h3_index, gbif_accepted_ids)``
* Global aggregate: ``(h3_cell, species_ids)``, where ``species_ids`` are
  IUCN ``internalTaxonId`` values.

Global input requires ``H3_ID_CROSSWALK_PATH``. The crosswalk is a Parquet
file with exactly one usable mapping per source ID:

* ``source_species_id`` or ``iucn_sis_id`` -- IUCN ``internalTaxonId``
* ``app_species_id`` -- stable application species identifier

For compatibility with the Denmark prototype, ``gbif_accepted_id`` is also
accepted in place of ``app_species_id``. The chosen application ID must match
the primary identifier in ``SpecInfo``.

The build deliberately does not materialize centroids or classify cells
against a Denmark polygon. System membership comes from species metadata,
and H3 geometry is produced only while streaming map features.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import duckdb
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ark_pipeline.runtime.progress import monitor_query, tracked_stage
from ark_pipeline.runtime.resources import configure_duckdb

load_dotenv(ROOT / ".env")


def configured_path(name: str, default: str) -> Path:
    value = Path(os.environ.get(name, default)).expanduser()
    return value if value.is_absolute() else (ROOT / value).resolve()


def sql_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


DATA_DIR = configured_path("DATA_DIR", "data")
TABULAR_DB_PATH = configured_path(
    "TABULAR_DUCKDB_PATH", str(DATA_DIR / "denmark_tabular.duckdb")
)
DB_PATH = configured_path("SOURCE_DUCKDB_PATH", str(DATA_DIR / "Ark-IV.duckdb"))
H3_RES3_PARQUET = configured_path(
    "H3_RES3_PARQUET", str(DATA_DIR / "h3_res3_species.parquet")
)
H3_RES7_PARQUET = configured_path(
    "H3_RES7_PARQUET", str(DATA_DIR / "h3_res7_species.parquet")
)
_species_parquet_value = os.environ.get("SPECIES_PARQUET", "").strip()
SPECIES_PARQUET = (
    configured_path("SPECIES_PARQUET", _species_parquet_value)
    if _species_parquet_value
    else None
)
_species_systems_value = os.environ.get("SPECIES_SYSTEMS_PARQUET", "").strip()
SPECIES_SYSTEMS_PARQUET = (
    configured_path("SPECIES_SYSTEMS_PARQUET", _species_systems_value)
    if _species_systems_value
    else None
)
VALIDATION_REPORT_PATH = configured_path(
    "SOURCE_VALIDATION_REPORT_PATH",
    str(DATA_DIR / "validation" / "source-validation.json"),
)
_crosswalk_value = os.environ.get("H3_ID_CROSSWALK_PATH", "").strip()
H3_ID_CROSSWALK_PATH = (
    configured_path("H3_ID_CROSSWALK_PATH", _crosswalk_value)
    if _crosswalk_value
    else None
)


def parquet_columns(connection: duckdb.DuckDBPyConnection, path: Path) -> set[str]:
    rows = connection.execute(
        "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
    ).fetchall()
    return {row[0] for row in rows}


def validate_crosswalk(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
) -> dict[str, Any]:
    columns = parquet_columns(connection, path)
    source_column = (
        "source_species_id" if "source_species_id" in columns else "iucn_sis_id"
    )
    if source_column not in columns:
        raise ValueError(
            f"Crosswalk {path} must contain source_species_id or iucn_sis_id"
        )
    if "app_species_id" in columns:
        target_column = "app_species_id"
    elif "gbif_accepted_id" in columns:
        target_column = "gbif_accepted_id"
    else:
        # The global application can retain IUCN as its stable primary key.
        target_column = source_column

    row = connection.execute(
        f"""
        WITH crosswalk AS (
            SELECT
                CAST({source_column} AS VARCHAR) AS source_species_id,
                CAST({target_column} AS VARCHAR) AS app_species_id
            FROM read_parquet(?)
        )
        SELECT
            count(*) AS rows,
            count(*) FILTER (
                WHERE source_species_id IS NULL OR app_species_id IS NULL
            ) AS null_mappings,
            count(*) - count(DISTINCT source_species_id) AS duplicate_source_rows,
            (
                SELECT count(*) FROM (
                    SELECT app_species_id
                    FROM crosswalk
                    GROUP BY app_species_id
                    HAVING count(DISTINCT source_species_id) > 1
                )
            ) AS many_to_one_targets
        FROM crosswalk
        """,
        [str(path)],
    ).fetchone()
    keys = ("rows", "null_mappings", "duplicate_source_rows", "many_to_one_targets")
    stats = dict(zip(keys, map(int, row), strict=True))
    failures = {key: value for key, value in stats.items() if key != "rows" and value}
    if failures:
        details = ", ".join(f"{key}={value}" for key, value in failures.items())
        raise ValueError(f"Crosswalk is not lossless and one-to-one: {details}")
    stats["source_column"] = source_column
    stats["target_column"] = target_column
    return stats


def h3_input_kind(columns: set[str]) -> str:
    if {"h3_index", "gbif_accepted_ids"} <= columns:
        return "prototype"
    if {"h3_cell", "species_ids"} <= columns:
        return "global"
    raise ValueError(
        "Unsupported H3 Parquet schema. Expected (h3_index, gbif_accepted_ids) "
        "or (h3_cell, species_ids)."
    )


def inspect_h3_input(
    connection: duckdb.DuckDBPyConnection,
    resolution: int,
    path: Path,
    *,
    deep: bool = False,
) -> dict[str, Any]:
    """Inspect one H3 list file without requiring species metadata or a crosswalk."""
    kind = h3_input_kind(parquet_columns(connection, path))
    if kind == "prototype":
        expressions = [
            "count(*) AS cells",
            "coalesce(sum(len(gbif_accepted_ids)), 0) AS relationships",
            "count(*) FILTER (WHERE h3_index IS NULL) AS null_h3_cells",
            "count(*) FILTER (WHERE gbif_accepted_ids IS NULL) AS null_lists",
        ]
        if deep:
            expressions.extend([
                "count(*) FILTER (WHERE list_unique(gbif_accepted_ids) "
                "<> len(gbif_accepted_ids)) AS cells_with_duplicate_ids",
                "count(*) - count(DISTINCT h3_index) AS duplicate_h3_cells",
            ])
        row = connection.execute(
            f"""
            SELECT {", ".join(expressions)}
            FROM read_parquet(?)
            """,
            [str(path)],
        ).fetchone()
        keys = (
            "cells", "relationships", "null_h3_cells", "null_lists",
            *(("cells_with_duplicate_ids", "duplicate_h3_cells") if deep else ()),
        )
    else:
        expressions = [
            "count(*) AS cells",
            "coalesce(sum(len(species_ids)), 0) AS relationships",
            "count(*) FILTER (WHERE h3_cell IS NULL) AS null_h3_cells",
            "count(*) FILTER (WHERE species_ids IS NULL) AS null_lists",
            "count(*) FILTER (WHERE ((h3_cell >> 52) & 15) <> ?) "
            "AS wrong_resolution_cells",
        ]
        if deep:
            expressions.extend([
                "count(*) FILTER (WHERE list_unique(species_ids) "
                "<> len(species_ids)) AS cells_with_duplicate_ids",
                "count(*) - count(DISTINCT h3_cell) AS duplicate_h3_cells",
            ])
        row = connection.execute(
            f"""
            SELECT {", ".join(expressions)}
            FROM read_parquet(?)
            """,
            [resolution, str(path)],
        ).fetchone()
        keys = (
            "cells", "relationships", "null_h3_cells", "null_lists",
            "wrong_resolution_cells",
            *(("cells_with_duplicate_ids", "duplicate_h3_cells") if deep else ()),
        )
    report: dict[str, Any] = dict(zip(keys, map(int, row), strict=True))
    if not deep:
        report["cells_with_duplicate_ids"] = None
        report["duplicate_h3_cells"] = None
    report.update({
        "deep_checks": deep,
        "input_kind": kind,
        "path": str(path),
        "resolution": resolution,
    })
    report["failures"] = [
        f"{key}={report[key]}"
        for key in (
            "null_h3_cells", "null_lists", "cells_with_duplicate_ids",
            "duplicate_h3_cells", "wrong_resolution_cells",
        )
        if report.get(key, 0)
    ]
    return report


def build_h3_table(
    connection: duckdb.DuckDBPyConnection,
    resolution: int,
    path: Path,
    crosswalk_path: Path | None,
    *,
    deep_validation: bool = True,
) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"H3 resolution-{resolution} input not found: {path}")
    inspection = inspect_h3_input(
        connection, resolution, path, deep=deep_validation
    )
    kind = inspection["input_kind"]
    table = f"H3Res{resolution}Species"

    if kind == "prototype":
        connection.execute(
            f"""
            CREATE TABLE {table} AS
            SELECT
                CAST(h3_index AS VARCHAR) AS h3_index,
                list_transform(
                    gbif_accepted_ids, item -> CAST(item AS VARCHAR)
                ) AS gbif_ids
            FROM read_parquet(?)
            """,
            [str(path)],
        )
        report: dict[str, Any] = {
            "source_cells": inspection["cells"],
            "source_relationships": inspection["relationships"],
            "null_lists": inspection["null_lists"],
            "cells_with_duplicate_ids": inspection["cells_with_duplicate_ids"],
            "input_kind": kind,
            "mapped_relationships": inspection["relationships"],
            "unmatched_relationships": 0,
        }
    else:
        if crosswalk_path is None:
            raise ValueError(
                f"{path} uses global IUCN species IDs; set H3_ID_CROSSWALK_PATH "
                "to the crosswalk Parquet before building the source database"
            )
        crosswalk_columns = parquet_columns(connection, crosswalk_path)
        source_column = (
            "source_species_id"
            if "source_species_id" in crosswalk_columns
            else "iucn_sis_id"
        )
        if "app_species_id" in crosswalk_columns:
            target_column = "app_species_id"
        elif "gbif_accepted_id" in crosswalk_columns:
            target_column = "gbif_accepted_id"
        else:
            target_column = source_column
        identity_mapping = target_column == source_column
        if identity_mapping:
            # The global app keeps IUCN as its stable key. Preserve the compact
            # lists directly instead of expanding tens of billions of
            # relationships only to map every ID to itself.
            connection.execute(
                f"""
                CREATE TABLE {table} AS
                SELECT
                    lower(to_hex(h3_cell)) AS h3_index,
                    list_transform(
                        species_ids, item -> CAST(item AS VARCHAR)
                    ) AS gbif_ids
                FROM read_parquet(?)
                """,
                [str(path)],
            )
        else:
            connection.execute(
                f"""
                CREATE TABLE {table} AS
                WITH expanded AS (
                    SELECT
                        lower(to_hex(h3_cell)) AS h3_index,
                        CAST(ids.source_species_id AS VARCHAR) AS source_species_id
                    FROM read_parquet(?),
                    UNNEST(species_ids) AS ids(source_species_id)
                ),
                mapped AS (
                    SELECT
                        expanded.h3_index,
                        CAST(cw.{target_column} AS VARCHAR) AS gbif_id
                    FROM expanded
                    JOIN read_parquet(?) cw
                      ON expanded.source_species_id =
                         CAST(cw.{source_column} AS VARCHAR)
                )
                SELECT h3_index, list(gbif_id ORDER BY gbif_id) AS gbif_ids
                FROM mapped
                GROUP BY h3_index
                """,
                [str(path), str(crosswalk_path)],
            )
        stats = connection.execute(
            f"""
            WITH mapped AS (
                SELECT
                    count(*) AS mapped_cells,
                    coalesce(sum(len(gbif_ids)), 0) AS mapped_relationships
                FROM {table}
            )
            SELECT * FROM mapped
            """,
        ).fetchone()
        report = {
            "source_cells": inspection["cells"],
            "source_relationships": inspection["relationships"],
            "null_lists": inspection["null_lists"],
            "cells_with_duplicate_ids": inspection["cells_with_duplicate_ids"],
            "mapped_cells": int(stats[0]),
            "mapped_relationships": int(stats[1]),
        }
        report.update({
            "input_kind": kind,
            "identity_mapping": identity_mapping,
            "unmatched_relationships": (
                report["source_relationships"] - report["mapped_relationships"]
            ),
            "unmatched_cells": report["source_cells"] - report["mapped_cells"],
        })

    connection.execute(
        f"CREATE UNIQUE INDEX {table.lower()}_h3_index ON {table}(h3_index)"
    )
    return report


def build_species_tables(
    connection: duckdb.DuckDBPyConnection,
    species_path: Path | None = None,
    species_systems_path: Path | None = None,
) -> None:
    if (species_path is None) != (species_systems_path is None):
        raise ValueError(
            "SPECIES_PARQUET and SPECIES_SYSTEMS_PARQUET must be provided together"
        )
    if species_path is not None and species_systems_path is not None:
        connection.execute(
            "CREATE TABLE SpecInfo AS SELECT * FROM read_parquet(?)",
            [str(species_path)],
        )
        connection.execute(
            "CREATE UNIQUE INDEX specinfo_id ON SpecInfo(gbif_accepted_id)"
        )
        connection.execute(
            "CREATE TABLE SpecSystems AS SELECT * FROM read_parquet(?)",
            [str(species_systems_path)],
        )
        return

    connection.execute(
        """
        CREATE TABLE SpecInfo (
            gbif_accepted_id VARCHAR PRIMARY KEY,
            iucn_sis_id VARCHAR,
            iucn_assessment_id VARCHAR,
            gbif_taxon_id VARCHAR,
            goat_taxon_id VARCHAR,
            species_name VARCHAR,
            family VARCHAR,
            redlist_category VARCHAR,
            has_dna_species_level BOOL,
            genus_has_dna BOOL,
            family_has_dna BOOL,
            goat_data_deficient BOOL,
            edge_group_name VARCHAR,
            has_ebp_criteria_evidence BOOL
        );

        INSERT INTO SpecInfo
        SELECT DISTINCT
            d.gbif_accepted_id,
            NULL::VARCHAR AS iucn_sis_id,
            NULL::VARCHAR AS iucn_assessment_id,
            d.gbif_accepted_id AS gbif_taxon_id,
            g.goat_taxon_id,
            d.species_name,
            d.family,
            d.redlist_category,
            d.has_dna_species_level,
            d.genus_has_dna,
            d.family_has_dna,
            coalesce(g.goat_data_deficient, true),
            e.edge_group_name,
            coalesce(g.has_ebp_criteria_evidence, false)
        FROM tabular.dna d
        LEFT JOIN tabular.edge e USING (gbif_accepted_id)
        LEFT JOIN (
            SELECT
                gbif_accepted_id,
                min(cast(taxon_id AS VARCHAR)) FILTER (WHERE taxon_id IS NOT NULL)
                    AS goat_taxon_id,
                NOT bool_or(taxon_id IS NOT NULL) AS goat_data_deficient,
                bool_or(
                    ebp_standard_criteria IS NOT NULL
                    AND (
                        ebp_standard_criteria LIKE '%6.7%'
                        OR ebp_standard_criteria LIKE '%6.C%'
                    )
                ) AS has_ebp_criteria_evidence
            FROM tabular.goat
            GROUP BY gbif_accepted_id
        ) g USING (gbif_accepted_id);

        CREATE TABLE SpecSystems AS
        SELECT DISTINCT
            i.gbif_accepted_id,
            CASE
                WHEN trim(raw_system) LIKE 'Freshwater%' THEN 'Freshwater'
                WHEN trim(raw_system) = 'Terrestrial' THEN 'Terrestrial'
                WHEN trim(raw_system) = 'Marine' THEN 'Marine'
                ELSE 'Unknown'
            END AS system
        FROM tabular.iucn i,
        UNNEST(string_split(coalesce(i.systems, ''), '|')) AS parts(raw_system);
        """
    )


def validate_source_database(
    connection: duckdb.DuckDBPyConnection,
    h3_reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    duplicate_species = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT gbif_accepted_id
            FROM SpecInfo
            GROUP BY gbif_accepted_id
            HAVING count(*) > 1
        )
        """
    ).fetchone()[0]
    if duplicate_species:
        failures.append(f"SpecInfo contains {duplicate_species} duplicate IDs")

    resolutions: dict[str, Any] = {}
    for resolution in sorted(map(int, h3_reports)):
        table = f"H3Res{resolution}Species"
        joined = connection.execute(
            f"""
            WITH expanded AS (
                SELECT h3_index, ids.gbif_accepted_id
                FROM {table}, UNNEST(gbif_ids) AS ids(gbif_accepted_id)
            )
            SELECT
                count(*) AS normalized_relationships,
                count(*) FILTER (
                    WHERE species.gbif_accepted_id IS NOT NULL
                ) AS relationships_with_species_metadata,
                count(DISTINCT expanded.h3_index) AS normalized_cells,
                count(DISTINCT expanded.h3_index) FILTER (
                    WHERE species.gbif_accepted_id IS NOT NULL
                ) AS cells_with_species_metadata
            FROM expanded
            LEFT JOIN SpecInfo species USING (gbif_accepted_id)
            """
        ).fetchone()
        keys = (
            "normalized_relationships", "relationships_with_species_metadata",
            "normalized_cells", "cells_with_species_metadata",
        )
        stats = dict(zip(keys, map(int, joined), strict=True))
        stats.update(h3_reports[str(resolution)])
        stats["relationships_without_species_metadata"] = (
            stats["normalized_relationships"]
            - stats["relationships_with_species_metadata"]
        )
        stats["cells_without_species_metadata"] = (
            stats["normalized_cells"] - stats["cells_with_species_metadata"]
        )
        resolutions[str(resolution)] = stats

        for field in (
            "null_lists",
            "cells_with_duplicate_ids",
            "unmatched_relationships",
            "relationships_without_species_metadata",
            "cells_without_species_metadata",
        ):
            if value := stats.get(field, 0):
                failures.append(f"resolution {resolution}: {field}={value}")

    return {
        "version": 1,
        "status": "ok" if not failures else "failed",
        "duplicate_species_ids": int(duplicate_species),
        "resolutions": resolutions,
        "failures": failures,
    }


def write_report(report: dict[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Wrote validation report: {target}")


def build_database(
    target: Path,
    overwrite: bool = False,
    *,
    resolutions: tuple[int, ...] = (3, 7),
    deep_validation: bool = True,
    require_lossless: bool = True,
    species_path: Path | None = None,
    species_systems_path: Path | None = None,
    h3_paths: dict[int, Path] | None = None,
    crosswalk_path: Path | None = None,
) -> dict[str, Any]:
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} already exists; pass --overwrite to replace it after a backup"
        )
    resolutions = tuple(sorted(set(resolutions)))
    if not resolutions or not set(resolutions) <= {3, 7}:
        raise ValueError("resolutions must contain 3 and/or 7")
    species_path = species_path if species_path is not None else SPECIES_PARQUET
    species_systems_path = (
        species_systems_path
        if species_systems_path is not None
        else SPECIES_SYSTEMS_PARQUET
    )
    h3_paths = h3_paths or {3: H3_RES3_PARQUET, 7: H3_RES7_PARQUET}
    crosswalk_path = (
        crosswalk_path if crosswalk_path is not None else H3_ID_CROSSWALK_PATH
    )
    required_paths = [h3_paths[resolution] for resolution in resolutions]
    if species_path is None:
        if species_systems_path is not None:
            raise ValueError(
                "species_path and species_systems_path must be provided together"
            )
        required_paths.append(TABULAR_DB_PATH)
    else:
        if species_systems_path is None:
            raise ValueError(
                "species_path and species_systems_path must be provided together"
            )
        required_paths.extend([species_path, species_systems_path])
    for required in required_paths:
        assert required is not None
        if not required.exists():
            raise FileNotFoundError(required)

    crosswalk_stats = None
    scratch = target.with_name(f"{target.name}.building")
    if scratch.exists():
        scratch.unlink()
    connection = duckdb.connect(str(scratch))
    configure_duckdb(connection)
    stop_query_monitor = monitor_query(connection)
    try:
        if species_path is None:
            connection.execute(
                f"ATTACH {sql_path(TABULAR_DB_PATH)} AS tabular (READ_ONLY)"
            )
        if crosswalk_path is not None:
            crosswalk_stats = validate_crosswalk(connection, crosswalk_path)
        build_species_tables(connection, species_path, species_systems_path)
        h3_reports = {
            str(resolution): build_h3_table(
                connection,
                resolution,
                h3_paths[resolution],
                crosswalk_path,
                deep_validation=deep_validation,
            )
            for resolution in resolutions
        }
        if require_lossless:
            report = validate_source_database(connection, h3_reports)
        else:
            report = {
                "version": 1,
                "status": "deferred",
                "validation": "Structural checks only; lossless join audit deferred",
                "resolutions": h3_reports,
                "failures": [],
            }
        if crosswalk_stats is not None:
            report["crosswalk"] = crosswalk_stats
        write_report(report, VALIDATION_REPORT_PATH)
        if report["failures"]:
            details = "\n".join(f"  - {item}" for item in report["failures"])
            raise RuntimeError(f"Source build is not lossless:\n{details}")
    except BaseException:
        stop_query_monitor()
        connection.close()
        if scratch.exists():
            scratch.unlink()
        raise
    else:
        stop_query_monitor()
        connection.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.stem}_backup{target.suffix}")
        shutil.copy2(target, backup)
        print(f"Backed up existing database to {backup}")
    scratch.replace(target)
    print(f"Built {target}")
    return report


@tracked_stage("coarse_db")
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Back up and replace an existing source database.",
    )
    parser.add_argument("--target", type=Path, default=DB_PATH)
    parser.add_argument(
        "--resolutions", type=int, nargs="+", choices=(3, 7), default=[3, 7]
    )
    parser.add_argument("--species-parquet", type=Path, default=SPECIES_PARQUET)
    parser.add_argument(
        "--species-systems-parquet", type=Path, default=SPECIES_SYSTEMS_PARQUET
    )
    parser.add_argument("--h3-res3", type=Path, default=H3_RES3_PARQUET)
    parser.add_argument("--h3-res7", type=Path, default=H3_RES7_PARQUET)
    parser.add_argument("--crosswalk", type=Path, default=H3_ID_CROSSWALK_PATH)
    parser.add_argument(
        "--defer-lossless-validation",
        action="store_true",
        help="Run structural checks but defer duplicate and full species-join audits.",
    )
    args = parser.parse_args()
    build_database(
        args.target,
        overwrite=args.overwrite,
        resolutions=tuple(args.resolutions),
        deep_validation=not args.defer_lossless_validation,
        require_lossless=not args.defer_lossless_validation,
        species_path=args.species_parquet,
        species_systems_path=args.species_systems_parquet,
        h3_paths={3: args.h3_res3, 7: args.h3_res7},
        crosswalk_path=args.crosswalk,
    )


if __name__ == "__main__":
    main()
