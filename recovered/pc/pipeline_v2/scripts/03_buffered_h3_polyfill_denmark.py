#!/usr/bin/env python3
"""
03_buffered_h3_polyfill_denmark.py  —  Optimised Denmark-only Pipeline

Based on preclassify results (126,533 species):
  • Fast path: 77,532 species (61.3%) — direct h3.geo_to_cells(), no simplification.
  • Slow path: 49,001 species (38.7%) — simplify(0.01°) FIRST, then polyfill.
    Benchmark proved this yields Jaccard≈0.98, only ~1% cell error, ~5x speedup.

NEVER tries direct polyfill on slow species — this was the main bottleneck.
Stream-writes to Parquet so memory stays flat.
Atomic temp-file + rename for crash safety.
Uses preclassify JSONL to avoid redundant inline geometry analysis.

Denmark-only variant:
  • Loads the actual Denmark boundary from
    data/sample/denmark_prototype/denmark_boundary.parquet
  • Filters species ranges by real spatial intersection (not a rough BBOX).
  • Always writes output to pipeline_v2/temp/h3_parts/denmark/.
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely import make_valid, simplify
from shapely import wkb as shapely_wkb
from shapely.geometry import MultiPolygon
from shapely.geometry.base import BaseGeometry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
H3_RES = 7
SLOW_TOL_DEG = 0.01
WRITE_BATCH = 200_000
MAX_PENDING_FACTOR = 4
READ_BATCH_ROWS = 10_000

ROOT = Path(__file__).resolve().parent.parent.parent
PARTS_DIR = ROOT / "pipeline_v2" / "temp" / "_unified_parts"
H3_PARTS_DIR = ROOT / "pipeline_v2" / "temp" / "h3_parts"
PRECLASSIFY_PATH = ROOT / "pipeline_v2" / "temp" / "_preclassify_species.jsonl"
DENMARK_BND_PATH = ROOT / "data" / "sample" / "denmark_prototype" / "denmark_boundary.parquet"
H3_PARTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Boundary loader
# ---------------------------------------------------------------------------
def _load_denmark_boundary() -> tuple[BaseGeometry, BaseGeometry]:
    """Load Denmark boundary from GeoParquet and union if multi-row.

    Returns (original, buffered) where buffer is ~0.05° (~5 km) to keep
    cells that the grid-ring expansion would later add.
    """
    if not DENMARK_BND_PATH.exists():
        raise FileNotFoundError(f"Denmark boundary not found: {DENMARK_BND_PATH}")

    table = pq.read_table(str(DENMARK_BND_PATH))

    geom_col_name = "geometry"
    if geom_col_name not in table.schema.names:
        for col in table.schema.names:
            if "geom" in col.lower():
                geom_col_name = col
                break
        else:
            raise KeyError(f"No geometry column found in {DENMARK_BND_PATH}")

    geom_col = table.column(geom_col_name)
    parts = []
    for wkb in geom_col.to_pylist():
        if wkb is not None and len(wkb) > 0:
            try:
                parts.append(shapely.from_wkb(wkb))
            except Exception:
                pass

    if not parts:
        raise ValueError("No valid geometry found in Denmark boundary file")

    if len(parts) == 1:
        geom = parts[0]
    else:
        geom = shapely.unary_union(parts)

    buffered = shapely.buffer(geom, 0.05)  # ~5 km buffer
    if not buffered.is_valid:
        buffered = make_valid(buffered)
    logger.info(
        "Denmark boundary loaded: %s (buffered %s)",
        geom.geom_type,
        buffered.geom_type,
    )
    return geom, buffered


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _extract(geom: BaseGeometry) -> BaseGeometry | None:
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
                inner = _extract(g)
                if inner is not None:
                    parts.append(inner)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


# ---------------------------------------------------------------------------
# H3 helpers
# ---------------------------------------------------------------------------
def _h3cells(geom: BaseGeometry) -> set[str] | None:
    try:
        return set(h3.geo_to_cells(geom.__geo_interface__, res=H3_RES))
    except Exception:
        return None


def _expand_boundary(cells: set[str]) -> set[str]:
    if not cells:
        return cells
    expanded = set(cells)
    for c in cells:
        ring = h3.grid_ring(c, 1)
        if any(n not in cells for n in ring):
            expanded.update(ring)
    return expanded


# ---------------------------------------------------------------------------
# Per-species pipeline (uses pre-classified fast/slow flag; no inline verts/diag)
# ---------------------------------------------------------------------------
def process_species(wkb: bytes, gbif_id: int, is_fast: bool, clip_geom: BaseGeometry | None = None) -> set[str] | None:
    try:
        geom = shapely_wkb.loads(wkb)
    except Exception:
        return None
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        try:
            geom = make_valid(geom)
        except Exception:
            return None
        if geom is None or geom.is_empty:
            return None
    geom = _extract(geom)
    if geom is None or geom.is_empty:
        return None

    # CRITICAL: clip to buffered Denmark boundary before H3. Without this,
    # a global tuna polygon produces millions of cells outside Denmark.
    if clip_geom is not None:
        try:
            geom = shapely.intersection(geom, clip_geom)
        except Exception:
            return None
        geom = _extract(geom)
        if geom is None or geom.is_empty:
            return set()

    if is_fast:
        cells = _h3cells(geom)
    else:
        try:
            simp = simplify(geom, SLOW_TOL_DEG)
        except Exception:
            return None
        simp = _extract(simp)
        if simp is None or simp.is_empty:
            return None
        cells = _h3cells(simp)

    if cells is None:
        return None
    if not cells:
        return set()
    return _expand_boundary(cells)


# ---------------------------------------------------------------------------
# Adaptive worker count
# ---------------------------------------------------------------------------
def _choose_worker_count(tasks: list[tuple[bytes, int, bool]]) -> int:
    if not tasks:
        return 1
    fast_count = sum(1 for _, _, is_fast in tasks if is_fast)
    fast_ratio = fast_count / len(tasks)

    if fast_ratio >= 0.8:
        return 6
    return 2


# ---------------------------------------------------------------------------
# Bounded task runner
# ---------------------------------------------------------------------------
def _run_tasks_bounded(
    tasks: list[tuple[bytes, int, bool]],
    workers: int,
    clip_geom: BaseGeometry | None = None,
):
    max_pending = max(workers * MAX_PENDING_FACTOR, workers)
    task_iter = iter(tasks)
    pending = {}

    with ThreadPoolExecutor(max_workers=workers) as tpe:
        for _ in range(max_pending):
            task = next(task_iter, None)
            if task is None:
                break
            wkb, gid, is_fast = task
            fut = tpe.submit(process_species, wkb, gid, is_fast, clip_geom)
            pending[fut] = (gid, is_fast)

        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                gid, is_fast = pending.pop(fut)
                try:
                    cells = fut.result()
                except Exception:
                    cells = None
                yield gid, is_fast, cells

                task = next(task_iter, None)
                if task is not None:
                    wkb, next_gid, next_is_fast = task
                    next_fut = tpe.submit(
                        process_species,
                        wkb,
                        next_gid,
                        next_is_fast,
                        clip_geom,
                    )
                    pending[next_fut] = (next_gid, next_is_fast)


# ---------------------------------------------------------------------------
# Denmark filter — uses real polygon intersection
# ---------------------------------------------------------------------------
def _filter_denmark(df: pd.DataFrame, boundary_geom: BaseGeometry) -> pd.DataFrame:
    if df.empty:
        return df

    # 1. Fast bbox pre-filter (boundary bbox)
    dminx, dminy, dmaxx, dmaxy = shapely.bounds(boundary_geom)

    CHUNK = 1_000
    keep_idx: list[int] = []

    for start in range(0, len(df), CHUNK):
        end = min(start + CHUNK, len(df))
        chunk_df = df.iloc[start:end]
        wkb_values = chunk_df["geom"].to_numpy()

        try:
            geoms = shapely.from_wkb(wkb_values, on_invalid="ignore")
        except TypeError:
            geoms = shapely.from_wkb(wkb_values)

        bounds = shapely.bounds(geoms)
        mask = ~(
            (bounds[:, 2] < dminx)
            | (bounds[:, 0] > dmaxx)
            | (bounds[:, 3] < dminy)
            | (bounds[:, 1] > dmaxy)
        )
        mask &= ~pd.isna(bounds[:, 0])

        # 2. Accurate polygon intersection for bbox candidates
        if mask.any():
            candidate_geoms = geoms[mask]
            inter = shapely.intersects(candidate_geoms, boundary_geom)
            inter_mask = np.zeros(len(chunk_df), dtype=bool)
            inter_mask[np.where(mask)[0][inter]] = True
            mask = inter_mask

        keep_idx.extend(chunk_df.index[mask].tolist())
        del geoms, bounds  # free C++ memory aggressively

    return df.loc[keep_idx]


# ---------------------------------------------------------------------------
# Task building
# ---------------------------------------------------------------------------
def _build_tasks_from_df(
    df: pd.DataFrame,
    fast_lookup: dict[int, bool],
) -> list[tuple[bytes, int, bool]]:
    if df.empty:
        return []
    geoms = df["geom"].to_numpy()
    gids = df["gbif_accepted_id"].to_numpy()
    mask = pd.notna(geoms) & pd.notna(gids)
    tasks: list[tuple[bytes, int, bool]] = []
    for i in mask.nonzero()[0]:
        gid = int(gids[i])
        tasks.append((geoms[i], gid, fast_lookup.get(gid, True)))
    return tasks


# ---------------------------------------------------------------------------
# Task processing
# ---------------------------------------------------------------------------
def _process_tasks(
    tasks: list[tuple[bytes, int, bool]],
    writer: pq.ParquetWriter,
    schema: pa.Schema,
    buffer: list[tuple[str, int]],
    clip_geom: BaseGeometry | None = None,
    file_id: str = "",
) -> tuple[int, int, int]:
    if not tasks:
        return 0, 0, 0

    workers = _choose_worker_count(tasks)
    species = len(tasks)
    total_pairs = 0
    errors = 0
    processed = 0

    for gid, _, cells in _run_tasks_bounded(tasks, workers, clip_geom):
        processed += 1
        if cells is None:
            errors += 1
        elif cells:
            buffer.extend((cell, gid) for cell in cells)
            total_pairs += len(cells)
            if len(buffer) >= WRITE_BATCH:
                _flush(buffer, schema, writer)

        # single updating line per file
        if processed % 5 == 0 or processed == species:
            print(
                f"\r[{file_id}] species {processed}/{species}, {total_pairs:,} pairs, {errors} errors",
                end="",
                flush=True,
            )

    print()  # newline after file finishes
    return species, total_pairs, errors


def _flush(buffer, schema, writer) -> None:
    if not buffer:
        return
    h3s, gids = zip(*buffer)
    table = pa.Table.from_pydict(
        {
            "h3_index": list(h3s),
            "gbif_accepted_id": list(gids),
        },
        schema=schema,
    )
    writer.write_table(table)
    buffer.clear()


def process_file(
    input_path: Path,
    output_path: Path,
    fast_lookup: dict[int, bool],
    boundary_geom: BaseGeometry,
    clip_geom: BaseGeometry,
) -> dict:
    t0 = time.time()
    res = {
        "input": str(input_path),
        "output": str(output_path),
        "rows": 0,
        "species": 0,
        "h3_pairs": 0,
        "errors": 0,
        "elapsed_sec": 0.0,
        "status": "pending",
    }

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    if output_path.exists():
        try:
            pq.read_metadata(str(output_path))
            logger.info("SKIP: %s", output_path.name)
            res["status"] = "skipped"
            return res
        except Exception as e:
            logger.warning("CORRUPT output %s (%s). Re-processing.", output_path.name, e)
            try:
                os.remove(output_path)
            except OSError:
                pass

    if tmp_path.exists():
        logger.warning("Removing orphaned temp file %s", tmp_path.name)
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    logger.info("START: %s", input_path.name)
    file_id = input_path.stem[:20]  # short label for progress line

    try:
        parquet_file = pq.ParquetFile(str(input_path))
    except Exception as e:
        logger.error("Read error %s: %s", input_path.name, e)
        res["status"] = "read_error"
        return res

    total_rows = parquet_file.metadata.num_rows
    res["rows"] = total_rows

    if total_rows == 0:
        res["status"] = "empty"
        return res

    schema = pa.schema(
        [
            pa.field("h3_index", pa.string()),
            pa.field("gbif_accepted_id", pa.int64()),
        ]
    )

    writer = None
    buffer: list[tuple[str, int]] = []
    total_species = 0
    total_pairs = 0
    total_errors = 0
    denmark_kept = 0

    try:
        writer = pq.ParquetWriter(str(tmp_path), schema, compression="zstd")

        for batch in parquet_file.iter_batches(
            batch_size=READ_BATCH_ROWS,
            columns=["geom", "gbif_accepted_id"],
        ):
            df = batch.to_pandas()

            before = len(df)
            df = _filter_denmark(df, boundary_geom)
            denmark_kept += len(df)

            if df.empty:
                continue

            tasks = _build_tasks_from_df(df, fast_lookup)

            species, pairs, errors = _process_tasks(
                tasks,
                writer,
                schema,
                buffer,
                clip_geom,
                file_id,
            )

            total_species += species
            total_pairs += pairs
            total_errors += errors

            del df, tasks

        _flush(buffer, schema, writer)
        writer.close()
        os.replace(str(tmp_path), str(output_path))

    except Exception as e:
        logger.error("Write/rename error for %s: %s", output_path.name, e)
        res["status"] = "write_error"

        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass

        try:
            if tmp_path.exists():
                os.remove(tmp_path)
        except OSError:
            pass

        return res

    res["species"] = total_species
    res["h3_pairs"] = total_pairs
    res["errors"] = total_errors
    res["elapsed_sec"] = round(time.time() - t0, 2)

    if denmark_kept == 0:
        res["status"] = "no_denmark"
    elif total_pairs == 0:
        res["status"] = "no_cells"
    else:
        logger.info(
            "DONE: %s -> %d pairs (%.1fs)",
            input_path.name,
            total_pairs,
            res["elapsed_sec"],
        )
        res["status"] = "success"

    del buffer
    gc.collect()
    return res


def _load_preclassify(path: Path) -> dict[int, bool]:
    """Load preclassify JSONL into {gbif_id: is_fast} dict.

    If a species appears in multiple source files, mark it slow if ANY
    source says slow. Slow -> fast is safe; fast -> slow is the
    performance-killing downgrade.
    """
    lookup: dict[int, bool] = {}
    if not path.exists():
        logger.warning("Preclassify file not found: %s — falling back to inline classification", path)
        return lookup
    t0 = time.time()
    duplicates = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                gid = int(rec["gbif_accepted_id"])
            except (json.JSONDecodeError, KeyError):
                continue
            is_fast = rec.get("fast", True)
            if gid in lookup:
                duplicates += 1
                # Conservative: one slow classification overrides all fast ones.
                if not is_fast:
                    lookup[gid] = False
            else:
                lookup[gid] = is_fast
    logger.info(
        "Loaded preclassify: %d unique species (%d duplicates) in %.1fs",
        len(lookup), duplicates, time.time() - t0,
    )
    return lookup


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="H3 Polyfill — Denmark Only (boundary-clipped)")
    parser.add_argument(
        "--files",
        nargs="*",
        help="Specific Parquet part files to process (relative to _unified_parts).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="denmark_v2",
        help="Output subdirectory under pipeline_v2/temp/h3_parts/ (default: denmark_v2)",
    )
    args = parser.parse_args()

    out_dir = H3_PARTS_DIR / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "_processing_summary.json"

    fast_lookup = _load_preclassify(PRECLASSIFY_PATH)
    denmark_boundary, denmark_buffered = _load_denmark_boundary()
    logger.info("Denmark boundary loaded: %s", denmark_boundary.geom_type)

    if args.files:
        input_files = [PARTS_DIR / f for f in args.files]
        missing = [f for f in input_files if not f.exists()]
        if missing:
            print("Missing:", ", ".join(str(f.name) for f in missing), file=sys.stderr)
            sys.exit(1)
    else:
        input_files = sorted(PARTS_DIR.glob("*.parquet"))
        done_names = {
            p.name.removesuffix("_h3.parquet")
            for p in out_dir.glob("*_h3.parquet")
        }
        input_files = [f for f in input_files if f.stem not in done_names]
        logger.info("Skipping %d already-complete files", len(done_names))

    logger.info("Files to process: %d", len(input_files))
    logger.info("MODE: Denmark only (boundary-clipped), output dir: %s", out_dir.name)

    outputs = [(f, out_dir / (f.stem + "_h3.parquet")) for f in input_files]

    results = []
    total_files = len(outputs)
    for file_idx, (inp, out) in enumerate(outputs, start=1):
        print(f"\n[File {file_idx}/{total_files}] {inp.name}", flush=True)
        r = process_file(inp, out, fast_lookup, denmark_boundary, denmark_buffered)
        results.append(r)
        _write_summary(results, outputs, summary_path)

    logger.info("=" * 60)
    logger.info("FINAL SUMMARY")
    logger.info("Files: %d", len(results))
    logger.info("Statuses: %s", {s: sum(1 for r in results if r.get("status") == s) for s in set(r.get("status", "unknown") for r in results)})
    logger.info("Species: %d", sum(r.get("species", 0) for r in results))
    logger.info("H3 pairs: %d", sum(r.get("h3_pairs", 0) for r in results))
    logger.info("Time: %.1fs", sum(r.get("elapsed_sec", 0) for r in results))


def _write_summary(results: list[dict], outputs: list[tuple], summary_path: Path) -> None:
    summary = {
        "total_files": len(outputs),
        "processed": len(results),
        "statuses": {},
        "total_species": sum(x.get("species", 0) for x in results),
        "total_pairs": sum(x.get("h3_pairs", 0) for x in results),
        "total_elapsed_sec": sum(x.get("elapsed_sec", 0) for x in results),
        "details": results,
    }
    for x in results:
        s = x.get("status", "unknown")
        summary["statuses"][s] = summary["statuses"].get(s, 0) + 1
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2, default=str)


if __name__ == "__main__":
    main()
