# DIKUs-Ark — Global H3 Dataset Reset Plan

## 1. What I reviewed

### Current pipeline scripts
- `pipeline_v2/config.py` and all `pipeline_v2/scripts/*.py`.
- The current global polyfill `03_buffered_h3_polyfill.py` and the Denmark-specific `03_buffered_h3_polyfill_denmark.py`.
- The aggregation, derive-res3, merged_gbif, and ToL scripts.

### Generated data / temp outputs
- `data/denmark/` final Parquet files. The historical production DuckDB cache (`data/denmark_h3.duckdb` / `data/precomputed_cache.duckdb`) stores both **per-cell species ID lists** and derived **per-cell metric counts**; the current `data/sample/denmark_prototype/denmark.duckdb` stores only pre-computed metric counts.
- `pipeline_v2/temp/_preclassify_summary.json` — 126,533 species, 61% fast / 39% slow.
- `pipeline_v2/temp/h3_parts/` partial global run — 24 of 51 source files already produced **~9.5 billion** raw `(h3_cell, species_id)` pairs.
- `pipeline_v2/temp/h3_parts/denmark_v3/` complete Denmark run — 1,257 species → 13.7 M pairs in ~9 min.
- `pipeline_v2/temp/bbox_index.parquet`, tiles DB, unified parts, and run logs.

### External source data (easily missed)
- `<external-geodata-root>/iucn_ranges_v2/` contains seven class-level GeoParquet files (`class=amphibians.parquet`, `class=fishes.parquet`, etc.).
- These files **already contain** the joined DNA-gap columns:
  `id_no`, `sci_name`, `redlistCategory`, `threat_score`, `dna_coverage_score`,
  `has_dna_species_level`, `genus_has_dna`, `family_has_dna`, `geom_wkb`,
  `iucn_grouping`, plus `presence`/`origin`/`seasonal`/`marine`/`terrestial`/`freshwater` flags.
- This is the real production input; `_unified_parts` is a lossy re-export that only keeps `gbif_accepted_id` + `geom`.

### Design docs in `Docs/`
- `Docs/README.md` — pipeline v2 design: produces `h3_res7_species.parquet` with per-cell species ID lists and a raw `h3_merged_raw.parquet`.
- `Docs/denmark_prototype_plan.md` — Denmark metrics schema with per-cell counts (CR/EN/VU/NT/DD/LC + missing DNA), no precomputed priority score.
- `Docs/visualization_plan.md` — future architecture is DuckDB + GeoParquet + H3 + Lonboard/deck.gl.
- `Docs/IUCN_Spatial_Ranges.md` — 128,768 polygon rows, 78,432 distinct species, presence/origin/seasonal codes.

### What I did not fully inspect
- The paths `scripts/precompute_h3.py`, `scripts/build_denmark_db.py`, and `scripts/create_sample_data.py` are not in the current tree or tracked history. The historical source-of-truth build scripts are tracked under `app/build_db.py` and `app/build_cache.py`, plus `denmark_prototype/build_denmark_spatial.py`; I inspected those in commit `fba18f1`.
- The full raw `BioDatasets/` files are gitignored and large; the GeoParquet files under `<external-geodata-root>` are the practical source.

---

## 2. The real problem with the current global generation

There are three related issues, not one.

### A. The data model is wrong for global scale

Pipeline v2 writes one row per `(h3_cell, species_id)` pair, then aggregates those into lists. This works for Denmark (13.7 M pairs) but not globally. The partial global run hit **~9.5 billion pairs after only ~24 of 51 source files** and left corrupted/temp files in `h3_parts/`. A full global run would likely produce tens of billions of pairs, making both the intermediate files and the final aggregation infeasible.

### B. The `_unified_parts` are lossy

The current global pipeline starts from `pipeline_v2/temp/_unified_parts/`, which only has:

```text
schema: gbif_accepted_id (int64), geom (binary)
```

All the scoring columns (`redlistCategory`, `dna_coverage_score`, `has_dna_species_level`, etc.) are gone. So the pipeline has to regenerate `merged_gbif` from CSV/TSV in script 06 *after* building the spatial grid. The seven source GeoParquet files already contain all of these columns joined to the geometry.

### C. Pipeline v2 skips the two-stage Denmark model

Pipeline v2 wants per-cell species ID lists **as the final visualization artifact**:

```text
h3_index, gbif_accepted_ids, geom
```

The historical production Denmark build is a **two-stage** model:

