#!/usr/bin/env python3
"""
Pre-classify all species across all 51 source Parquet files.

For every row, read WKB once and compute:
  - vertex_count
  - wkb_bytes
  - bbox_diagonal_deg
  - geom_type
  - fast_flag

Output:
  _preclassify_summary.json      — per-file summary stats
  _preclassify_species.jsonl     — one line per species with classification

This script is intentionally single-threaded (sequential over files) to
avoid the Windows GIL/threading issues seen in the pipeline. It reads
only the necessary columns and uses pyarrow chunked iteration for speed.
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq
from shapely import wkb as shapely_wkb
from shapely.geometry import MultiPolygon

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

MAX_VERTICES_FAST = 5000
MAX_WKB_BYTES_FAST = 300_000
MAX_BBOX_DIAG_FAST_DEG = 30.0

ROOT = Path(__file__).resolve().parent.parent.parent
PARTS_DIR = ROOT / "pipeline_v2" / "temp" / "_unified_parts"
OUT_DIR = ROOT / "pipeline_v2" / "temp"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _extract_polygons(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = []
    for g in getattr(geom, "geoms", [geom]):
        if g.geom_type == "Polygon":
            parts.append(g)
        elif g.geom_type == "MultiPolygon":
            parts.extend(g.geoms)
    if not parts and geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            if g.geom_type in ("Polygon", "MultiPolygon"):
                parts.append(g)
            elif g.geom_type == "GeometryCollection":
                inner = _extract_polygons(g)
                if inner is not None:
                    parts.append(inner)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


def _vertex_count(geom) -> int:
    if geom is None or geom.is_empty:
        return 0
    n = 0
    for p in (geom.geoms if hasattr(geom, "geoms") else [geom]):
        if p.geom_type == "Polygon":
            n += len(p.exterior.coords)
            for ir in p.interiors:
                n += len(ir.coords)
        elif p.geom_type == "MultiPolygon":
            n += _vertex_count(p)
    return n


def _bbox_diagonal(geom) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    minx, miny, maxx, maxy = geom.bounds
    return math.hypot(max(0.0, maxx - minx), max(0.0, maxy - miny))


def classify_file(path: Path) -> tuple[list[dict], dict]:
    """Classify every species in a single file. Returns (species_list, summary)."""
    logger.info("SCAN: %s", path.name)
    t0 = time.time()

    species_list = []
    bad_rows = 0

    pf = pq.ParquetFile(str(path))
    n_total_rows = pf.metadata.num_rows

    for rg in range(pf.num_row_groups):
        table = pf.read_row_group(rg, columns=["geom", "gbif_accepted_id"])
        geom_col = table.column("geom")
        gbif_col = table.column("gbif_accepted_id")

        for i in range(len(table)):
            wkb = geom_col[i].as_py() if hasattr(geom_col[i], "as_py") else geom_col[i]
            gbif_id = gbif_col[i].as_py() if hasattr(gbif_col[i], "as_py") else gbif_col[i]
            if wkb is None or gbif_id is None:
                bad_rows += 1
                continue

            geom = _extract_polygons(shapely_wkb.loads(wkb))
            if geom is None:
                bad_rows += 1
                continue

            verts = _vertex_count(geom)
            wkb_size = len(wkb) if isinstance(wkb, bytes) else 0
            diag = _bbox_diagonal(geom)
            fast = (
                verts <= MAX_VERTICES_FAST
                and wkb_size <= MAX_WKB_BYTES_FAST
                and diag <= MAX_BBOX_DIAG_FAST_DEG
            )

            species_list.append({
                "gbif_accepted_id": int(gbif_id),
                "file": path.name,
                "verts": verts,
                "wkb_bytes": wkb_size,
                "bbox_diag_deg": round(diag, 4),
                "geom_type": geom.geom_type,
                "fast": fast,
            })

    elapsed = time.time() - t0
    fast_count = sum(1 for s in species_list if s["fast"])
    slow_count = len(species_list) - fast_count
    slow_species = [s for s in species_list if not s["fast"]]

    summary = {
        "file": path.name,
        "rows_total": n_total_rows,
        "rows_scanned": len(species_list),
        "bad_rows": bad_rows,
        "fast_count": fast_count,
        "slow_count": slow_count,
        "slow_verts_max": max((s["verts"] for s in slow_species), default=0),
        "slow_wkb_max": max((s["wkb_bytes"] for s in slow_species), default=0),
        "slow_diag_max": max((s["bbox_diag_deg"] for s in slow_species), default=0),
        "slow_verts_avg": round(sum(s["verts"] for s in slow_species) / slow_count, 1) if slow_count else 0,
        "elapsed_sec": round(elapsed, 2),
    }

    logger.info(
        "  DONE: %s -> %d scanned (%d fast, %d slow) in %.1fs",
        path.name, len(species_list), fast_count, slow_count, elapsed,
    )
    return species_list, summary


def main() -> None:
    files = sorted(PARTS_DIR.glob("*.parquet"))
    n_files = len(files)
    logger.info("Files to scan: %d", n_files)
    logger.info("Writing outputs to %s", OUT_DIR)

    summary_path = OUT_DIR / "_preclassify_summary.json"
    species_path = OUT_DIR / "_preclassify_species.jsonl"

    # Open species file for append
    with open(species_path, "w") as sp_out:
        summaries = []
        for idx, f in enumerate(files, 1):
            t_start = time.time()
            species_list, summary = classify_file(f)
            summaries.append(summary)

            # Write species lines immediately
            for s in species_list:
                sp_out.write(json.dumps(s) + "\n")

            # Write summary incrementally
            with open(summary_path, "w") as fh:
                json.dump({
                    "total_files": len(summaries),
                    "total_species": sum(s["rows_scanned"] for s in summaries),
                    "total_fast": sum(s["fast_count"] for s in summaries),
                    "total_slow": sum(s["slow_count"] for s in summaries),
                    "by_file": summaries,
                }, fh, indent=2, default=str)

            elapsed_total = time.time() - t_start
            remaining = n_files - idx
            if remaining > 0:
                logger.info("  Progress: %d/%d files done, %d remaining", idx, n_files, remaining)

    logger.info("=" * 60)
    logger.info("COMPLETE")
    logger.info("Summary: %s", summary_path)
    logger.info("Species: %s", species_path)
    logger.info("Files: %d", len(summaries))
    logger.info("Total species: %d", sum(s["rows_scanned"] for s in summaries))
    logger.info("Fast: %d", sum(s["fast_count"] for s in summaries))
    logger.info("Slow: %d", sum(s["slow_count"] for s in summaries))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
