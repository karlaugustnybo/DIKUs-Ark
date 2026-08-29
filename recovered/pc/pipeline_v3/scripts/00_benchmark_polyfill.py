#!/usr/bin/env python3
"""
00_benchmark_polyfill.py  —  Phase 0: polyfill strategy benchmark.

§4 of GLOBAL_H3_RESET_PLAN.md. Picks the 10-20 largest polygons by bbox
area from the 7 source GeoParquet files and benchmarks the candidate
strategies against the baseline:

  - baseline:           h3.geo_to_cells(geom, 7) as-is
  - simplify:           cell-size-aware simplify, then baseline (idea 3)
  - coarse_refine:      coarse-fill + interior children + boundary refine (idea 1)
  - tile_clip:          grid-clip before polyfilling (idea 2)
  - coarse_refine_simp: coarse_refine + simplify on boundary pieces (B+3)

For each polygon × variant, records wall time, peak memory, and cell-set
delta vs baseline. Writes the verdict to pipeline_v3/temp/phase0_benchmark.json.

The baseline is infeasible (5+ min) on cosmopolitan marine polygons, so
the benchmark caps each variant at BASELINE_TIMEOUT seconds and falls back
to tile_clip as the reference if the baseline times out.

Usage:
    uv run python pipeline_v3/scripts/00_benchmark_polyfill.py
    uv run python pipeline_v3/scripts/00_benchmark_polyfill.py --n 5 --files amphibians
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import tracemalloc
from pathlib import Path

import duckdb
import h3
from shapely import wkb as shapely_wkb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polyfill  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


BASELINE_TIMEOUT: float = 30.0   # seconds; baseline is killed after this
TOP_N: int = 20                   # default number of largest polygons to test


# ---------------------------------------------------------------------------
# Timeout helper (Unix-style SIGALRM not available on Windows; use a thread)
# ---------------------------------------------------------------------------
class _Timeout(Exception):
    pass


def _run_with_timeout(fn, args=(), kwargs=None, timeout: float = BASELINE_TIMEOUT):
    """Run fn(*args, **kwargs) with a wall-clock timeout.

    Uses a daemon thread + join so it works on Windows (where SIGALRM is
    unavailable). The killed thread is NOT truly cancelled (Python can't
    kill threads), but the result is discarded and a _Timeout is raised.
    """
    import threading

    kwargs = kwargs or {}
    result: dict = {"ok": False, "value": None, "error": None}

    def _worker():
        try:
            result["value"] = fn(*args, **kwargs)
            result["ok"] = True
        except Exception as exc:
            result["error"] = repr(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise _Timeout(f"timed out after {timeout}s")
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["value"]


# ---------------------------------------------------------------------------
# Candidate selection: largest polygons by bbox area
# ---------------------------------------------------------------------------
def pick_largest_polygons(files: list[Path], n: int) -> list[dict]:
    """Return the n largest polygons (by bbox area) across the source files."""
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    picked: list[dict] = []
    for f in files:
        key = config.source_key(f)
        rows = con.execute(
            f"""
            SELECT id_no, geom_wkb,
                   (ST_XMax(ST_GeomFromWKB(geom_wkb)) - ST_XMin(ST_GeomFromWKB(geom_wkb))) *
                   (ST_YMax(ST_GeomFromWKB(geom_wkb)) - ST_YMin(ST_GeomFromWKB(geom_wkb))) AS bbox_area
            FROM read_parquet('{f.as_posix()}')
            WHERE geom_wkb IS NOT NULL AND {config.SOURCE_FILTER_SQL}
            ORDER BY bbox_area DESC
            LIMIT {n}
            """).fetchall()
        for id_no, wkb, area in rows:
            picked.append({
                "source": key,
                "id_no": int(id_no),
                "bbox_area_deg2": float(area),
                "wkb": bytes(wkb),
            })
        logger.info("  scanned %s: %d candidate rows", key, len(rows))
    con.close()
    # Keep only the overall top N
    picked.sort(key=lambda r: r["bbox_area_deg2"], reverse=True)
    return picked[:n]


# ---------------------------------------------------------------------------
# Benchmark a single polygon against all variants
# ---------------------------------------------------------------------------
def benchmark_polygon(rec: dict, variants: list[str]) -> dict:
    geom = shapely_wkb.loads(rec["wkb"])
    geom = polyfill.extract_polygons(geom)
    if geom is None or geom.is_empty:
        return {"id_no": rec["id_no"], "status": "skipped_empty"}

    out: dict = {
        "source": rec["source"],
        "id_no": rec["id_no"],
        "bbox_area_deg2": round(rec["bbox_area_deg2"], 1),
        "geom_type": geom.geom_type,
        "bounds": list(geom.bounds),
        "results": {},
    }

    # Baseline (with timeout). If it times out, tile_clip becomes the
    # reference for delta computation.
    baseline_cells: set[str] | None = None
    baseline_time: float | None = None
    try:
        tracemalloc.start()
        t0 = time.time()
        baseline_cells = _run_with_timeout(
            polyfill.strategy_baseline, args=(geom,), timeout=BASELINE_TIMEOUT
        )
        baseline_time = time.time() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        out["results"]["baseline"] = {
            "cells": len(baseline_cells),
            "wall_sec": round(baseline_time, 3),
            "peak_mb": round(peak / 1e6, 1),
            "status": "ok",
        }
    except _Timeout:
        out["results"]["baseline"] = {
            "cells": None,
            "wall_sec": BASELINE_TIMEOUT,
            "peak_mb": None,
            "status": "timeout",
        }
    except Exception as exc:
        out["results"]["baseline"] = {"cells": None, "status": f"error: {exc!r}"}

    # Other variants
    reference_cells = baseline_cells
    if reference_cells is None:
        # Baseline timed out: use tile_clip as the reference (it's exact).
        logger.info("  baseline timed out for id_no=%d; using tile_clip as reference", rec["id_no"])
        t0 = time.time()
        reference_cells = polyfill.strategy_tile_clip(geom)
        out["results"]["_reference"] = "tile_clip (baseline timed out)"

    for variant in variants:
        if variant == "baseline":
            continue
        fn = polyfill.STRATEGIES[variant]
        try:
            tracemalloc.start()
            t0 = time.time()
            cells = fn(geom)
            elapsed = time.time() - t0
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            delta = len(reference_cells ^ cells) if reference_cells is not None else None
            out["results"][variant] = {
                "cells": len(cells),
                "wall_sec": round(elapsed, 3),
                "peak_mb": round(peak / 1e6, 1),
                "delta_vs_reference": delta,
                "status": "ok",
            }
        except Exception as exc:
            out["results"][variant] = {"cells": None, "status": f"error: {exc!r}"}

    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 0 polyfill benchmark")
    parser.add_argument("--n", type=int, default=TOP_N,
                        help=f"Top-N largest polygons per file (default {TOP_N})")
    parser.add_argument("--files", nargs="*",
                        help="Subset of source files (keys, e.g. 'amphibians fishes')")
    args = parser.parse_args()

    if args.files:
        files = [f for f in config.SOURCE_FILES if config.source_key(f) in args.files]
        if not files:
            print(f"No files matched {args.files}", file=sys.stderr)
            sys.exit(1)
    else:
        files = config.SOURCE_FILES

    logger.info("Phase 0: picking top-%d largest polygons from %d source files",
                args.n, len(files))
    candidates = pick_largest_polygons(files, args.n)
    logger.info("Picked %d candidate polygons", len(candidates))

    variants = list(polyfill.STRATEGIES.keys())
    results = []
    for i, rec in enumerate(candidates, 1):
        logger.info("[%d/%d] id_no=%d bbox_area=%.0f deg^2 source=%s",
                    i, len(candidates), rec["id_no"], rec["bbox_area_deg2"], rec["source"])
        r = benchmark_polygon(rec, variants)
        results.append(r)
        # Write incrementally so partial results survive a crash.
        with open(config.BENCHMARK_OUT, "w") as fh:
            json.dump({"variants": variants, "results": results}, fh, indent=2, default=str)

    # Print summary table
    logger.info("=" * 80)
    logger.info("PHASE 0 SUMMARY")
    logger.info("=" * 80)
    logger.info("%-22s %10s %10s %10s %10s",
                "variant", "mean_sec", "mean_delta", "timeouts", "errors")
    for v in variants:
        wall = [r["results"][v]["wall_sec"] for r in results
                if v in r["results"] and isinstance(r["results"][v].get("wall_sec"), (int, float))]
        deltas = [r["results"][v]["delta_vs_reference"] for r in results
                  if v in r["results"] and r["results"][v].get("delta_vs_reference") is not None]
        timeouts = sum(1 for r in results
                       if v in r["results"] and r["results"][v].get("status") == "timeout")
        errors = sum(1 for r in results
                     if v in r["results"] and r["results"][v].get("status", "").startswith("error"))
        mean_wall = round(sum(wall) / max(len(wall), 1), 3)
        mean_delta = round(sum(deltas) / max(len(deltas), 1), 1) if deltas else 0
        logger.info("%-22s %10s %10s %10s %10s",
                    v, mean_wall, mean_delta, timeouts, errors)
    logger.info("=" * 80)
    logger.info("Frozen production kernel: %s", polyfill.DEFAULT_KERNEL)
    logger.info("Full results: %s", config.BENCHMARK_OUT)


if __name__ == "__main__":
    main()