1. **Source of truth**: per-cell ID lists (`H3Res7Species` / `H3Res3Species` in `data/Ark-IV.duckdb`).
2. **Derived metrics**: per-cell count tables (`h3_res7_metrics` / `h3_res3_metrics` in `data/denmark_h3.duckdb` / `data/precomputed_cache.duckdb`) built by joining the ID lists to `SpecInfo`.

Those metrics tables include:

```text
h3_index, latitude, longitude,
total_species,
crit_endangered_count, endangered_count, vulnerable_count,
near_threatened_count, data_deficient_count, least_concern_count,
missing_species_dna, missing_genus_dna, missing_family_dna,
dna_coverage_score
```

The app then computes the score on-the-fly from UI weights. The metric-count output scales globally because it is one row per H3 cell, not one row per `(cell, species)` pair.

---

## 3. Key findings from Denmark that should be reused

The Denmark V3 run in `pipeline_v2/temp/h3_parts/denmark_v3/` validates the right building blocks:

1. **Size-aware polyfill** — Denmark's `preclassify.py` splits polygons into a fast path (direct `h3.geo_to_cells()`) and a slow path (simplify first). The instinct is right — huge polygons dominate cost — but the heuristic is untested; Phase 0 (§4) replaces it with a benchmarked kernel.
2. **Clip before polyfilling** — Denmark clips each species range to the buffered Denmark boundary *before* running H3, which makes even huge marine ranges cheap to process because only relevant cells are generated.
3. **Filter by real geometry** not bbox — `shapely.intersects()` against the actual boundary polygon.
4. **Stream-write Parquet + atomic rename** — memory stays flat and crashes are recoverable.
5. **Pre-compute per-cell ID lists, then derive metric counts** — the canonical Denmark build (`app/build_db.py` + `app/build_cache.py`, and `denmark_prototype/build_denmark_spatial.py`) stores **per-cell species ID lists** as the source of truth and materializes **per-cell metric counts** by joining those lists to species metadata. The current `pipeline_v2/scripts/03_buffered_h3_polyfill_denmark.py` therefore emits `(h3_index, gbif_accepted_id)` pairs, just like the global script; those pairs are later aggregated into ID lists before the count cache is built.

The global reset should copy 1–5, but replace "clip to Denmark" with a strategy that works for the whole planet and preserve the ID-list → metrics two-stage pattern. The fast/slow pre-classify heuristic (finding 1) is the exception: it is replaced by the benchmarked kernel from Phase 0 (§4).

---

## 4. Phase 0 — Polyfill strategy experimentation

Before building the full reset, settle the polyfill strategy empirically. The current `preclassify.py` fast/slow split (fast path = direct `h3.geo_to_cells()`, slow path = simplify first) is a reasonable instinct — huge polygons dominate cost — but the heuristic is *weird and complex* to reason about and is untested against alternatives. This phase replaces it with a benchmarked choice, and its output feeds Step B of the reset (§5).

### Why this matters

The single biggest cost in the global run is polyfilling continent-scale polygons at res 7. A naive `h3.geo_to_cells(geom, 7)` on a cosmopolitan marine range performs millions of point-in-polygon tests. Most of that work is wasted: interior cells can be enumerated by pure H3 parent→children math, and only the polygon boundary needs geometry testing.

### Candidate strategies (mostly independent; test one at a time)

1. **Coarse-polyfill + boundary refine.** Polyfill at res 3 (or 4). For coarse cells fully inside the polygon, expand with `h3.cell_to_children(cell, 7)` (pure math, no geometry tests). For coarse cells crossing the polygon boundary, clip the polygon to that cell's bbox and polyfill the clipped piece at res 7. Use `shapely.prepared.prep(geom)` for the inside test. Biggest single win for huge polygons.
2. **Grid-clip before polyfilling (tile the polygon).** Intersect the polygon with the existing 10×10° tile grid, then polyfill each piece independently. Each call is small and memory-bounded; pieces are parallel and resumable; antimeridian problems vanish because no piece crosses ±180. This generalises Denmark's "clip to boundary" trick from one country to the whole planet.
3. **Cell-size-aware simplification.** Simplify with a tolerance of roughly half a res-7 cell edge (~600 m ≈ `0.005°`). `geom.simplify(0.005, preserve_topology=False)` is effectively lossless at res 7 because the polyfill cannot resolve sub-cell detail. Cheap and combines well with 1 and 2.
4. **Skip the interior for coarse storage.** If a polygon covers more than N res-3 cells, store it at res 3 (or res 2) only in a small `coarse_coverage` table of `(res3_cell, id_no)` and expand to res-7 children lazily at merge time in DuckDB. Moves the expansion into a bulk DuckDB operation and keeps the polyfill stage fast.
5. **Shape-ratio routing.** Compute `geom.area / geom.envelope.area` plus vertex count before polyfilling. High ratio (blob-like) → boundary refine (1) is cheap. Low ratio (long skinny archipelago) → tiling (2) wins. This can replace the current fast/slow heuristic with a principled router.

