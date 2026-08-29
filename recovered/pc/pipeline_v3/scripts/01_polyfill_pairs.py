#!/usr/bin/env python3
"""
01_polyfill_pairs.py  —  Polyfill the 7 source GeoParquet files into
                          raw (h3_index, id_no) pairs at H3 res 7.

§5 Steps A-D of GLOBAL_H3_RESET_PLAN.md.

This script does NOT compute metrics. It produces the raw pair relation
that scripts 02 and 04 consume:
  - 02_merge_metrics.py        → h3_res7_metrics.parquet  (per-cell counts)
  - 04_partition_species_lists.py → h3_res7_species/        (drill-down dataset)

For each of the 7 already-joined GeoParquet files in ``ARK_GEODATA_DIR``:
  1. Read rows with the Step A filter (presence=1, origin IN (1,2), geom_wkb
     IS NOT NULL). In --denmark mode, DuckDB does a bbox pre-filter first
     (parallel, C++, memory-managed) so only ~1-5% of rows reach Python.
  2. Polyfill each geometry at H3 res 7 using the frozen kernel
     (polyfill.DEFAULT_KERNEL = "tile_clip", exact, zero delta, handles
     antimeridian). Use --fast for the routed lossy kernel
     (coarse_refine_simp on large polygons, simplify on small; 3-11x
     faster than the old simplify kernel on large-bbox polygons,
     ~0.7% cell delta). Polyfill runs in a multiprocessing pool (h3
     holds the GIL, so threads don't parallelise).
  3. Stream raw (h3_index, id_no) pairs to
     pipeline_v3/temp/h3_pairs/class=<key>.parquet. No per-chunk Python
     dedup set — deduplication is out-of-core in DuckDB later (§5 Step C).
  4. Stream-write + atomic rename so memory stays flat and crashes are
     recoverable. Already-complete intermediate files are skipped on
     restart (§5 Step D).

Usage:
    uv run python pipeline_v3/scripts/01_polyfill_pairs.py
    uv run python pipeline_v3/scripts/01_polyfill_pairs.py --files amphibians
    uv run python pipeline_v3/scripts/01_polyfill_pairs.py --fast       # routed lossy kernel
    uv run python pipeline_v3/scripts/01_polyfill_pairs.py --denmark    # validation subset
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

import duckdb
import h3
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polyfill  # noqa: E402
from polyfill import polyfill_row  # noqa: E402 — picklable by ProcessPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


PAIRS_SCHEMA = pa.schema([
    pa.field("h3_index", pa.string()),
    pa.field("id_no", pa.int64()),
])


# ---------------------------------------------------------------------------
# Stream-read source rows via DuckDB (pushdown filter at Parquet reader)
# ---------------------------------------------------------------------------
def _stream_pyarrow(
    path: Path,
    batch_size: int,
):
    """Stream any Parquet file with the Step A filter applied."""
    pf = pq.ParquetFile(path)
    needed = [c for c in config.SOURCE_COLUMNS if c in pf.schema_arrow.names]
    for batch in pf.iter_batches(batch_size=batch_size, columns=needed):
        if batch.num_rows == 0:
            continue
        pyd = batch.to_pydict()
        n = batch.num_rows
        presence = pyd.get("presence", [1] * n)
        origin = pyd.get("origin", [1] * n)
        geom_wkb = pyd.get("geom_wkb", [None] * n)
        keep = [
            i for i in range(n)
            if presence[i] == 1
            and origin[i] in (1, 2)
            and geom_wkb[i] is not None
            and len(geom_wkb[i]) > 0
        ]
        if not keep:
            continue
        filtered_cols = {col: [pyd[col][i] for i in keep] for col in pyd}
        yield pa.Table.from_pydict(filtered_cols)


def stream_source_rows(
    source: Path,
    batch_size: int = config.READ_BATCH_ROWS,
):
    """Yield Arrow batches of source rows that pass the Step A filter.

    Uses pyarrow's ParquetFile.iter_batches to stream columns directly from
    the Parquet file. This is used for the GLOBAL (unclipped) run where every
    row is a candidate — no bbox pre-filter is possible, so we stream all rows.

    The presence/origin filter is applied in Python per batch (cheap integer
    comparison).
    """
    pf = pq.ParquetFile(source)
    needed = [c for c in config.SOURCE_COLUMNS if c in pf.schema_arrow.names]
    for batch in pf.iter_batches(batch_size=batch_size, columns=needed):
        if batch.num_rows == 0:
            continue
        pyd = batch.to_pydict()
        n = batch.num_rows
        # Step A filter: presence=1 AND origin IN (1,2) AND geom_wkb IS NOT NULL.
        presence = pyd.get("presence", [1] * n)
        origin = pyd.get("origin", [1] * n)
        geom_wkb = pyd.get("geom_wkb", [None] * n)
        keep = [
            i for i in range(n)
            if presence[i] == 1
            and origin[i] in (1, 2)
            and geom_wkb[i] is not None
            and len(geom_wkb[i]) > 0
        ]
        if not keep:
            continue
        filtered_cols = {col: [pyd[col][i] for i in keep] for col in pyd}
        yield pa.Table.from_pydict(filtered_cols)


# ---------------------------------------------------------------------------
# DuckDB bbox-filtered candidates (for --denmark clip mode)
# ---------------------------------------------------------------------------
def write_clip_candidates(
    source: Path,
    clip_bbox: tuple[float, float, float, float],
    out_path: Path,
    threads: int = 2,
) -> int:
    """Use DuckDB to filter the source file by bbox and write candidates to a
    small temp Parquet. DuckDB handles the 16 GB WKB files internally in C++
    (multithreaded, memory-managed, spills to disk) — no Arrow appender
    overflow and no Python OOM.

    Returns the number of candidate rows written.
    """
    cminx, cminy, cmaxx, cmaxy = clip_bbox
    cols = ", ".join(config.SOURCE_COLUMNS)
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={threads}")
    con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory='{config.TEMP_DIR.as_posix()}'")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"""
        COPY (
            SELECT {cols}
            FROM read_parquet('{source.as_posix()}')
            WHERE {config.SOURCE_FILTER_SQL}
              AND ST_XMax(ST_GeomFromWKB(geom_wkb)) >= {cminx}
              AND ST_XMin(ST_GeomFromWKB(geom_wkb)) <= {cmaxx}
              AND ST_YMax(ST_GeomFromWKB(geom_wkb)) >= {cminy}
              AND ST_YMin(ST_GeomFromWKB(geom_wkb)) <= {cmaxy}
        ) TO '{out_path.as_posix()}'
        (FORMAT 'parquet', COMPRESSION 'zstd')
    """)
    n = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{out_path.as_posix()}')"
    ).fetchone()[0]
    con.close()
    return n


# ---------------------------------------------------------------------------
# Bounded task runner (mirrors 03_buffered_h3_polyfill_denmark.py)
# ---------------------------------------------------------------------------
def _run_bounded(
    tasks: list[tuple[bytes, int]],
    executor: ProcessPoolExecutor,
    workers: int,
    kernel: str,
    clip_geom=None,
    max_pending_factor: int = 4,
):
    """Submit polyfill tasks to a process pool and yield (id_no, cells) as
    they complete.

    Uses a bounded queue (max_pending = workers * max_pending_factor) so
    memory stays flat even with huge WKB blobs. The executor is owned by
    the caller (created once per source file, reused across batches) to
    avoid the ~1-2s per-process spawn cost on Windows.
    """
    max_pending = max(workers * max_pending_factor, workers)
    task_iter = iter(tasks)
    pending: dict = {}

    for _ in range(max_pending):
        task = next(task_iter, None)
        if task is None:
            break
        wkb, id_no = task
        fut = executor.submit(polyfill_row, wkb, kernel, clip_geom)
        pending[fut] = id_no

    while pending:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        for fut in done:
            id_no = pending.pop(fut)
            try:
                cells = fut.result()
            except Exception:
                cells = None
            yield id_no, cells

            task = next(task_iter, None)
            if task is not None:
                wkb, next_id_no = task
                next_fut = executor.submit(polyfill_row, wkb, kernel, clip_geom)
                pending[next_fut] = next_id_no


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------
def _fmt_eta(seconds: float) -> str:
    """Format seconds into a human-readable ETA string."""
    if seconds < 0 or seconds != seconds:  # NaN check
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def _fmt_count(n: int) -> str:
    """Abbreviate large numbers: 168,716,387 -> 168.7M."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}K"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000_000:.2f}B"


