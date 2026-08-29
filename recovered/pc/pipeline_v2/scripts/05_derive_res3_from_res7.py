#!/usr/bin/env python3
"""
Script 05: Derive H3 res3 cells from the res7 GeoParquet.

For each res7 cell:
  1. Compute its res3 parent (h3.cell_to_parent(..., 3))
  2. Map all child species ids up to the parent
  3. Deduplicate (union) species lists per res3 cell
  4. Generate boundary geometry for each res3 cell
  5. Write GeoParquet: data/global/h3_res3_species.parquet

This is intended to run AFTER Script 04 produces h3_res7_species.parquet.
"""
from __future__ import annotations

import argparse
import gc
import logging
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Paths (relative to repo root)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent
H3_PARTS_DIR = ROOT / "pipeline_v2" / "temp" / "h3_parts"
DATA_DIR = ROOT / "data" / "global"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_RES7 = DATA_DIR / "h3_res7_species.parquet"
DEFAULT_RES3 = DATA_DIR / "h3_res3_species.parquet"

# Chunk size for reading / writing (rows per chunk)
READ_CHUNK: int = 10_000
WRITE_FLUSH_COUNT: int = 500_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------
# Expected input columns from Script 04:
#   h3_index: string
#   gbif_accepted_ids: list<int64>
#   geom: binary (WKB) — ignored; we regenerate geometry from H3

def res3_schema() -> pa.Schema:
    """Output schema for the derived res3 GeoParquet."""
    return pa.schema([
        pa.field("h3_index", pa.string()),
        pa.field("gbif_accepted_ids", pa.list_(pa.int64())),
        pa.field("geom", pa.binary()),
    ])


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _parent(cell: str, res: int) -> str | None:
    try:
        return h3.cell_to_parent(cell, res)
    except Exception:
        return None


def _res3_boundary(cell: str) -> bytes:
    """Return WKB for the res3 H3 cell polygon."""
    from shapely.geometry import Polygon
    from shapely import wkb as shapely_wkb

    boundary = h3.cell_to_boundary(cell)
    # boundary: list of (lat, lng)
    coords = [(lng, lat) for lat, lng in boundary]
    # Close ring
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    poly = Polygon(coords)
    return shapely_wkb.dumps(poly)


def derive_res3(
    res7_path: Path,
    res3_path: Path,
    read_chunk: int = READ_CHUNK,
    write_flush: int = WRITE_FLUSH_COUNT,
) -> dict[str, Any]:
    """
    Build the res3 GeoParquet from a res7 GeoParquet.

    Strategy
    --------
    We do a classic two-pass streaming derivation because we must deduplicate
    species ids across all res7 children of each res3 parent. This is bounded
    by the number of res7 cells (~39K for Denmark), so it fits comfortably in
    RAM. For global scale, peak memory is ~|res7_cells| * avg_species_per_cell.
    """
    if not res7_path.exists():
        raise FileNotFoundError(f"Input res7 GeoParquet not found: {res7_path}")

    t0 = time.time()
    # Phase 1: accumulate {res3_cell -> set(species_id)}
    logger.info("Phase 1: reading res7 input and mapping to res3 parents...")
    pf = pq.ParquetFile(res7_path)

    # res3_cell -> set(species_id)
    species_by_res3: dict[str, set[int]] = defaultdict(set)
    total_res7_read = 0

    for i, batch in enumerate(pf.iter_batches(batch_size=read_chunk)):
        dict_batch = batch.to_pydict()
        h3_indices = dict_batch["h3_index"]
        species_lists = dict_batch["gbif_accepted_ids"]

        for h7_cell, sp_list in zip(h3_indices, species_lists):
            total_res7_read += 1
            parent = _parent(h7_cell, 3)
            if parent is None:
                continue
            species_by_res3[parent].update(int(s) for s in sp_list)

        if (i + 1) % 10 == 0:
            logger.info(
                "  ... read %d batches, %d res7 rows -> %d unique res3 cells",
                i + 1, total_res7_read, len(species_by_res3),
            )

    pf.close()
    logger.info(
        "Phase 1 complete: %d res7 cells -> %d unique res3 cells (%.1fs)",
        total_res7_read, len(species_by_res3), time.time() - t0,
    )

    # Phase 2: build & write output
    logger.info("Phase 2: writing res3 GeoParquet with geometries...")
    t1 = time.time()
    schema = res3_schema()
    writer = pq.ParquetWriter(str(res3_path), schema, compression="zstd")

    h3_indices: list[str] = []
    species_lists: list[list[int]] = []
    geoms: list[bytes] = []
    rows_written = 0

    for res3_cell in sorted(species_by_res3.keys()):
        species = sorted(species_by_res3[res3_cell])
        h3_indices.append(res3_cell)
        species_lists.append(species)
        geoms.append(_res3_boundary(res3_cell))

        if len(h3_indices) >= write_flush:
            table = pa.table({
                "h3_index": h3_indices,
                "gbif_accepted_ids": species_lists,
                "geom": geoms,
            }, schema=schema)
            writer.write_table(table)
            rows_written += len(h3_indices)
            h3_indices.clear()
            species_lists.clear()
            geoms.clear()
            gc.collect()

    if h3_indices:
        table = pa.table({
            "h3_index": h3_indices,
            "gbif_accepted_ids": species_lists,
            "geom": geoms,
        }, schema=schema)
        writer.write_table(table)
        rows_written += len(h3_indices)

    writer.close()
    elapsed_total = time.time() - t0
    logger.info(
        "Done: %d res3 rows written to %s (%.1fs total)",
        rows_written, res3_path, elapsed_total,
    )

    return {
        "input_path": str(res7_path),
        "output_path": str(res3_path),
        "res7_cells_read": total_res7_read,
        "res3_cells_written": rows_written,
        "elapsed_sec": round(elapsed_total, 2),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Derive res3 GeoParquet from res7")
    parser.add_argument(
        "--res7",
        type=Path,
        default=DEFAULT_RES7,
        help=f"Input res7 GeoParquet (default: {DEFAULT_RES7})",
    )
    parser.add_argument(
        "--res3",
        type=Path,
        default=DEFAULT_RES3,
        help=f"Output res3 GeoParquet (default: {DEFAULT_RES3})",
    )
    parser.add_argument(
        "--read-chunk",
        type=int,
        default=READ_CHUNK,
        help=f"Rows per read batch (default: {READ_CHUNK})",
    )
    parser.add_argument(
        "--write-flush",
        type=int,
        default=WRITE_FLUSH_COUNT,
        help=f"Flush output after this many rows (default: {WRITE_FLUSH_COUNT})",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("SCRIPT 05: DERIVE RES3 FROM RES7")
    logger.info("=" * 60)

    result = derive_res3(
        res7_path=args.res7,
        res3_path=args.res3,
        read_chunk=args.read_chunk,
        write_flush=args.write_flush,
    )

    logger.info("Result summary: %s", result)


if __name__ == "__main__":
    main()
