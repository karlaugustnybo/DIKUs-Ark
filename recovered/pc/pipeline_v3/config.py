#!/usr/bin/env python3
"""
Shared configuration for the global H3 pipeline v3.

Pipeline v3 implements GLOBAL_H3_RESET_PLAN.md:
  - Reads 7 already-joined GeoParquet files from ARK_GEODATA_DIR
  - Streams raw (h3_index, id_no) pairs to per-file intermediate Parquet
  - Lets DuckDB perform out-of-core SELECT DISTINCT across the 7 files
  - Joins to species metadata (already in the same GeoParquet files) and
    aggregates to per-cell metric counts (h3_res7_metrics, h3_res3_metrics)
  - Stores the res7 ID-list layer as a Parquet dataset partitioned by H3
    res-2 parent (single source of truth for drill-down + counts)

All scripts import paths and tuning knobs from this module so the pipeline
has ONE configuration surface.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────
ROOT: Path = Path(__file__).resolve().parent.parent

PIPE_DIR: Path = ROOT / "pipeline_v3"
TEMP_DIR: Path = PIPE_DIR / "temp"
SCRIPTS_DIR: Path = PIPE_DIR / "scripts"
DATA_GLOBAL: Path = ROOT / "data" / "global"

# Intermediate raw (h3_index, id_no) pairs streamed from the polyfill
H3_PAIRS_DIR: Path = TEMP_DIR / "h3_pairs"

# Partitioned res7 ID-list dataset (Parquet partitioned by H3 res-2 parent)
H3_RES7_SPECIES_DIR: Path = DATA_GLOBAL / "h3_res7_species"

# Final metric-count outputs (one row per unique H3 cell)
H3_RES7_METRICS: Path = DATA_GLOBAL / "h3_res7_metrics.parquet"
H3_RES3_METRICS: Path = DATA_GLOBAL / "h3_res3_metrics.parquet"

# Optional raw IUCN+GOAT detail dump + ToL export
H3_MERGED_RAW: Path = DATA_GLOBAL / "h3_merged_raw.parquet"
H3_TOL: Path = DATA_GLOBAL / "h3_tol.parquet"

# Benchmark output (Phase 0)
BENCHMARK_OUT: Path = TEMP_DIR / "phase0_benchmark.json"

# Pre-create directories
for p in (TEMP_DIR, SCRIPTS_DIR, DATA_GLOBAL, H3_PAIRS_DIR):
    p.mkdir(parents=True, exist_ok=True)

# ── Source data ─────────────────────────────────────────────────────
# The 7 already-joined GeoParquet files. Each row is one IUCN range polygon
# with all scoring columns (redlistCategory, has_dna_species_level,
# genus_has_dna, family_has_dna, dna_coverage_score) already attached.
GEODATA_DIR: Path = Path(
    os.environ.get(
        "ARK_GEODATA_DIR",
        str(ROOT / "external_data" / "iucn_ranges_v2"),
    )
)
SOURCE_FILES: list[Path] = [
    GEODATA_DIR / "class=amphibians.parquet",
    GEODATA_DIR / "class=fishes.parquet",
    GEODATA_DIR / "class=freshwater_groups.parquet",
    GEODATA_DIR / "class=mammals.parquet",
    GEODATA_DIR / "class=marine_groups.parquet",
    GEODATA_DIR / "class=plants.parquet",
    GEODATA_DIR / "class=reptiles.parquet",
]

# Columns read from the source GeoParquet files (Step A, §5).
# id_no is the IUCN internal taxon identifier; the reset uses it as the
# species key for the (h3_index, id_no) pair relation.
SOURCE_COLUMNS: list[str] = [
    "id_no",
    "sci_name",
    "redlistCategory",
    "has_dna_species_level",
    "genus_has_dna",
    "family_has_dna",
    "dna_coverage_score",
    "terrestial",   # NOTE: source column is misspelled this way
    "freshwater",
    "marine",
    "presence",
    "origin",
    "geom_wkb",
]

# ── Row filter (Step A, §5) ───────────────────────────────────────────
# presence = 1  -> extant ranges only (drop extinct / uncertain)
# origin IN (1,2) -> native and reintroduced ranges
SOURCE_FILTER_SQL: str = (
    "presence = 1 AND origin IN (1, 2) AND geom_wkb IS NOT NULL"
)

# ── H3 / polyfill ────────────────────────────────────────────────────
H3_RES: int = 7
H3_PARENT_RES: int = 3
H3_PARTITION_RES: int = 2

# Coarse polyfill resolution (Phase 0 idea 1). Interior coarse cells are
# expanded to res-7 children via h3.cell_to_children (pure math); only
# boundary coarse cells are refined with h3.geo_to_cells on the clipped
# piece. Tunable: 3 is the default; 4 is finer (more, smaller boundary
# pieces) and may win for some polygon shapes.
COARSE_RES: int = 3

# Cell-size-aware simplification tolerance for boundary pieces (Phase 0
# idea 3). ~half a res-7 cell edge (~600 m ≈ 0.005°). Effectively lossless
# at res 7 because the polyfill cannot resolve sub-cell detail.
SIMPLIFY_TOL_DEG: float = 0.005

# ── Streaming / performance ─────────────────────────────────────────
# Rows read per batch from each source Parquet file. Kept small because
# some source WKB blobs are huge (up to 23 MB for marine polygons), so a
# 5k-row batch can be ~15 GB of WKB in RAM. 1000 rows × ~500 KB avg is
# a safe ~500 MB peak per batch.
READ_BATCH_ROWS: int = 1_000

# Sub-chunk size for the vectorized bbox pre-filter in --denmark mode.
# Each sub-chunk holds this many WKBs in RAM simultaneously for the
# shapely.from_wkb + shapely.bounds call. 250 rows × worst-case 23 MB
# WKB = ~5.5 GB peak — bounded and safe. Smaller = safer but slower.
BBOX_FILTER_CHUNK: int = 250

# (h3_index, id_no) pairs buffered in memory before flushing to the
# intermediate Parquet writer. ~200k pairs ≈ 200k * ~20 bytes ≈ 4 MB.
WRITE_BATCH: int = 200_000

# Adaptive worker pool cap. The polyfill uses multiprocessing — h3 does
# NOT release the GIL (profiled: 4 threads = 21.3s vs 5.45s single on a
# 946K-cell polygon, fully serial), so threads give zero polyfill
# parallelism. Processes are required for true parallelism. Tuned down
# for slow (huge-polygon) batches to avoid memory blowup (each worker
# holds WKB blobs in RAM).
MAX_WORKERS: int = min(8, int(os.cpu_count() or 1))

# ── DuckDB ───────────────────────────────────────────────────────────
DUCKDB_THREADS: int = min(8, int(os.cpu_count() or 1))
DUCKDB_MEMORY_LIMIT: str = "16GB"

# ── IUCN threat categories (build_cache.py ordering) ──────────────────
# These are the 6 categories included in the per-cell metric counts.
THREAT_CATEGORIES: dict[str, str] = {
    "crit_endangered_count": "Critically Endangered",
    "endangered_count": "Endangered",
    "vulnerable_count": "Vulnerable",
    "near_threatened_count": "Near Threatened",
    "data_deficient_count": "Data Deficient",
    "least_concern_count": "Least Concern",
}

# ── Denmark validation bbox (§10) ───────────────────────────────────
DENMARK_BBOX: tuple[float, float, float, float] = (8.0, 54.5, 15.5, 57.8)
DENMARK_BND_PATH: Path = (
    ROOT / "data" / "sample" / "denmark_prototype" / "denmark_boundary.parquet"
)


def ensure_dirs() -> None:
    """Ensure all working directories exist."""
    for p in (TEMP_DIR, DATA_GLOBAL, H3_PAIRS_DIR, H3_RES7_SPECIES_DIR):
        p.mkdir(parents=True, exist_ok=True)


def source_key(path: Path) -> str:
    """Stable short key for a source file (e.g. 'amphibians')."""
    # Source filenames look like 'class=amphibians.parquet'.
    name = path.name
    if "=" in name:
        return name.split("=", 1)[1].removesuffix(".parquet")
    return path.removesuffix(".parquet").stem


def pairs_path_for(source: Path | str) -> Path:
    """Return the intermediate Parquet path for a source file's pairs.

    Accepts either the source Path or the source_key string.
    """
    key = source if isinstance(source, str) else source_key(source)
    return H3_PAIRS_DIR / f"class={key}.parquet"