def _print_progress(
    file_idx: int,
    n_files: int,
    source_key: str,
    rows_done: int,
    rows_total: int,
    pairs: int,
    errors: int,
    elapsed: float,
    final: bool = False,
    grand_rows_done: int = 0,
    grand_rows_total: int = 0,
    grand_elapsed: float = 0.0,
) -> None:
    """Print a single updating progress line with per-file and overall ETA."""
    pct = (rows_done / rows_total * 100) if rows_total > 0 else 0.0
    rate = rows_done / elapsed if elapsed > 0 else 0.0
    file_eta = (rows_total - rows_done) / rate if rate > 0 else 0.0

    prefix = f"[{source_key} {file_idx}/{n_files}]"
    bar_len = 20
    filled = int(bar_len * pct / 100)
    bar = "#" * filled + "-" * (bar_len - filled)

    # Overall progress across all files.
    if grand_rows_total > 0:
        g_pct = (grand_rows_done / grand_rows_total * 100)
        g_rate = grand_rows_done / grand_elapsed if grand_elapsed > 0 else 0.0
        g_eta = (grand_rows_total - grand_rows_done) / g_rate if g_rate > 0 else 0.0
        g_filled = int(bar_len * g_pct / 100)
        g_bar = "#" * g_filled + "-" * (bar_len - g_filled)
        overall = f" | overall [{g_bar}] {g_pct:.1f}% ETA {_fmt_eta(g_eta)}"
    else:
        overall = ""

    line = (
        f"\r{prefix} [{bar}] {rows_done:,}/{rows_total:,} ({pct:.1f}%) "
        f"| {_fmt_count(pairs)} pairs | {errors} err "
        f"| file ETA {_fmt_eta(file_eta)}{overall}"
    )

    max_len = 140
    if len(line) > max_len:
        line = line[:max_len]

    if final:
        print(line)
    else:
        print(line, end="", flush=True)


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------
def process_source_file(
    source: Path,
    output: Path,
    kernel: str,
    workers: int,
    clip_geom=None,
    file_idx: int = 0,
    n_files: int = 0,
    grand_rows_done: int = 0,
    grand_rows_total: int = 0,
    grand_elapsed: float = 0.0,
) -> dict:
    """Polyfill one source file -> intermediate Parquet of (h3_index, id_no).

    If clip_geom is provided (a shapely geometry), each row's geometry is
    intersected with it before polyfilling. Used for --denmark validation.
    """
    t0 = time.time()
    source_key = config.source_key(source)
    res = {
        "source": source_key,
        "input": str(source),
        "output": str(output),
        "kernel": kernel,
        "rows_read": 0,
        "rows_polyfilled": 0,
        "h3_pairs": 0,
        "errors": 0,
        "elapsed_sec": 0.0,
        "status": "pending",
    }

    tmp_path = output.with_suffix(output.suffix + ".tmp")

    # Resume: skip if the output file already exists and is valid.
    if output.exists():
        try:
            pq.read_metadata(str(output))
            print(f"[{source_key} {file_idx}/{n_files}] already complete -- skipped")
            res["status"] = "skipped"
            return res
        except Exception as e:
            logger.warning("CORRUPT output %s (%s). Re-processing.", output.name, e)
            try:
                os.remove(output)
            except OSError:
                pass

    if tmp_path.exists():
        logger.warning("Removing orphaned temp file %s", tmp_path.name)
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Get total row count from Parquet metadata (instant — no data read).
    if clip_geom is not None:
        candidates_path = config.TEMP_DIR / f"clip_candidates_{source_key}.parquet"
        if candidates_path.exists():
            rows_total = pq.ParquetFile(candidates_path).metadata.num_rows
        else:
            # Will be produced by Phase 1; fall back to source metadata.
            rows_total = pq.ParquetFile(source).metadata.num_rows
    else:
        rows_total = pq.ParquetFile(source).metadata.num_rows

    print(f"\n[{source_key} {file_idx}/{n_files}] {rows_total:,} rows, kernel={kernel}")

    # Bbox of the clip geometry. In clip mode, DuckDB does the bbox filter
    # in C++ (multithreaded, memory-managed) and writes only the ~1-5%
    # candidate rows to a small temp Parquet — avoiding both the Arrow
    # appender overflow on 16 GB WKB files and Python OOM. In global mode
    # (no clip), pyarrow streams all rows directly.
    clip_bbox = shapely.bounds(clip_geom) if clip_geom is not None else None

    # In clip mode: write the bbox-filtered candidates to a temp file first.
    candidates_path: Path | None = None
    if clip_bbox is not None:
        candidates_path = config.TEMP_DIR / f"clip_candidates_{source_key}.parquet"
        if not candidates_path.exists():
            logger.info("  DuckDB bbox-filter -> %s ...", candidates_path.name)
            t_filt = time.time()
            n_cand = write_clip_candidates(source, clip_bbox, candidates_path)
            logger.info("  bbox-filter: %d candidates in %.1fs",
                        n_cand, time.time() - t_filt)
            rows_total = n_cand
        else:
            n_cand = pq.ParquetFile(candidates_path).metadata.num_rows
            logger.info("  reusing cached candidates: %s (%d rows)",
                        candidates_path.name, n_cand)
            rows_total = n_cand

    writer: pq.ParquetWriter | None = None
    buffer: list[tuple[str, int]] = []
    total_rows = 0
    total_pairs = 0
    total_errors = 0
    total_kept = 0
    executor = None

    try:
        writer = pq.ParquetWriter(str(tmp_path), PAIRS_SCHEMA, compression="zstd")

        # Choose the row iterator: candidates file (clip mode) or source
        # file directly (global mode).
        if candidates_path is not None:
            row_iter = _stream_pyarrow(candidates_path, config.READ_BATCH_ROWS)
        else:
            row_iter = stream_source_rows(source, config.READ_BATCH_ROWS)

        # One process pool per source file, reused across all batches. h3
        # holds the GIL so threads don't parallelise the polyfill; processes
        # give true parallelism. Created once per file to amortise the
        # ~1-2s per-process spawn cost (Windows) over thousands of rows.
        executor = ProcessPoolExecutor(max_workers=workers)
        for arrow_tbl in row_iter:
            total_rows += arrow_tbl.num_rows

            # Build (wkb, id_no) tasks. In clip mode, DuckDB already filtered
            # by bbox so every row is a candidate. In global mode, no filter.
            wkb_col = arrow_tbl.column("geom_wkb").to_pylist()
            id_no_col = arrow_tbl.column("id_no").to_pylist()

            tasks: list[tuple[bytes, int]] = [
                (bytes(wkb), int(id_no))
                for wkb, id_no in zip(wkb_col, id_no_col)
                if wkb is not None and id_no is not None
            ]
            total_kept += len(tasks)

            for id_no, cells in _run_bounded(tasks, executor, workers, kernel, clip_geom):
                if cells is None:
                    total_errors += 1
                elif cells:
                    for cell in cells:
                        buffer.append((cell, id_no))
                    total_pairs += len(cells)
                    if len(buffer) >= config.WRITE_BATCH:
                        _flush(buffer, writer)
                        buffer.clear()

            # After each batch, print progress (skip if 100% — final print handles it).
            if total_rows < rows_total:
                elapsed = time.time() - t0
                _print_progress(
                    file_idx, n_files, source_key,
                    total_rows, rows_total,
                    total_pairs, total_errors, elapsed,
                    grand_rows_done=grand_rows_done + total_rows,
                    grand_rows_total=grand_rows_total,
                    grand_elapsed=grand_elapsed + elapsed,
                )

            del wkb_col, id_no_col, tasks

        executor.shutdown(wait=True)
        _flush(buffer, writer)
        writer.close()
        writer = None
        os.replace(str(tmp_path), str(output))

    except Exception as e:
        logger.error("Write/rename error for %s: %s", output.name, e)
        res["status"] = "write_error"
        if executor is not None:
            executor.shutdown(wait=True)
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

    elapsed = time.time() - t0
    res["rows_read"] = total_rows
    res["rows_polyfilled"] = total_rows - total_errors
    res["h3_pairs"] = total_pairs
    res["errors"] = total_errors
    res["elapsed_sec"] = round(elapsed, 2)
    res["status"] = "success" if total_pairs > 0 else "no_cells"

    _print_progress(
        file_idx, n_files, source_key,
        total_rows, rows_total,
        total_pairs, total_errors, elapsed,
        final=True,
        grand_rows_done=grand_rows_done + total_rows,
        grand_rows_total=grand_rows_total,
        grand_elapsed=grand_elapsed + elapsed,
    )

    del buffer
    gc.collect()
    return res


