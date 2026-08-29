#!/usr/bin/env python3
"""
Script 07: Export Tree-of-Life (ToL) entries for Denmark species lineages.

Reads the merged_gbif output from Script 06 to collect **every** unique
taxonomic name from genus → kingdom that appears among the Denmark species.
Then reads the **new** ToL TSV dataset (BioDatasets/TOL/*.tsv) and filters to
only entries whose lowercased `scientific_name` matches one of the collected
Denmark names.  Keeps **all** original ToL columns intact.

Result: one row per matching ToL entry.

Usage (Denmark)
    uv run python pipeline_v2/scripts/07_export_tol.py \
        --merged data/denmark/h3_merged_raw.parquet \
        --out data/denmark/h3_tol.parquet
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = ROOT / "BioDatasets"
DEFAULT_MERGED = ROOT / "data" / "global" / "h3_merged_raw.parquet"
DEFAULT_OUT = ROOT / "data" / "global" / "h3_tol.parquet"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _extract_tol_names(merged_path: Path) -> set[str]:
    """Collect every unique lower-case taxonomic name from the merged output."""
    logger.info("Reading merged file to collect Denmark taxa ...")
    con = duckdb.connect()
    con.execute(f"PRAGMA threads=4")

    names: set[str] = set()
    # Collect _all_ non-null column values that hold taxonomic names.
    # Columns: kingdomName, phylumName, className, orderName, familyName, genusName
    #          (IUCN side) and kingdom, phylum, class, "order", family, genus
    #          (GOAT side).
    iucn_cols = ["kingdomName", "phylumName", "className", "orderName", "familyName", "genusName"]
    goat_cols = ["kingdom", "phylum", "class", "order", "family", "genus"]
    all_cols = iucn_cols + [c for c in goat_cols if c not in iucn_cols]  # keep distinct

    for col in all_cols:
        try:
            rows = con.execute(f"""
                SELECT DISTINCT LOWER(CAST("{col}" AS VARCHAR))
                FROM read_parquet('{merged_path}')
                WHERE "{col}" IS NOT NULL AND CAST("{col}" AS VARCHAR) != ''
            """).fetchall()
            for r in rows:
                if r[0]:
                    names.add(r[0])
        except Exception:
            # Column may not exist in current merged_schema (e.g. orderName vs "order" confusion)
            continue

    con.close()
    logger.info("  Collected %d unique taxonomic names", len(names))
    return names


def _build_pipeline(
    merged_path: Path,
    out_path: Path,
    raw_dir: Path,
    threads: int = 4,
) -> dict[str, Any]:
    if not raw_dir.exists():
        raise FileNotFoundError(f"Raw data directory not found: {raw_dir}")
    if not merged_path.exists():
        raise FileNotFoundError(f"Merged input not found: {merged_path}")

    tol_names = _extract_tol_names(merged_path)
    t0 = time.time()
    con = duckdb.connect(":memory:")
    con.execute(f"PRAGMA threads={threads}")
    con.execute(f"PRAGMA memory_limit='4GB'")

    # -----------------------------------------------------------------
    # Phase 1: Load names into temp table (for efficient filtering)
    # -----------------------------------------------------------------
    logger.info("Loading filter names into temp table ...")
    con.execute("CREATE TEMP TABLE _all_names (name VARCHAR)")
    batch: list[tuple[str]] = []
    batch_size = 50_000
    for n in tol_names:
        batch.append((n,))
        if len(batch) >= batch_size:
            con.executemany("INSERT INTO _all_names VALUES (?)", batch)
            batch.clear()
    if batch:
        con.executemany("INSERT INTO _all_names VALUES (?)", batch)
        batch.clear()
    cnt = con.execute("SELECT COUNT(*) FROM _all_names").fetchone()[0]
    logger.info("  %d unique names loaded into _all_names", cnt)

    # -----------------------------------------------------------------
    # Phase 1b: Build name -> gbif_accepted_id list mapping from merged
    # -----------------------------------------------------------------
    logger.info("Building gbif_accepted_id lists per taxon name ...")
    con.execute("CREATE TEMP TABLE _name_ids (name VARCHAR, gbif_accepted_ids VARCHAR[])")
    name_cols = ["kingdomName", "phylumName", "className", "orderName", "familyName", "genusName", "kingdom", "phylum", "class", "order", "family", "genus"]
    union_queries: list[str] = []
    for col in name_cols:
        union_queries.append(
            f"SELECT LOWER(CAST(\"{col}\" AS VARCHAR)) AS name, CAST(gbif_accepted_id AS VARCHAR) AS gbif_accepted_id "
            f"FROM read_parquet('{merged_path}') "
            f"WHERE \"{col}\" IS NOT NULL AND CAST(\"{col}\" AS VARCHAR) != '' AND gbif_accepted_id IS NOT NULL"
        )
    union_sql = " UNION ALL ".join(union_queries)
    con.execute(f"""
        INSERT INTO _name_ids
        SELECT name, LIST_DISTINCT(LIST(gbif_accepted_id)) AS gbif_accepted_ids
        FROM ({union_sql})
        WHERE name IS NOT NULL AND name != ''
        GROUP BY name
    """)
    name_cnt = con.execute("SELECT COUNT(*) FROM _name_ids").fetchone()[0]
    logger.info("  %d name-to-gbif_accepted_id lists built", name_cnt)

    # -----------------------------------------------------------------
    # Phase 2: Load ToL TSVs, filter to matching entries
    # -----------------------------------------------------------------
    logger.info("Loading ToL TSV dataset ...")
    _tab = chr(9)
    tol_glob = raw_dir / "TOL" / "*.tsv"
    con.execute(f"""
        CREATE TEMP TABLE tol_full AS
        SELECT *
        FROM read_csv_auto(
            '{tol_glob}',
            delim = '{_tab}',
            all_varchar = true,
            strict_mode = false
        )
    """)
    total = con.execute("SELECT COUNT(*) FROM tol_full").fetchone()[0]
    logger.info("  ToL total rows: %d", total)

    # Match ANY ToL entry whose lowercased scientific_name is in the Denmark set.
    # This includes all ranks (kingdom, phylum, class, order, family, genus).
    logger.info("Filtering to matching ToL entries ...")
    con.execute(f"""
        CREATE TEMP TABLE tol_filtered AS
        SELECT tol_full.*, ids.gbif_accepted_ids
        FROM tol_full
        LEFT JOIN _name_ids ids
          ON LOWER(CAST(tol_full.scientific_name AS VARCHAR)) = ids.name
        WHERE LOWER(CAST(tol_full.scientific_name AS VARCHAR)) IN (SELECT name FROM _all_names)
    """)
    matched = con.execute("SELECT COUNT(*) FROM tol_filtered").fetchone()[0]
    logger.info("  Matched: %d rows", matched)

    # Export all original ToL columns plus gbif_accepted_ids list
    tol_cols = [r[0] for r in con.execute("DESCRIBE tol_filtered").fetchall()]
    select_sql = ", ".join(f'"{c}"' for c in tol_cols)

    export_sql = f"""
        SELECT {select_sql}
        FROM tol_filtered
        ORDER BY taxon_rank, scientific_name
    """

    con.execute(f"COPY ({export_sql}) TO '{out_path}' (FORMAT 'parquet', COMPRESSION 'zstd')")
    n_rows = con.execute(f"SELECT COUNT(*) FROM ({export_sql})").fetchone()[0]
    con.close()
    elapsed = time.time() - t0
    logger.info("Done: %d rows written to %s (%.1fs)", n_rows, out_path, elapsed)
    return {
        "output_path": str(out_path),
        "tol_total": total,
        "tol_matched": matched,
        "rows_exported": n_rows,
        "elapsed_sec": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export Tree-of-Life entries for Denmark species lineages"
    )
    parser.add_argument(
        "--merged",
        type=Path,
        default=DEFAULT_MERGED,
        help=f"Input merged_gbif from Script 06 (default: {DEFAULT_MERGED})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output Parquet (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=RAW_DIR,
        help=f"Source data root (default: {RAW_DIR})",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="DuckDB threads (default: 4)",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("SCRIPT 07: EXPORT TOL ENTRIES")
    logger.info("=" * 60)
    logger.info("  Merged   : %s", args.merged)
    logger.info("  Raw dir  : %s", args.raw_dir)
    logger.info("  Output   : %s", args.out)

    result = _build_pipeline(
        merged_path=args.merged,
        out_path=args.out,
        raw_dir=args.raw_dir,
        threads=args.threads,
    )
    logger.info("Result: %s", result)


if __name__ == "__main__":
    main()
