# DIKUs-Ark: Visualization Plan

## Current Project State

### Data Pipeline (COMPLETE)
- **`01_merge_datasets.py`**: Marimo notebook that merges IUCN + GOAT via GBIF backbone taxonomy
  - 171,604 IUCN species → 98.7% matched to GBIF → 53.3% matched to GOAT
  - Scoring: `threat_score` (0-3), `dna_coverage_score` (0-4), `sampling_priority` = threat × (4 - coverage)
  - Priority tiers: P1 Critical (~1,013), P2 High (~2,846), P3 Medium (~43,489), P4 Low (~253), P5 Done (~1,025)
  - Key views: `dna_gaps` (47,262 threatened species with zero DNA), `family_gaps`
  - Raw source tables dropped after merge; final table is `merged_gbif`

- **`spatial_merge.py`**: Marimo notebook that loads 51 IUCN shapefiles into DuckDB
  - 30 taxonomic groups, 128,768 total polygon rows
  - Unified `all_spatial_ranges` VIEW (UNION ALL over 51 tables, adds `taxon_group` column)

- **`02_visualize.py`**: Marimo notebook — interactive polygon visualization with threat coloring
  - DuckDB → Lonboard pipeline validated
  - GPU-side filtering by taxon group and threat score via `DataFilterExtension`
  - Chaikin's corner-cutting polygon smoothing (area-adaptive alpha)
  - Both sample mode (Denmark) and full mode (attached databases)

- **`03_h3_gap_map.py`**: Marimo notebook — H3 hexagonal grid heatmap
  - Polyfill-based aggregation (fills each range polygon with H3 cells, not centroid)
  - Global view at resolution 3-4, regional at 4-7
  - Species explorer: look up threatened species within a specific H3 cell
  - Expedition view: full-resolution polygon rendering for local detail
  - Both sample mode and full mode

### Databases

| Database | Size | Key Tables/Views |
|----------|------|-------------------|
| `dna_gap_analysis.duckdb` | 885 MB | `merged_gbif` (171,604 rows), `dna_genera` (101,098), `dna_families` (8,278), `dna_gaps` (VIEW, 47,262), `family_gaps` (VIEW) |
| `spatial_all.duckdb` | 47.7 GB | `spatial_mammals`, `spatial_amphibians`, ... (51 tables), `all_spatial_ranges` (VIEW, 128,768 rows) |

### GeoParquet Schema

7 main dataset files under `<external-geodata-root>/iucn_ranges_v2/`:
`class=mammals.parquet`, `class=amphibians.parquet`, `class=reptiles.parquet`, `class=fishes.parquet`, `class=marine_groups.parquet`, `class=plants.parquet`, `class=freshwater_groups.parquet`

Each file contains: `id_no`, `sci_name`, `iucn_group`, `iucn_grouping` (VARCHAR[]), taxonomy columns, `geom_wkb` (WKB binary), IUCN status, DNA gap scores, GOAT sequencing fields, habitat flags.

### Key Columns

**`merged_gbif`**: `internalTaxonId`, `gbif_accepted_id`, `kingdomName`-`speciesName`, `redlistCategory`, `threat_score` (0-3), `dna_coverage_score` (0-4), `sampling_priority`, `match_method`, `has_dna_species_level`, `genus_has_dna`, `family_has_dna`, `assembly_level`, `sequencing_status`

**`all_spatial_ranges` / GeoParquet**: `taxon_group`/`iucn_group`, `id_no` (matches `internalTaxonId`), `sci_name`, `category`, `presence`, `origin`, `seasonal`, `kingdom`-`genus`, `geom`/`geom_wkb`, `iucn_grouping`, DNA gap columns

### Join Key
`merged_gbif.internalTaxonId` = `all_spatial_ranges.id_no` / GeoParquet `id_no`

---

## Visualization Implementation (COMPLETE)

### Step 1: Single Species Polygon (DONE)
**File**: `02_visualize.py`
Validated DuckDB → Lonboard pipeline with *Panthera tigris*.