def _flush(buffer: list[tuple[str, int]], writer: pq.ParquetWriter) -> None:
    if not buffer:
        return
    h3s, ids = zip(*buffer)
    table = pa.Table.from_pydict(
        {"h3_index": list(h3s), "id_no": list(ids)},
        schema=PAIRS_SCHEMA,
    )
    writer.write_table(table)


# ---------------------------------------------------------------------------
# Denmark validation clipper (§10)
# ---------------------------------------------------------------------------
def _load_denmark_clip() -> shapely.geometry.base.BaseGeometry | None:
    """Load the buffered Denmark boundary for --denmark validation mode."""
    if not config.DENMARK_BND_PATH.exists():
        logger.warning("Denmark boundary not found: %s — running unclipped", config.DENMARK_BND_PATH)
        return None
    table = pq.read_table(str(config.DENMARK_BND_PATH))
    geom_col_name = "geometry"
    if geom_col_name not in table.schema.names:
        for col in table.schema.names:
            if "geom" in col.lower():
                geom_col_name = col
                break
    parts = []
    for wkb in table.column(geom_col_name).to_pylist():
        if wkb is not None and len(wkb) > 0:
            try:
                parts.append(shapely.from_wkb(wkb))
            except Exception:
                pass
    if not parts:
        return None
    geom = parts[0] if len(parts) == 1 else shapely.unary_union(parts)
    buffered = shapely.buffer(geom, 0.05)  # ~5 km, matches Denmark V3
    if not buffered.is_valid:
        buffered = shapely.make_valid(buffered)
    return buffered


