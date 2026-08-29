#!/usr/bin/env python3
"""
02_merge_metrics.py  —  Merge the 7 intermediate pair files and derive
                         per-cell res-7 metric counts.

§5 Step E of GLOBAL_H3_RESET_PLAN.md.

Consumes the intermediate Parquet files at
  pipeline_v3/temp/h3_pairs/class=<key>.parquet
produced by 01_polyfill_pairs.py, and writes
  data/global/h3_res7_metrics.parquet

Steps (all out-of-core in DuckDB):
  1. Build a species-metadata temp table from the 7 source GeoParquet files:
       SELECT DISTINCT id_no, redlistCategory, has_dna_species_level,
                        genus_has_dna, family_has_dna, dna_coverage_score
     (~78k rows — fits in memory).
  2. SELECT DISTINCT h3_index, id_no across all 7 pair files. This is the
     global deduplication that removes cross-file double counts (§5 Step C:
     9,201 species appear in more than one file) and overlapping polygons
     within a species (~1.6 polygons/species). DuckDB spills to disk.
  3. JOIN the distinct (h3_index, id_no) relation to species metadata and
     aggregate per cell with COUNT FILTER (WHERE redlistCategory = ...)
     to populate the 6 threat-category counters, total_species, the 3
     missing-DNA counters, and the average dna_coverage_score.
  4. Add latitude/longitude via h3.cell_to_latlng in Python (DuckDB has no
     stable h3 extension function for this; the cell count is bounded by
     |unique res-7 cells| so the Python pass is cheap).
  5. Stream-write the final Parquet with zstd compression.

Usage:
    uv run python pipeline_v3/scripts/02_merge_metrics.py
    uv run python pipeline_v3/scripts/02_merge_metrics.py --pairs-dir pipeline_v3/temp/h3_pairs/denmark
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import duckdb
import h3
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from _common_metrics import (  # noqa: E402
    METRICS_SCHEMA,
    build_species_metadata,
    add_lat_lng,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 2-3: DISTINCT + JOIN + aggregate -> per-cell res-7 metrics
# ---------------------------------------------------------------------------
def aggregate_res7_metrics(
    con: duckdb.DuckDBPyConnection,
    pairs_dir: Path,
    meta_table: str,
) -> Path:
    """Run the global DISTINCT + JOIN + aggregate pass; return the temp
    Parquet path holding the aggregated counts (without lat/lng yet)."""
    pair_files = sorted(pairs_dir.glob("class=*.parquet"))
    if not pair_files:
        raise RuntimeError(f"No pair files found in {pairs_dir}")
    file_list = ", ".join(f"'{f.as_posix()}'" for f in pair_files)
    logger.info("Aggregating %d pair files -> res-7 metrics...", len(pair_files))
    t0 = time.time()

    # DISTINCT h3_index, id_no across ALL pair files. DuckDB does this
    # out-of-core (spills to disk on the temp dir if needed).
    # Then JOIN to species metadata and aggregate per cell.
    agg_sql = f"""
        SELECT
            d.h3_index,
            COUNT(*)                                AS total_species,
            COUNT(*) FILTER (WHERE s.redlist_category = 'Critically Endangered') AS crit_endangered_count,
            COUNT(*) FILTER (WHERE s.redlist_category = 'Endangered')             AS endangered_count,
            COUNT(*) FILTER (WHERE s.redlist_category = 'Vulnerable')             AS vulnerable_count,
            COUNT(*) FILTER (WHERE s.redlist_category = 'Near Threatened')        AS near_threatened_count,
            COUNT(*) FILTER (WHERE s.redlist_category = 'Data Deficient')         AS data_deficient_count,
            COUNT(*) FILTER (WHERE s.redlist_category = 'Least Concern')          AS least_concern_count,
            COUNT(*) FILTER (WHERE s.has_dna_species_level = false)                AS missing_species_dna,
            COUNT(*) FILTER (WHERE s.genus_has_dna = false)                       AS missing_genus_dna,
            COUNT(*) FILTER (WHERE s.family_has_dna = false)                      AS missing_family_dna,
            CAST(AVG(s.dna_coverage_score) AS BIGINT)                             AS dna_coverage_score
        FROM (
            SELECT DISTINCT h3_index, id_no
            FROM read_parquet([{file_list}])
        ) d
        LEFT JOIN {meta_table} s ON d.id_no = s.id_no
        GROUP BY d.h3_index
        ORDER BY d.h3_index
    """
    # Write to a temp Parquet (no lat/lng yet — added in the Python pass).
    agg_path = config.TEMP_DIR / "res7_metrics_no_latlng.parquet"
    con.execute(f"""
        COPY ({agg_sql}) TO '{agg_path.as_posix()}'
        (FORMAT 'parquet', COMPRESSION 'zstd', ROW_GROUP_SIZE 50000)
    """)
    n_cells = con.execute(f"SELECT COUNT(*) FROM read_parquet('{agg_path.as_posix()}')").fetchone()[0]
    logger.info("  aggregated %d res-7 cells in %.1fs", n_cells, time.time() - t0)
    return agg_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge pair files and derive res-7 metrics"
    )
    parser.add_argument("--pairs-dir", type=Path, default=config.H3_PAIRS_DIR,
                        help=f"Dir of intermediate pair files (default: {config.H3_PAIRS_DIR})")
    parser.add_argument("--out", type=Path, default=config.H3_RES7_METRICS,
                        help=f"Output Parquet (default: {config.H3_RES7_METRICS})")
    args = parser.parse_args()

    config.ensure_dirs()

    logger.info("=" * 60)
    logger.info("SCRIPT 02: MERGE + AGGREGATE -> RES-7 METRICS")
    logger.info("=" * 60)

    con = duckdb.connect()
    con.execute(f"PRAGMA threads={config.DUCKDB_THREADS}")
    con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
    con.execute("INSTALL spatial; LOAD spatial;")
    # Spill to the pipeline temp dir to avoid filling the system temp.
    con.execute(f"SET temp_directory='{config.TEMP_DIR.as_posix()}'")

    t_total = time.time()

    meta_table = build_species_metadata(con)
    agg_path = aggregate_res7_metrics(con, args.pairs_dir, meta_table)
    result = add_lat_lng(agg_path, args.out)

    con.close()

    logger.info("=" * 60)
    logger.info("DONE: %s", result["output"])
    logger.info("  res-7 cells: %d", result["cells"])
    logger.info("  total time: %.1fs", time.time() - t_total)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
