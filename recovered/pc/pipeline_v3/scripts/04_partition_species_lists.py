#!/usr/bin/env python3
"""
04_partition_species_lists.py  —  Build the res-7 species-list Parquet
                                   dataset partitioned by H3 res-2 parent.

§5 Step F + decision 3 (§8) of GLOBAL_H3_RESET_PLAN.md.

DATA FLOW:
  script 01_polyfill_pairs.py  →  pipeline_v3/temp/h3_pairs/class=*.parquet
                                 (raw h3_index, id_no pairs, one per file)
  script 04 (this)             →  data/global/h3_res7_species/
                                 (SELECT DISTINCT pairs, partitioned by res2)

This script reads the raw pair files from script 01, deduplicates globally
via DuckDB SELECT DISTINCT (removes cross-file and cross-polygon duplicates),
and writes the result as a Parquet dataset partitioned by H3 res-2 parent
cell so the app can run
  SELECT id_no FROM ... WHERE res2 = ?
without scanning the whole planet.

Output layout:
  data/global/h3_res7_species/
  └── res2=<parent_cell>/
      └── data_0.parquet   # columns: h3_index, id_no  (one row per distinct pair)

LOSSLESS: SELECT DISTINCT only removes exact duplicate rows (same cell +
same species from overlapping polygons or cross-file species). No cells or
species are dropped. If (cell X, species Y) is in the input, it is in the
output exactly once.

Usage:
    uv run python pipeline_v3/scripts/04_partition_species_lists.py
"""
from __future__ import annotations

import argparse
import logging
import os
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


SPECIES_SCHEMA = pa.schema([
    pa.field("h3_index", pa.string()),   # res-7 cell
    pa.field("id_no", pa.int64()),       # IUCN species id
    pa.field("res2", pa.string()),       # res-2 parent (partition column, also stored for safety)
])


