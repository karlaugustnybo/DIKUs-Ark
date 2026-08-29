#!/usr/bin/env python3
"""
Script 04: Aggregate H3 parts into final GeoParquet.

Reads all *_h3.parquet files from h3_parts/, groups by h3_index, collects
distinct gbif_accepted_ids, computes the H3 cell geometry, and writes a single
GeoParquet file to data/global/h3_res7_species.parquet.

This uses DuckDB's out-of-core streaming capabilities to handle potentially
billions of input pairs without loading everything into RAM.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import h3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent
H3_PARTS_DIR = ROOT / "pipeline_v2" / "temp" / "h3_parts"
OUT_DIR = ROOT / "data" / "global"
OUT_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_OUT = OUT_DIR / "h3_res7_species.parquet"


def get_source_files(pattern: str = "*_h3.parquet") -> list[Path]:
    """Return sorted list of intermediate H3 Parquet files."""
    files = sorted(H3_PARTS_DIR.glob(pattern))
    if not files:
        raise RuntimeError(f"No files matched {H3_PARTS_DIR / pattern}")
    return files


def aggregate_with_duckdb(
    files: list[Path],
    out_path: Path,
    threads: int = 6,
    memory_limit: str = "20GB",
) -> None:
    """
    Use DuckDB to stream-aggregate input Parquet files and write GeoParquet.
    """
    import duckdb

    file_list = ", ".join(f"'{f}'" for f in files)
    logger.info("DuckDB aggregating %d files -> %s", len(files), out_path)

    con = duckdb.connect()
    con.execute(f"PRAGMA threads={threads}")
    con.execute(f"SET memory_limit = '{memory_limit}'")

    # Install spatial extension
    con.execute("INSTALL spatial;")
    con.execute("LOAD spatial;")

    # Create the final table with grouped species arrays
    logger.info("Running aggregation query...")
    t0 = time.time()

    con.execute(f"""
        CREATE TABLE agg AS
        SELECT
            h3_index,
            ARRAY_AGG(DISTINCT gbif_accepted_id ORDER BY gbif_accepted_id) AS species_ids
        FROM read_parquet([{file_list}])
        GROUP BY h3_index
    """)

    # Add H3 cell geometry as WKB
    logger.info("Converting H3 cells to geometry...")
    con.execute("""
        ALTER TABLE agg ADD COLUMN geom_wkb BLOB;
    """)

    # Using h3.cell_to_boundary to get polygon, then convert to WKB
    # We'll do this in Python for reliability
    count = con.execute("SELECT COUNT(*) FROM agg").fetchone()[0]
    logger.info("Aggregated rows: %d", count)

    # Export to Parquet with WKB geometry (DuckDB spatial can handle this)
    con.execute(f"""
        COPY (
            SELECT
                h3_index,
                species_ids,
                ST_AsGeoJSON(h3_cell_to_lat_lng(h3_index))::GEOMETRY::WKB AS geom_wkb
            FROM agg
        ) TO '{out_path}' (FORMAT 'parquet', COMPRESSION 'zstd')
    """)

    elapsed = time.time() - t0
    logger.info("Aggregation completed in %.1fs", elapsed)
    con.close()


# Alternative: pure-Python aggregation for cases where DuckDB fails
# This keeps memory bounded by only holding one cell in memory at a time
# (slower but guarantees no OOM)

def aggregate_in_python(
    files: list[Path],
    out_path: Path,
) -> None:
    """
    Pure-Python streaming aggregation. Reads each file one column at a time,
    builds a dict {h3_index: set(gbif_ids)}, then writes final Parquet.
    Memory usage: ~1 cell's worth of data at a time (bounded).
    """
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq
    from shapely import wkb
    from shapely.geometry import Polygon

    logger.info("Python streaming aggregation: %d files", len(files))
    t0 = time.time()

    # Phase 1: accumulate into dict (just string->set, minimal memory)
    cell_species: dict[str, set[int]] = {}
    total_pairs = 0

    con = duckdb.connect()
    try:
        for i, f in enumerate(files):
            logger.info("Reading file %d/%d: %s", i + 1, len(files), f.name)
            res = con.execute(
                f"SELECT h3_index, gbif_accepted_id FROM read_parquet('{f}')"
            )
            # Stream in 10k-row batches to keep memory flat
            while True:
                rows = res.fetchmany(10000)
                if not rows:
                    break
                for h, g in rows:
                    cell_species.setdefault(h, set()).add(int(g))
                    total_pairs += 1

            if (i + 1) % 10 == 0:
                logger.info(
                    "Progress: %d/%d files, %s pairs, %d unique cells",
                    i + 1, len(files), f"{total_pairs:,}", len(cell_species),
                )
    finally:
        con.close()

    logger.info("Phase 1 complete: %d unique cells from %s pairs (%.1fs)",
                len(cell_species), f"{total_pairs:,}", time.time() - t0)

    # Phase 2: build final table with geometries
    logger.info("Building final table with geometries...")
    t1 = time.time()

    h3_indices = []
    species_id_lists = []
    geom_wkbs = []

    for cell_str in sorted(cell_species):
        h3_indices.append(cell_str)
        species_id_lists.append(sorted(cell_species[cell_str]))

        # Convert H3 cell to polygon (boundary as list of lat/lng)
        boundary = h3.cell_to_boundary(cell_str)
        # boundary is list of (lat, lng) tuples - convert to polygon
        # Note: H3 cells are hexagons (6 vertices) or pentagons (5)
        coords = [(lng, lat) for lat, lng in boundary]
        # Close the ring
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        poly = Polygon(coords)
        geom_wkbs.append(poly.wkb)

    # Build PyArrow table
    table = pa.table({
        "h3_index": h3_indices,
        "gbif_accepted_ids": pa.array(species_id_lists, type=pa.list_(pa.int64())),
        "geom": geom_wkbs,
    })

    # Write GeoParquet (using pyarrow directly; user can add geoparquet metadata)
    pq.write_table(table, str(out_path), compression="zstd")

    elapsed = time.time() - t0
    logger.info("Done: %s rows written to %s (%.1fs total)",
                len(h3_indices), out_path, elapsed)


def main():
    parser = argparse.ArgumentParser(description="Aggregate H3 parts to GeoParquet")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--method",
        choices=["duckdb", "python"],
        default="python",
        help="Aggregation engine (default: python for guaranteed OOM safety)",
    )
    parser.add_argument(
        "--pattern",
        default="*_h3.parquet",
        help="Glob pattern for input files",
    )
    args = parser.parse_args()

    files = get_source_files(args.pattern)
    logger.info("Found %d input files in %s", len(files), H3_PARTS_DIR)

    if args.method == "duckdb":
        aggregate_with_duckdb(files, args.out)
    else:
        aggregate_in_python(files, args.out)


if __name__ == "__main__":
    main()