### Benchmark protocol

Pick the 10–20 largest polygons by bbox area from the 7 GeoParquet files and benchmark:

| Variant | Description |
|---------|-------------|
| Baseline | `h3.geo_to_cells(geom, 7)` as-is |
| A | Idea 3 alone (simplify) |
| B | Idea 1 (coarse + children + boundary refine), with 3 applied to boundary pieces |
| C | Idea 2 (tile clip) |
| D | B + 3 combined |

Record **wall time**, **peak memory**, and **cell-set delta vs baseline** for each. A and D should be near-zero delta; B and C should be exactly zero if implemented correctly. The expected winner is B (with 3 on the boundary pieces): 10–100× faster on continent-scale polygons with identical output.

### Exit criterion

Pick one strategy (or a router combining 1 + 2 + 3 by shape ratio) and freeze it as the polyfill kernel for Step B (§5). Only then start the global build. The `preclassify.py` fast/slow heuristic is retired in favour of this benchmarked kernel.

---

## 5. Recommended reset architecture (pipeline v3)

### Core idea

Start from the **already-joined GeoParquet files** under `<external-geodata-root>/iucn_ranges_v2/`. For each polygon row, polyfill its geometry into H3 res-7 cells and emit a distinct `(h3_index, id_no)` record, deduplicating on the fly across multiple polygons for the same species and across the 7 class files. From the distinct `(h3_index, id_no)` relation, build per-cell ID lists, then JOIN to species metadata to produce per-cell metric counts. Keep the ID-list layer as the source of truth and derive `h3_res3_metrics.parquet` from it.

This avoids:
- materializing billions of raw `(cell, species)` pairs as a final artifact,
- rebuilding `merged_gbif` just to get scores,
- the lossy `_unified_parts` step.

### Step A — Input and filtering

Read from the 7 source GeoParquet files. Apply sensible row filters at read time:

```sql
SELECT id_no,
       redlistCategory,
       has_dna_species_level,
       genus_has_dna,
       family_has_dna,
       dna_coverage_score,
       geom_wkb
FROM read_parquet('<external-geodata-root>/iucn_ranges_v2/*.parquet')
WHERE presence = 1          -- extant ranges only (drop extinct/uncertain)
   AND origin IN (1, 2)     -- native and reintroduced ranges
   AND geom_wkb IS NOT NULL;
```

The `presence`/`origin` codes are documented in `Docs/IUCN_Spatial_Ranges.md`.


### Step B — Avoid the pair-explosion with per-cell metrics

Instead of emitting `(cell, species_id)` rows, accumulate counters in memory keyed by cell.

Suggested metric schema (matches the historical production cache built by `app/build_cache.py` / `denmark_prototype/build_denmark_spatial.py`):

```text
h3_index                  string
latitude                  double
longitude                 double
total_species             int64   -- distinct id_no per cell
crit_endangered_count     int64
endangered_count        int64
vulnerable_count          int64
near_threatened_count     int64
data_deficient_count      int64
least_concern_count       int64
missing_species_dna       int64
missing_genus_dna         int64
missing_family_dna        int64
dna_coverage_score        int64   -- average coverage score per cell
```

The production Denmark build also materializes **separate filtered metrics tables** per system (`_agg_all`, `_agg_Terrestrial`, `_agg_Freshwater`, `_agg_Marine`) and uses a land/sea classification per cell. Decide whether the global reset needs the same system split; for a first reset the `_all` table above is sufficient.

For every polygon row:

1. Load WKB via `shapely.from_wkb`.
2. Make valid if needed.
3. Apply cell-size-aware simplify (`tol ≈ 0.005°`) to large polygons — effectively lossless at res 7 (Phase 0, §4).
4. Polyfill with the Phase 0 kernel: coarse-fill at res 3/4, expand interior coarse cells via `h3.cell_to_children`, refine only boundary cells with `h3.geo_to_cells` on the clipped piece.
5. Stream each `(h3_index, id_no)` pair directly to a DuckDB relation or intermediate Parquet (see Step C). No per-chunk Python `seen` set — deduplication is out-of-core in DuckDB.
6. When flushing a chunk, just append to the streaming output. Counts are derived later in Step E from the global distinct relation, not per-chunk.

