#!/usr/bin/env python3
"""
03_derive_res3_metrics.py  —  Derive res-3 metrics by re-running a fresh
                               DISTINCT (parent, id_no) + JOIN + aggregate.

§5 Step E points 6-7 of GLOBAL_H3_RESET_PLAN.md.

Critical: res-3 metrics are NOT the sum of res-7 metric columns. Summing
would double-count species that span multiple res-7 children of the same
res-3 cell. Instead we:
  1. Re-derive the distinct (h3_index, id_no) relation from the pair files.
  2. Map each res-7 h3_index to its res-3 parent via h3.cell_to_parent.
  3. Run SELECT DISTINCT (parent, id_no) — removes the cross-res7-child
     duplicates within the same res-3 cell.
  4. Re-join the distinct (parent, id_no) pairs to species metadata and
     aggregate counts fresh, exactly as for res-7.
  5. Recalculate dna_coverage_score fresh from the distinct relation (NOT by
     averaging the res-7 averages — averaging averages is statistically wrong
     and mis-weights cells whose res-7 children carry different species counts).
  6. Add lat/lng via h3.cell_to_latlng(parent) in Python.
  7. Output data/global/h3_res3_metrics.parquet.

Usage:
    uv run python pipeline_v3/scripts/03_derive_res3_metrics.py
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
from _common_metrics import METRICS_SCHEMA, build_species_metadata, add_lat_lng  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Map res-7 cells to res-3 parents and emit distinct (parent, id_no) pairs.
# ---------------------------------------------------------------------------
def build_distinct_parent_pairs(
    con: duckdb.DuckDBPyConnection,
    pairs_dir: Path,
    out_path: Path,
) -> Path:
    """Write distinct (res3_parent, id_no) pairs to a Parquet file.

    We map each res-7 h3_index to its res-3 parent in Python (h3 has no
    DuckDB-side function for this), then let DuckDB do the DISTINCT on the
    (parent, id_no) relation across all pair files. The intermediate file
    is bounded by |res-3 cells × species| which is much smaller than the
    res-7 pair relation.
    """
    pair_files = sorted(pairs_dir.glob("class=*.parquet"))
    if not pair_files:
        raise RuntimeError(f"No pair files found in {pairs_dir}")
    logger.info("Mapping res-7 pairs -> res-3 parents (%d pair files)...",
                len(pair_files))
    t0 = time.time()

    parent_schema = pa.schema([
        pa.field("h3_index", pa.string()),  # res-3 parent cell
        pa.field("id_no", pa.int64()),
    ])

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    writer = pq.ParquetWriter(str(tmp), parent_schema, compression="zstd")
    buffer: list[tuple[str, int]] = []
    total_in = 0

    for pf_path in pair_files:
        pf = pq.ParquetFile(pf_path)
        for batch in pf.iter_batches(batch_size=100_000, columns=["h3_index", "id_no"]):
            pyd = batch.to_pydict()
            for h7, id_no in zip(pyd["h3_index"], pyd["id_no"]):
                try:
                    parent = h3.cell_to_parent(h7, config.H3_PARENT_RES)
                except Exception:
                    continue
                buffer.append((parent, int(id_no)))
                if len(buffer) >= config.WRITE_BATCH:
                    _flush_parent(buffer, writer)
                    buffer.clear()
            total_in += batch.num_rows
        logger.info("  read %s (cumulative %d pairs, %.1fs)",
                    pf_path.name, total_in, time.time() - t0)

    _flush_parent(buffer, writer)
    writer.close()
    import os
    os.replace(str(tmp), str(out_path))

    logger.info("  wrote res-3 (parent, id_no) pairs to %s (%.1fs)",
                out_path.name, time.time() - t0)
    return out_path


def _flush_parent(buffer: list[tuple[str, int]], writer: pq.ParquetWriter) -> None:
    if not buffer:
        return
    h3s, ids = zip(*buffer)
    table = pa.Table.from_pydict(
        {"h3_index": list(h3s), "id_no": list(ids)},
        schema=pa.schema([pa.field("h3_index", pa.string()), pa.field("id_no", pa.int64())]),
    )
    writer.write_table(table)


# ---------------------------------------------------------------------------
# Aggregate distinct (parent, id_no) -> res-3 metrics (mirrors script 02)
# ---------------------------------------------------------------------------
def aggregate_res3_metrics(
    con: duckdb.DuckDBPyConnection,
    parent_pairs_path: Path,
    meta_table: str,
) -> Path:
    """SELECT DISTINCT (parent, id_no) -> JOIN metadata -> aggregate per cell."""
    logger.info("Aggregating res-3 metrics from %s ...", parent_pairs_path.name)
    t0 = time.time()

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
            FROM read_parquet('{parent_pairs_path.as_posix()}')
        ) d
        LEFT JOIN {meta_table} s ON d.id_no = s.id_no
        GROUP BY d.h3_index
        ORDER BY d.h3_index
    """
    agg_path = config.TEMP_DIR / "res3_metrics_no_latlng.parquet"
    con.execute(f"""
        COPY ({agg_sql}) TO '{agg_path.as_posix()}'
        (FORMAT 'parquet', COMPRESSION 'zstd', ROW_GROUP_SIZE 50000)
    """)
    n_cells = con.execute(f"SELECT COUNT(*) FROM read_parquet('{agg_path.as_posix()}')").fetchone()[0]
    logger.info("  aggregated %d res-3 cells in %.1fs", n_cells, time.time() - t0)
    return agg_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive res-3 metrics (fresh DISTINCT + aggregate)"
    )
    parser.add_argument("--pairs-dir", type=Path, default=config.H3_PAIRS_DIR,
                        help=f"Dir of intermediate pair files (default: {config.H3_PAIRS_DIR})")
    parser.add_argument("--out", type=Path, default=config.H3_RES3_METRICS,
                        help=f"Output Parquet (default: {config.H3_RES3_METRICS})")
    args = parser.parse_args()

    config.ensure_dirs()

    logger.info("=" * 60)
    logger.info("SCRIPT 03: DERIVE RES-3 METRICS (fresh DISTINCT)")
    logger.info("=" * 60)

    con = duckdb.connect()
    con.execute(f"PRAGMA threads={config.DUCKDB_THREADS}")
    con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"SET temp_directory='{config.TEMP_DIR.as_posix()}'")

    t_total = time.time()

    meta_table = build_species_metadata(con)
    parent_pairs = config.TEMP_DIR / "res3_parent_pairs.parquet"
    build_distinct_parent_pairs(con, args.pairs_dir, parent_pairs)
    agg_path = aggregate_res3_metrics(con, parent_pairs, meta_table)
    result = add_lat_lng(agg_path, args.out)

    con.close()

    logger.info("=" * 60)
    logger.info("DONE: %s", result["output"])
    logger.info("  res-3 cells: %d", result["cells"])
    logger.info("  total time: %.1fs", time.time() - t_total)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
