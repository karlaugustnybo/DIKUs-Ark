#!/usr/bin/env python3
"""Bulletproof A/B: simplify vs coarse_refine vs coarse_refine_simp.

Per-kernel daemon-thread timeout (30s), incremental write after each polygon,
per-id_no row-pruned DuckDB lookup (no full scans).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import shapely
from shapely import wkb as shapely_wkb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polyfill  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

OUT = config.TEMP_DIR / "kernel_ab.json"
TIMEOUT = 30.0

def load_targets() -> list[tuple[str, int]]:
    """Load ``class:id`` targets without embedding provider record IDs."""
    raw = os.environ.get("ARK_KERNEL_TARGETS", "")
    targets: list[tuple[str, int]] = []
    for item in filter(None, (part.strip() for part in raw.split(","))):
        source, separator, identifier = item.partition(":")
        if not separator:
            raise ValueError("ARK_KERNEL_TARGETS entries must use class:id")
        targets.append((source, int(identifier)))
    return targets


TARGETS = load_targets()


def run_with_timeout(fn, geom, timeout=TIMEOUT):
    res = {"ok": False, "value": None, "elapsed": 0.0}
    def _w():
        t0 = time.perf_counter()
        try:
            res["value"] = fn(geom)
            res["ok"] = True
        except Exception as e:
            res["value"] = repr(e)
        res["elapsed"] = time.perf_counter() - t0
    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, timeout, "timeout"
    if not res["ok"]:
        return None, res["elapsed"], f"error: {res['value']}"
    return res["value"], res["elapsed"], "ok"


def main():
    if not TARGETS:
        raise SystemExit(
            "Set ARK_KERNEL_TARGETS to comma-separated class:id values."
        )
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    kernels = [
        ("simplify", polyfill.strategy_simplify),
        ("coarse_refine", polyfill.strategy_coarse_refine),
        ("coarse_refine_simp", polyfill.strategy_coarse_refine_simp),
        ("tile_clip", polyfill.strategy_tile_clip),
    ]
    out = []
    for source, id_no in TARGETS:
        f = next((x for x in config.SOURCE_FILES if config.source_key(x) == source), None)
        if f is None:
            continue
        r = con.execute(f"""
            SELECT id_no, geom_wkb FROM read_parquet('{f.as_posix()}')
            WHERE id_no = {id_no} AND geom_wkb IS NOT NULL
        """).fetchone()
        if not r:
            continue
        geom = polyfill.extract_polygons(shapely_wkb.loads(bytes(r[1])))
        if geom is None or geom.is_empty:
            continue
        n_verts = sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors)
                      for p in (geom.geoms if geom.geom_type == "MultiPolygon" else [geom]))
        row = {"source": source, "id_no": id_no, "vertices_pre": n_verts}
        for name, fn in kernels:
            cells, elapsed, status = run_with_timeout(fn, geom)
            row[f"{name}_sec"] = round(elapsed, 3)
            row[f"{name}_cells"] = len(cells) if cells is not None else None
            row[f"{name}_status"] = status
        s = row["simplify_sec"]
        for k in ("coarse_refine", "coarse_refine_simp", "tile_clip"):
            v = row[f"{k}_sec"]
            row[f"speedup_{k}"] = round(s / v, 2) if v and v > 0.01 and row[f"{k}_status"] == "ok" else None
        out.append(row)
        with open(OUT, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"{source:18s} id={id_no:>10d} verts={n_verts:>6d} | "
              f"simp={row['simplify_sec']:>6}s(cs={row['simplify_cells']}) "
              f"coarse={row['coarse_refine_sec']:>6}s({row['coarse_refine_status']}) "
              f"csimp={row['coarse_refine_simp_sec']:>6}s({row['coarse_refine_simp_status']}) "
              f"tile={row['tile_clip_sec']:>6}s({row['tile_clip_status']}) "
              f"| x_csimp={row.get('speedup_coarse_refine_simp')}")
    con.close()
    print(f"\nFull: {OUT}")


if __name__ == "__main__":
    main()