### Step C — Handle duplicate polygons and duplicate species across files (out-of-core)

The 7 files contain **128,768 polygon rows for 78,432 species** (~1.6 polygons/species). In addition, **9,201 species appear in more than one of the 7 class files**. If a species has two polygons that overlap the same H3 cell, simple per-polygon counting would double-count that species; if it is counted once per class file, it would also be double-counted after merging.

**Do not attempt in-memory deduplication in Python.** In-memory `set` objects can OOM on densely populated cells (tropical rainforests, coral reefs) where a single cell may be covered by thousands of species. Instead:

1. **Stream raw `(h3_index, id_no)` pairs directly** into a DuckDB relation or intermediate Parquet files as they are produced by the polyfill. No chunk-local `seen` set, no Python aggregation.
2. Let **DuckDB** perform the `SELECT DISTINCT (h3_index, id_no)` across the 9,201 shared species and overlapping polygons. DuckDB is heavily optimised for out-of-core `DISTINCT` and will spill to disk if needed.
3. Only after the distinct relation exists, join to species metadata and aggregate to per-cell counts (Step E).

This is exactly what the historical Denmark build does (ID lists in `H3Res7Species`, counts derived from them) — just with DuckDB replacing the Python `seen` set so the global scale cannot OOM.

### Step D — Resumable, chunked processing

The 7 GeoParquet files are huge (37 GB total). Process them one file at a time.

For each source file, stream raw `(h3_index, id_no)` pairs into an intermediate Parquet (not metrics — counts are derived later in DuckDB):

```text
pipeline_v3/temp/h3_pairs/class=amphibians.parquet
pipeline_v3/temp/h3_pairs/class=fishes.parquet
...
```

If a run crashes, skip already-completed intermediate files. This mirrors the Denmark file-level resume logic.

### Step E — Merge and derive

Once all 7 intermediate files exist:

1. Merge all intermediate files into a single DuckDB table and run `SELECT DISTINCT h3_index, id_no` to remove cross-file double counts.
2. Join the distinct `(h3_index, id_no)` records to a species metadata table that carries `redlistCategory`, `has_dna_species_level`, `genus_has_dna`, and `family_has_dna`.
3. Aggregate per H3 cell using `COUNT FILTER (WHERE ...)` to populate the metric counters, `total_species`, and `dna_coverage_score`.
4. Add `latitude` and `longitude` via `h3.cell_to_latlng(h3_index)`.
5. Output `data/global/h3_res7_metrics.parquet`.
6. For res-3, map each res-7 cell to its res-3 parent, again run `SELECT DISTINCT parent, id_no`, and aggregate counts. **Do not sum the res-7 metric columns** — summing would double-count species that span multiple res-7 children of the same res-3 cell.
7. **Recalculate `dna_coverage_score` at res-3 from the distinct `(parent, id_no)` relation**, not by averaging the res-7 averages. Averaging averages is statistically wrong for coverage scores and would mis-weight cells whose res-7 children carry different species counts. Re-join the distinct res-3 `(parent, id_no)` pairs to species metadata and compute the score fresh, exactly as for res-7.

This matches the count cache built by `app/build_cache.py` and `denmark_prototype/build_denmark_spatial.py`.

### Step F — Partitioned drill-down species lists (kept, not transient)

Per-cell species lists (`h3_res7_species`) are useful for "show me the species in this cell" and are required to build the metric counts correctly. Per decision 3 (§8), they are **not transient** and are **not a monolithic global file**. Store them as a **Parquet dataset partitioned by H3 res-2 (or res-3) parent cell**:

```text
data/global/h3_res7_species/
└── res2=<parent_cell>/
    └── part-000.parquet   # rows: h3_index, id_no (or h3_index, list<id_no>)
```

This lets the application run on-demand drill-down queries (`SELECT id_no FROM ... WHERE res2 = ?`) without scanning the whole planet. The metric-count build (Step E) reads the same dataset for its `SELECT DISTINCT` + JOIN, so there is a single source of truth.

### Step G — Optional raw merged table

If the app still needs a raw IUCN+GOAT row dump for detail panels, keep script 06 as-is or rebuild it to read the 7 GeoParquet files (which already contain almost all those columns). This is separate from the H3 metrics output.

