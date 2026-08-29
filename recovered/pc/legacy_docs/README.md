# DIKUs-Ark — Global DNA Gap Analysis

This repository builds global H3-hexagon maps showing where threatened species lack genomic DNA data. It is the global successor to the Denmark prototype at <https://github.com/karlaugustnybo/DIKUs-Ark>.

## What this repo does

The pipeline turns raw biodiversity data into scored hexagon datasets:

1. **Spatial → H3 cells:** IUCN species range polygons are converted into H3 res-7 cells (`data/global/h3_res7_species.parquet`) and rolled up to res-3 cells (`data/global/h3_res3_species.parquet`).
2. **Tabular merge:** IUCN Red List + GOAT + EDGE data are merged by GBIF accepted name and filtered to species present in the H3 cells (`data/global/h3_merged_raw.parquet`).
3. **Tree-of-Life export:** Higher-rank GOAT/ToL entries matching those species are exported (`data/global/h3_tol.parquet`).

These Parquet outputs are the same shape as the inputs used by the Denmark `app/build_db.py` and `app/build_cache.py`, so the next step is to run equivalent global builders and the Flask app.

## Project layout

```
DIKUs-Ark/
├── pipeline_v2/              # Working global H3 pipeline
│   ├── config.py             # Shared paths and constants
│   └── scripts/
│       ├── 01_create_unified_spatial.py
│       ├── 02_generate_tiles.py
│       ├── 03_buffered_h3_polyfill.py
│       ├── 04_aggregate_to_geoparquet.py
│       ├── 05_derive_res3_from_res7.py
│       ├── 06_generate_merged_gbif_from_h3.py
│       └── 07_export_tol.py
├── data/
│   ├── denmark/              # Denmark reference outputs (kept for comparison)
│   ├── sample/               # Denmark boundary + small sample subsets
│   └── global/               # Pipeline output directory (ignored by git)
├── BioDatasets/              # Raw IUCN, GOAT, EDGE, backbone, ToL data (ignored)
├── pyproject.toml
├── uv.lock
├── Justfile
└── README.md
```

## Running the pipeline

All scripts are self-contained and run from the repo root with `uv`:

```bash
# 0. Optional: pre-classify polygons for speed/memory (run once)
uv run python pipeline_v2/scripts/preclassify.py

# 1. Create the unified spatial manifest
uv run python pipeline_v2/scripts/01_create_unified_spatial.py

# 2. Generate 10x10 degree tiles
uv run python pipeline_v2/scripts/02_generate_tiles.py

# 3. Fill species polygons with H3 res-7 cells
uv run python pipeline_v2/scripts/03_buffered_h3_polyfill.py

# 4. Aggregate H3 parts into a single GeoParquet
uv run python pipeline_v2/scripts/04_aggregate_to_geoparquet.py

# 5. Derive res-3 cells from res-7
uv run python pipeline_v2/scripts/05_derive_res3_from_res7.py

# 6. Build the merged IUCN + GOAT + EDGE table
uv run python pipeline_v2/scripts/06_generate_merged_gbif_from_h3.py

# 7. Export matching higher-rank Tree-of-Life entries
uv run python pipeline_v2/scripts/07_export_tol.py
```

Outputs land in `data/global/`:

- `h3_res7_species.parquet`
- `h3_res3_species.parquet`
- `h3_merged_raw.parquet`
- `h3_tol.parquet`

## Useful commands

```bash
just sync       # install / sync dependencies
just upgrade    # upgrade uv.lock
just clean      # remove __pycache__ and caches
```

## Data

- `BioDatasets/` is gitignored and **not tracked**. It contains the raw global IUCN spatial ranges, GOAT TSVs, IUCN taxonomy/assessments, GBIF backbone, EDGE species list, and ToL TSVs.
- `data/global/` is also gitignored; it holds the generated Parquet files.

## Next steps

- Create global versions of `build_db.py`/`build_cache.py` to produce `Ark-IV.duckdb` and `precomputed_cache.duckdb`.
- Port/adapt the Denmark Flask app to consume those global DuckDBs.
