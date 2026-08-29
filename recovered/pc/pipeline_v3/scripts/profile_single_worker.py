#!/usr/bin/env python3
"""Per-stage profiler for the single-worker polyfill path.

Picks polygons across the WKB-size spectrum (proxy for vertex count) and
times every stage of `polyfill_row` + `strategy_simplify` so we can see
exactly where single-worker wall time goes.

Writes results to a JSON file so we can read it back even if the run is
interrupted.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import duckdb
import h3
import shapely
from shapely import wkb as shapely_wkb
from shapely import simplify as shp_simplify

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polyfill  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

OUT = config.TEMP_DIR / "single_worker_profile.json"


def time_stage(fn, *args, **kwargs):
    t0 = time.perf_counter()
    res = fn(*args, **kwargs)
    return time.perf_counter() - t0, res


def profile_one(wkb: bytes, label: str, source: str, id_no: int) -> dict:
    """Time every stage of polyfill_row + strategy_simplify on one polygon."""
    stages: dict[str, float] = {}
    rec = {"label": label, "source": source, "id_no": id_no,
           "wkb_bytes": len(wkb), "stages": stages}

    # Stage 1: WKB load
    stages["1_wkb_load"], geom = time_stage(shapely_wkb.loads, wkb)
    rec["geom_type"] = geom.geom_type
    rec["bounds"] = list(geom.bounds)

    # Stage 2: extract_polygons (first call, in polyfill_row)
    stages["2_extract_polygons_1"], geom = time_stage(polyfill.extract_polygons, geom)
    if geom is None or geom.is_empty:
        rec["status"] = "empty"
        return rec

    # Count vertices BEFORE simplify (the thing that drives h3 cost)
    t0 = time.perf_counter()
    n_verts_pre = sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors)
                      for p in (geom.geoms if geom.geom_type == "MultiPolygon" else [geom]))
    stages["2b_count_vertices_pre"] = time.perf_counter() - t0
    rec["vertices_pre"] = n_verts_pre

    # Stage 3: simplify (the --fast kernel's first step)
    stages["3_simplify"], g_simp = time_stage(shp_simplify, geom, polyfill.SIMPLIFY_TOL_DEG, True)

    # Stage 4: extract_polygons (second call, inside strategy_simplify)
    stages["4_extract_polygons_2"], g_simp = time_stage(polyfill.extract_polygons, g_simp)
    if g_simp is None or g_simp.is_empty:
        rec["status"] = "empty_after_simplify"
        return rec

    # Count vertices AFTER simplify
    t0 = time.perf_counter()
    n_verts_post = sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors)
                       for p in (g_simp.geoms if g_simp.geom_type == "MultiPolygon" else [g_simp]))
    stages["4b_count_vertices_post"] = time.perf_counter() - t0
    rec["vertices_post"] = n_verts_post
    rec["vertex_reduction"] = round(1 - n_verts_post / max(n_verts_pre, 1), 3)

    # Stage 5: antimeridian_split (bounds check + potential split)
    stages["5_antimeridian_split"], pieces = time_stage(polyfill.antimeridian_split, g_simp)
    rec["n_pieces"] = len(pieces)

    # Stage 6: __geo_interface__ conversion (shapely -> geojson dict) — a known
    # hot spot because it's pure Python for big polygons.
    geo_dicts = []
    t0 = time.perf_counter()
    for p in pieces:
        geo_dicts.append(p.__geo_interface__)
    stages["6_geo_interface"] = time.perf_counter() - t0

    # Stage 7: h3.geo_to_cells (the actual polyfill) — measured per piece
    h3_times = []
    cells_total: set[str] = set()
    for gi in geo_dicts:
        t0 = time.perf_counter()
        try:
            c = set(h3.geo_to_cells(gi, res=polyfill.H3_RES))
        except Exception:
            c = set()
        h3_times.append(time.perf_counter() - t0)
        cells_total |= c
    stages["7_h3_geo_to_cells"] = sum(h3_times)
    rec["h3_per_piece"] = [round(x, 3) for x in h3_times]
    rec["n_cells"] = len(cells_total)

    # Derived metrics
    total = sum(stages.values())
    rec["total_staged_sec"] = round(total, 3)
    rec["pct_breakdown"] = {k: round(v / total * 100, 1) for k, v in stages.items() if v > 0}

    # Also time the full strategy_simplify end-to-end for comparison
    stages_full, cells_full = polyfill.strategy_simplify(geom), None
    t0 = time.perf_counter()
    cells_full = polyfill.strategy_simplify(geom)
    rec["full_strategy_simplify_sec"] = round(time.perf_counter() - t0, 3)
    rec["full_cells"] = len(cells_full)
    rec["cells_match"] = (len(cells_total) == len(cells_full))

    return rec


def pick_samples(con, n_per_bucket: int = 2) -> list[dict]:
    """Pick polygons without full-table scans (which read all 37GB of WKB).

    Strategy: use 3 first rows per file (instant — no ORDER BY, reads the
    first row group). Historical provider record IDs used for targeted
    profiling were removed from this recovered copy.
    """
    samples: list[dict] = []
    for f in config.SOURCE_FILES:
        key = config.source_key(f)
        # Cheap first rows.
        rows = con.execute(f"""
            SELECT id_no, geom_wkb FROM read_parquet('{f.as_posix()}')
            WHERE presence = 1 AND origin IN (1,2) AND geom_wkb IS NOT NULL
            LIMIT 3
        """).fetchall()
        for id_no, wkb in rows:
            samples.append({"source": key, "id_no": int(id_no),
                            "wkb": bytes(wkb), "label": f"{key} small",
                            "wkb_size": len(bytes(wkb)), "quantile": "head"})
    return samples


def main():
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    logger = print

    samples = pick_samples(con)
    print(f"Picked {len(samples)} sample polygons across {len(config.SOURCE_FILES)} files")

    results = []
    for i, s in enumerate(samples, 1):
        print(f"\n[{i}/{len(samples)}] {s['label']}  (id_no={s['id_no']}, {s['wkb_size']/1e6:.2f} MB)")
        try:
            rec = profile_one(s["wkb"], s["label"], s["source"], s["id_no"])
        except Exception as e:
            rec = {"label": s["label"], "source": s["source"], "id_no": s["id_no"],
                   "error": repr(e)}
        results.append(rec)
        # Incremental write
        with open(OUT, "w") as fh:
            json.dump(results, fh, indent=2, default=str)
        if "stages" in rec:
            for k, v in rec["stages"].items():
                if v > 0.01:
                    print(f"    {k:32s} {v:8.3f}s")
            print(f"    {'n_cells':32s} {rec.get('n_cells','?')}")
            print(f"    {'full_strategy_simplify':32s} {rec.get('full_strategy_simplify_sec','?')}s")

    con.close()
    print(f"\nFull results: {OUT}")


if __name__ == "__main__":
    main()