### Step H — ToL export

The current `07_export_tol.py` filters ToL rows by the taxa names in the merged output. Globally this set is much larger but still manageable with DuckDB batch inserts. Reuse the logic, but expect larger counts and still expect the kingdom/phylum gaps documented in `data/denmark/README.md`.

---

## 6. What should be kept / dropped

| Keep | Drop / rework |
|------|---------------|
| Size-aware polyfill kernel (Phase 0 winner) | Current `_unified_parts` workflow (lossy) |
| Cell-size-aware simplify (`tol ≈ 0.005°`) on boundary pieces | `03_buffered_h3_polyfill.py` pair-emission model |
| Stream-write Parquet + atomic rename | `grid_ring(1)` boundary expansion (adds noise and size) |
| Denmark clipping pattern (for subsets) | Resuming from the corrupted `h3_parts` partial run |
| Two-stage model: per-cell ID lists as source of truth, then metrics cache | Per-cell full species ID lists as a mandatory **global visualization** output |
| 10×10° tile grid for restartability | Script 06 rebuilding merged_gbif from CSV if GeoParquet has all columns |

---

## 7. Why this solves the current pain points

| Pain point | How the reset fixes it |
|------------|------------------------|
| 9.5 B+ raw pairs and growing | Final output is one row per unique cell, not per `(cell, species)` pair. Raw pairs are streamed to DuckDB and deduplicated out-of-core, never materialised as a deliverable. |
| Lossy `_unified_parts` | Read the joined GeoParquet files that already contain scores. |
| Rebuilding merged_gbif separately | Scores are already in the source GeoParquet. |
| `grid_ring(1)` bloat | Drop the ring expansion (neither the original Denmark build nor the prototype app used it); polyfill the real polygon instead. The current `03_buffered_h3_polyfill_denmark.py` still uses `grid_ring(1)`, so this is an explicit design change, not a Denmark lesson. |
| Corrupted partial run / not resumable | Process each of the 7 source files independently to its own intermediate pair file. |
| Denmark and global pipelines diverging | Both should follow the same two-stage pattern: emit `(h3_index, id_no)` pairs → ID lists → metric cache. The global variant just omits the Denmark boundary clip and optionally adds system/land-sea filters. |
| OOM on dense-cell dedup | No Python `seen` set; raw pairs go straight to DuckDB which spills to disk (Step C). |
| Antimeridian hangs | Split polygons at ±180 before H3 (§11); grid-clip tiling makes this a non-issue. |

---

## 8. Decisions (resolved)

Three choices that determine the shape of the pipeline are now settled:

1. **Scope of the source data** — Use the 7 GeoParquet files under `<external-geodata-root>/iucn_ranges_v2/` directly. Rebuilding `merged_gbif` from raw `BioDatasets/` when the pre-joined data already exists is redundant and introduces unnecessary failure points.

2. **Geographic scope** — Begin with **Land + EEZ only**. Marine cosmopolitan species (e.g. pelagic fishes) create massive polygon bounding boxes that generate millions of cells per species. Validating the pipeline on Land + EEZ ensures the logic holds at scale before tackling the computational edge cases of the open ocean. Open-ocean coverage is a follow-up phase.

3. **Output priority** — The precomputed metric counts (`h3_res7_metrics` and `h3_res3_metrics`) are entirely sufficient for the primary heatmap layer. The `h3_res7_species` ID lists, however, are **not transient**: store them as a **partitioned Parquet dataset** (partitioned by H3 res-2 or res-3 parent cell). This lets the application run on-demand "drill-down" species-list queries without scanning a monolithic global file.

---

## 9. Suggested file layout

```text
GLOBAL_H3_RESET_PLAN.md           # this doc
pipeline_v3/
├── config.py
└── scripts/
    ├── 00_benchmark_polyfill.py     # Phase 0: benchmark candidate polyfill strategies
    ├── 01_compute_h3_metrics.py      # GeoParquet → per-cell res-7 metrics (chunked)
    ├── 02_merge_metrics.py            # combine 7 chunk files
    ├── 03_derive_res3_metrics.py      # res-7 → res-3 parent roll-up
    ├── 04_partition_species_lists.py  # res-7 ID lists partitioned by res-2 parent
    ├── 05_optional_raw_merge.py     # raw IUCN+GOAT detail dump
    └── 06_export_tol.py               # ToL filter by taxa names
data/global/
├── h3_res7_metrics.parquet
├── h3_res3_metrics.parquet
└── h3_res7_species/                 # partitioned dataset (res2=<parent>/part-*.parquet)
    └── res2=.../
```