# ---------------------------------------------------------------------------
# Distinct (h3_index, id_no) -> Parquet with res2 parent column
# ---------------------------------------------------------------------------
def write_distinct_with_res2(
    con: duckdb.DuckDBPyConnection,
    pairs_dir: Path,
    out_path: Path,
) -> int:
    """SELECT DISTINCT (h3_index, id_no) across all pair files, add the res-2
    parent column via h3.cell_to_parent in a Python streaming pass, and write
    to a single Parquet file (partitioned dataset written in step 2).

    Returns the total number of distinct pairs.
    """
    pair_files = sorted(pairs_dir.glob("class=*.parquet"))
    if not pair_files:
        raise RuntimeError(f"No pair files found in {pairs_dir}")
    file_list = ", ".join(f"'{f.as_posix()}'" for f in pair_files)
    logger.info("Reading DISTINCT (h3_index, id_no) from %d pair files...", len(pair_files))
    t0 = time.time()

    # First, write the distinct relation to a temp Parquet (DuckDB out-of-core).
    distinct_path = config.TEMP_DIR / "distinct_pairs.parquet"
    con.execute(f"""
        COPY (
            SELECT DISTINCT h3_index, id_no
            FROM read_parquet([{file_list}])
        ) TO '{distinct_path.as_posix()}'
        (FORMAT 'parquet', COMPRESSION 'zstd', ROW_GROUP_SIZE 100000)
    """)
    n_distinct = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{distinct_path.as_posix()}')"
    ).fetchone()[0]
    logger.info("  distinct pairs: %d (%.1fs)", n_distinct, time.time() - t0)

    # Then add the res2 parent column via a Python streaming pass.
    logger.info("Adding res2 parent column and writing %s ...", out_path)
    t1 = time.time()
    pf = pq.ParquetFile(distinct_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    writer = pq.ParquetWriter(str(tmp), SPECIES_SCHEMA, compression="zstd")
    rows_written = 0

    for batch in pf.iter_batches(batch_size=100_000):
        pyd = batch.to_pydict()
        h3_indices = pyd["h3_index"]
        id_nos = pyd["id_no"]
        n = len(h3_indices)
        res2s = [""] * n
        for i, cell in enumerate(h3_indices):
            try:
                res2s[i] = h3.cell_to_parent(cell, config.H3_PARTITION_RES)
            except Exception:
                res2s[i] = ""
        table = pa.Table.from_pydict(
            {"h3_index": h3_indices, "id_no": id_nos, "res2": res2s},
            schema=SPECIES_SCHEMA,
        )
        writer.write_table(table)
        rows_written += n
        if rows_written % 2_000_000 == 0:
            logger.info("  ... %d pairs (%.1fs)", rows_written, time.time() - t1)

    writer.close()
    os.replace(str(tmp), str(out_path))
    logger.info("  wrote %d pairs with res2 column in %.1fs", rows_written, time.time() - t1)

    # Clean up the temp distinct file.
    try:
        os.remove(distinct_path)
    except OSError:
        pass
    return rows_written


# ---------------------------------------------------------------------------
# Partition by res2 parent — write the dataset layout
# ---------------------------------------------------------------------------
def write_partitioned_dataset(distinct_with_res2: Path, out_dir: Path) -> int:
    """Re-write the flat Parquet as a partitioned dataset by res2 parent.

    Uses DuckDB's COPY ... PARTITION_BY to write one directory per res2
    parent cell. This is the layout the app drills into.
    """
    logger.info("Partitioning %s by res2 -> %s ...", distinct_with_res2.name, out_dir)
    t0 = time.time()
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={config.DUCKDB_THREADS}")

    # Clean any existing partitioned dataset (atomic-ish: write to a temp
    # dir, then swap).
    tmp_dir = out_dir.parent / (out_dir.name + ".tmp")
    if tmp_dir.exists():
        import shutil
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    con.execute(f"""
        COPY (
            SELECT h3_index, id_no, res2
            FROM read_parquet('{distinct_with_res2.as_posix()}')
        ) TO '{tmp_dir.as_posix()}'
        (FORMAT 'parquet', COMPRESSION 'zstd', PARTITION_BY (res2),
         ROW_GROUP_SIZE 100000, OVERWRITE_OR_IGNORE TRUE)
    """)

    # Swap the temp dir into place.
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    os.replace(str(tmp_dir), str(out_dir))

    n_parts = len(list(out_dir.glob("res2=*")))
    con.close()
    logger.info("  wrote %d res2 partitions in %.1fs", n_parts, time.time() - t0)
    return n_parts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the partitioned res-7 species-list dataset"
    )
    parser.add_argument("--pairs-dir", type=Path, default=config.H3_PAIRS_DIR,
                        help=f"Dir of intermediate pair files (default: {config.H3_PAIRS_DIR})")
    parser.add_argument("--out-dir", type=Path, default=config.H3_RES7_SPECIES_DIR,
                        help=f"Output partitioned dataset dir (default: {config.H3_RES7_SPECIES_DIR})")
    args = parser.parse_args()

    config.ensure_dirs()

    logger.info("=" * 60)
    logger.info("SCRIPT 04: PARTITIONED RES-7 SPECIES LISTS (by res2)")
    logger.info("=" * 60)

    t_total = time.time()

    con = duckdb.connect()
    con.execute(f"PRAGMA threads={config.DUCKDB_THREADS}")
    con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory='{config.TEMP_DIR.as_posix()}'")

    distinct_path = config.TEMP_DIR / "distinct_pairs_with_res2.parquet"
    n_pairs = write_distinct_with_res2(con, args.pairs_dir, distinct_path)
    con.close()

    n_parts = write_partitioned_dataset(distinct_path, args.out_dir)

    # Clean up the flat intermediate.
    try:
        os.remove(distinct_path)
    except OSError:
        pass

    logger.info("=" * 60)
    logger.info("DONE: %s", args.out_dir)
    logger.info("  distinct pairs: %d", n_pairs)
    logger.info("  res2 partitions: %d", n_parts)
    logger.info("  total time: %.1fs", time.time() - t_total)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