**Critical Lonboard + DuckDB notes**:
- MUST use `con.sql()` NOT `con.execute()` — Lonboard needs the `DuckDBPyRelation` object for its Arrow export path
- Lonboard auto-converts `GEOMETRY` to WKB via replacement scan internally — no need for manual `ST_AsWKB()`
- Data must be in EPSG:4326 (already is)

### Step 2: Color by Threat Score (DONE)
**File**: `02_visualize.py`
Attached `dna_gap_analysis.duckdb` to spatial connection via `ATTACH ... AS gap_db (READ_ONLY)`, then joined in SQL. Rendered with `apply_continuous_cmap` using inferno colormap.

Also implemented Chaikin's corner-cutting polygon smoothing with area-adaptive alpha (small polygons get more smoothing than large ones).

### Step 3: Interactive Threat Map (DONE)
**File**: `02_visualize.py`
GPU-side filtering with `DataFilterExtension`:
- Taxon group dropdown + threat score slider
- Filter range updated on widget change (avoids re-creating the layer)
- Sample mode reads from `data/sample/spatial/denmark.parquet`, full mode from attached databases

### Step 4: H3 Hex Grid Heatmap (DONE)
**File**: `03_h3_gap_map.py`
Uses **polyfill** (not centroid) — fills each species range polygon with all H3 cells it covers:

```python
h3poly = h3.LatLngPoly(outer, *holes)
cells = h3.polygon_to_cells(h3poly, res)
```

Aggregated per-cell stats: `n_species`, `total_priority`, `total_threat`, `n_no_dna`

Features:
- Global heatmap at resolution 3 (from precomputed parquet)
- Interactive regional heatmap at resolution 4-7 (computed on-the-fly with polyfill)
- Species explorer: click an H3 cell, see which threatened species overlap it
- Expedition view: full-resolution polygon rendering for local detail
- Summary statistics table grouped by `iucn_group`

Precomputed via `scripts/precompute_h3.py`.

---

## Future Architecture

| Component | Responsibility | Why? |
|-----------|---------------|------|
| **DuckDB** | Spatial Joins & Aggregation | Blazing fast analytical queries; H3 support |
| **GeoParquet** | Data Format | Columnar, partitioned by taxon group, predicate pushdown |
| **GeoArrow** | Data Transfer | Zero-copy DuckDB → GPU path |
| **Deck.gl / Lonboard** | WebGL Rendering | Handles 100K+ features |

### Optimization Roadmap
1. ~~Simplify geometries~~ — DONE in queries via `ST_Simplify(geom, tolerance)`
2. ~~GeoParquet partitioning~~ — DONE: 7 main files with `iucn_grouping` column
3. ~~H3 grid~~ — DONE: polyfill-based aggregation, precomputed for global view
4. **Web app** — Wrap notebooks in a SvelteKit or Streamlit app for non-technical users
5. **GeoArrow streaming** — DuckDB → Arrow buffer → Deck.gl `GeoArrowLayer` for zero-copy GPU rendering

---

## File Locations

| File | Purpose |
|------|---------|
| `01_merge_datasets.py` | Data pipeline (COMPLETE) |
| `spatial_merge.py` | Spatial ETL (COMPLETE) |
| `02_visualize.py` | Interactive polygon visualization (COMPLETE) |
| `03_h3_gap_map.py` | H3 hex grid heatmap (COMPLETE) |
| `dna_gap_analysis.duckdb` | Tabular data (885 MB) |
| `spatial_all.duckdb` | Spatial data (47.7 GB) |
| `scripts/` | All data scripts (see `scripts/README.md`) |
| `data/sample/` | Small sample datasets for development |

## Dependencies

Current `pyproject.toml` deps: `duckdb`, `geoarrow-pyarrow`, `geopandas`, `h3`, `lonboard`, `marimo`, `matplotlib`, `numpy`, `palettable`, `pandas`, `polars`, `pyarrow`, `shapely`, `sqlglot`, `tol-sdk`