# ---------------------------------------------------------------------------
# Parallel bbox filtering (clip mode only)
# ---------------------------------------------------------------------------
def _run_bbox_filters_parallel(
    sources: list[Path],
    clip_bbox: tuple[float, float, float, float],
) -> dict[str, Path]:
    """Run DuckDB bbox filters for all source files in parallel.

    Each file gets its own DuckDB connection + thread. The filters are
    I/O-bound (reading 37 GB of WKB from disk) so running them in parallel
    overlaps the I/O — total wall time ≈ the slowest file, not the sum.

    Returns {source_key: candidates_path} for all files that produced
    candidates.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _filter_one(source: Path) -> tuple[str, Path, int]:
        key = config.source_key(source)
        candidates_path = config.TEMP_DIR / f"clip_candidates_{key}.parquet"
        if candidates_path.exists():
            n = pq.ParquetFile(candidates_path).metadata.num_rows
            logger.info("  [cached] %s: %d candidates", key, n)
            return (key, candidates_path, n)
        logger.info("  [filter] %s ...", key)
        t0 = time.time()
        n = write_clip_candidates(source, clip_bbox, candidates_path)
        logger.info("  [filter] %s: %d candidates (%.1fs)", key, n, time.time() - t0)
        return (key, candidates_path, n)

    results: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=len(sources)) as tpe:
        futures = {tpe.submit(_filter_one, s): s for s in sources}
        for fut in as_completed(futures):
            key, cpath, n = fut.result()
            if n > 0:
                results[key] = cpath
    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream raw (h3_index, id_no) pairs from source GeoParquet"
    )
    parser.add_argument("--files", nargs="*",
                        help="Subset of source files (keys, e.g. 'amphibians fishes')")
    parser.add_argument("--fast", action="store_true",
                        help="Use the routed lossy kernel (coarse_refine_simp for "
                             "large polygons, simplify for small; 3-11x faster than "
                             "the old simplify kernel on large-bbox polygons, "
                             "~0.7%% cell delta)")
    parser.add_argument("--denmark", action="store_true",
                        help="Clip to buffered Denmark boundary (validation mode, §10)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Worker processes for polyfill (default 4; uses "
                             "multiprocessing since h3 holds the GIL — each worker "
                             "is fully parallel. Lower if huge-polygon WKBs cause "
                             "memory pressure.)")
    args = parser.parse_args()

    config.ensure_dirs()
    kernel = "fast" if args.fast else polyfill.DEFAULT_KERNEL
    if args.fast:
        logger.warning("FAST MODE: routed kernel (coarse_refine_simp on large "
                       "polygons, simplify on small). 3-11x faster than the old "
                       "simplify kernel on the large-bbox polygons that dominate "
                       "runtime; ~0.7%% cell delta (sub-cell detail H3 res-7 "
                       "cannot represent).")

    clip_geom = _load_denmark_clip() if args.denmark else None
    if args.denmark:
        out_dir = config.H3_PAIRS_DIR / "denmark"
        out_dir.mkdir(parents=True, exist_ok=True)
        logger.info("DENMARK VALIDATION MODE: clipping to buffered Denmark boundary")
    else:
        out_dir = config.H3_PAIRS_DIR

    if args.files:
        sources = [f for f in config.SOURCE_FILES if config.source_key(f) in args.files]
        if not sources:
            print(f"No files matched {args.files}", file=sys.stderr)
            sys.exit(1)
    else:
        sources = config.SOURCE_FILES

    logger.info("Sources to process: %d", len(sources))
    logger.info("Kernel: %s", kernel)
    logger.info("Output dir: %s", out_dir)

    # Get total row counts from Parquet metadata (instant, no data read).
    total_source_rows = 0
    file_row_counts: dict[str, int] = {}
    for s in sources:
        n = pq.ParquetFile(s).metadata.num_rows
        file_row_counts[config.source_key(s)] = n
        total_source_rows += n
    logger.info("Total rows across %d files: %s", len(sources), f"{total_source_rows:,}")

    # ── Phase 1: parallel bbox filtering (clip mode only) ──────────
    # Run all DuckDB bbox filters in parallel to overlap I/O. In global
    # mode (no clip) this phase is skipped — every row is a candidate.
    if clip_geom is not None:
        logger.info("=" * 60)
        logger.info("PHASE 1: Parallel DuckDB bbox filtering (%d files)", len(sources))
        logger.info("=" * 60)
        t_phase1 = time.time()
        clip_bbox = shapely.bounds(clip_geom)
        candidates_map = _run_bbox_filters_parallel(sources, clip_bbox)
        logger.info("Phase 1 done in %.1fs (%d files with candidates)",
                    time.time() - t_phase1, len(candidates_map))

    # Compute the grand total rows for the overall progress bar. In clip
    # mode this is the sum of candidate counts (much smaller than source
    # counts). In global mode it's the sum of source file row counts.
    if clip_geom is not None:
        grand_total_rows = sum(
            pq.ParquetFile(config.TEMP_DIR / f"clip_candidates_{config.source_key(s)}.parquet").metadata.num_rows
            for s in sources
            if (config.TEMP_DIR / f"clip_candidates_{config.source_key(s)}.parquet").exists()
        )
    else:
        grand_total_rows = total_source_rows

    # ── Phase 2: polyfill candidate files ──────────────────────────
    print()
    print("=" * 60)
    print(f"PHASE 2: Polyfill ({len(sources)} files, {grand_total_rows:,} rows total)")
    print("=" * 60)

    t_phase2 = time.time()

    results = []
    summary_path = out_dir / "_processing_summary.json"
    cumulative_rows = 0
    cumulative_elapsed = 0.0

    for file_idx, source in enumerate(sources, start=1):
        key = config.source_key(source)
        output = out_dir / f"class={key}.parquet"
        r = process_source_file(
            source, output, kernel, args.workers, clip_geom,
            file_idx=file_idx, n_files=len(sources),
            grand_rows_done=cumulative_rows,
            grand_rows_total=grand_total_rows,
            grand_elapsed=cumulative_elapsed,
        )
        results.append(r)
        cumulative_rows += r.get("rows_read", 0)
        cumulative_elapsed += r.get("elapsed_sec", 0.0)

        # Incremental summary so a crash mid-run still has partial state.
        summary = {
            "total_files": len(sources),
            "processed": len(results),
            "statuses": {},
            "total_rows": sum(x.get("rows_read", 0) for x in results),
            "total_pairs": sum(x.get("h3_pairs", 0) for x in results),
            "total_errors": sum(x.get("errors", 0) for x in results),
            "total_elapsed_sec": sum(x.get("elapsed_sec", 0.0) for x in results),
            "kernel": kernel,
            "details": results,
        }
        for x in results:
            s = x.get("status", "unknown")
            summary["statuses"][s] = summary["statuses"].get(s, 0) + 1
        with open(summary_path, "w") as fh:
            json.dump(summary, fh, indent=2, default=str)

    elapsed_total = time.time() - t_phase2

    print()
    print("=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    print(f"Files processed: {len(results)}")
    statuses = {s: sum(1 for r in results if r.get("status") == s)
                for s in set(r.get("status", "unknown") for r in results)}
    print(f"Statuses: {statuses}")
    print(f"Total pairs: {sum(r.get('h3_pairs', 0) for r in results):,}")
    print(f"Total errors: {sum(r.get('errors', 0) for r in results)}")
    print(f"Total time: {elapsed_total:.1f}s ({_fmt_eta(elapsed_total)})")
    print(f"Summary: {summary_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