---

## 10. First validation step

After implementing `pipeline_v3/scripts/01_compute_h3_metrics.py`, run it **only on Denmark** first and compare the results to the existing `denmark.duckdb` tables:

1. Total `total_species` per cell should match the number of distinct GBIF IDs in the source `h3_res7_species.parquet` ID lists (the current prototype DuckDB has 36,195 res-7 cells and 27 res-3 cells, not the older 35,873 / 15 numbers in `Docs/denmark_prototype_plan.md`).
2. `missing_species_dna`, `missing_genus_dna`, `missing_family_dna`, and `dna_coverage_score` should match the historical production logic in `denmark_prototype/build_denmark_spatial.py`: species DNA is present only when `sequencing_status IN ('insdc_open', 'published')` AND `assembly_level IS NOT NULL`; genus/family DNA is true only if every species in that genus/family has DNA.
3. Category counts summed across cells should match the filtered source totals.
4. If system-specific metric tables are kept (Terrestrial / Freshwater / Marine), validate them against the cell-level land/sea classification as well.

Only after the Denmark validation passes should you run the full global 7-file batch.

---

## 11. Risks to watch

- **Antimeridian-crossing ranges** exist (e.g. abalone species spanning -180 to +180). `h3.geo_to_cells()` frequently **fails or hangs** on polygons crossing the 180th meridian. **Mitigation:** before passing geometries to H3, explicitly split polygons that cross the antimeridian — slice with Shapely at the -180 and +180 bounding boxes so H3 always receives standard, valid geometries. This prevents silent pipeline hangs. The grid-clip strategy (Phase 0 idea 2, §4) makes this a non-issue because no tile crosses ±180.
- **Very large polygons** (e.g. cosmopolitan marine fishes) can still produce hundreds of thousands of res-7 cells. Phase 0 (§4) is designed to make these cheap; verify the chosen kernel's peak memory on the largest sample before the full run. The Land + EEZ scope (decision 2, §8) further limits this by excluding open-ocean cosmopolitans from the first run.
- **Duplicate species across source files**: the 7 GeoParquet files are *not* fully disjoint by species. I found **9,201** distinct `id_no` values that appear in more than one file. Any per-file or per-polygon metric aggregation must be deduplicated globally before summing counts. Handled out-of-core in DuckDB (Step C).
- **Duplicate polygons within species**: 128,768 polygon rows map to 78,432 species (~1.6 polygons/species). A species whose ranges overlap the same H3 cell must be counted only once. Handled by the same DuckDB `SELECT DISTINCT` pass.
- **OOM during deduplication**: avoid by not deduplicating in Python. Stream raw pairs to DuckDB and let the engine spill to disk (Step C).

---

## 12. Summary

The global pipeline should be reset to:

First run **Phase 0** (§4): benchmark the candidate polyfill strategies on the 10–20 largest polygons and freeze the winner as the polyfill kernel. Then:

1. Read the already-joined GeoParquet files from `<external-geodata-root>/iucn_ranges_v2/` directly (decision 1, §8) — no `merged_gbif` rebuild.
2. Scope the first run to **Land + EEZ** (decision 2, §8); open ocean is a follow-up.
3. Polyfill with the Phase 0 kernel, splitting antimeridian-crossing polygons before H3, and **stream raw `(h3_index, id_no)` pairs straight to DuckDB/Parquet** — no in-Python deduplication (Step C).
4. Let DuckDB run `SELECT DISTINCT (h3_index, id_no)` across the 9,201 shared species and overlapping polygons, out-of-core.
5. Materialise the global per-cell ID-list layer as a **Parquet dataset partitioned by H3 res-2 parent** (decision 3, §8) — the single source of truth for both the heatmap and on-demand drill-down.
6. JOIN the distinct relation to species metadata to produce `h3_res7_metrics.parquet`, then derive `h3_res3_metrics.parquet` by re-running `SELECT DISTINCT (parent, id_no)` and **recalculating `dna_coverage_score` fresh** at res-3 (not averaging res-7 averages).
7. Keep system-specific and land/sea-filtered metric tables as an optional follow-up.

This removes the billion-row pair-explosion that would come from materialising every pair as a final deliverable, but still uses the ID-list → metrics pattern that the historical Denmark build and the current app actually depend on.
