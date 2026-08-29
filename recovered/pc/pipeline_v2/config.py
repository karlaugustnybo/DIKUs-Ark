#!/usr/bin/env python3
"""
Shared configuration for the global H3 pipeline v2.
All scripts import from this module.
"""
from __future__ import annotations
import os
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────
ROOT: Path = Path(__file__).resolve().parent.parent

# Source DB (global spatial data)
SOURCE_DB: Path = ROOT / "spatial_clean.duckdb"

# Pipeline working directories
PIPE_DIR: Path = ROOT / "pipeline_v2"
TEMP_DIR: Path = PIPE_DIR / "temp"
OUT_DIR: Path = PIPE_DIR / "outputs"
SCRIPTS_DIR: Path = PIPE_DIR / "scripts"
DATA_GLOBAL: Path = ROOT / "data" / "global"

# Pre-create directories
for p in (TEMP_DIR, OUT_DIR, SCRIPTS_DIR, DATA_GLOBAL):
    p.mkdir(parents=True, exist_ok=True)

# ── Spatial parameters ───────────────────────────────────────────────
TILE_SIZE_DEG: float = 10.0          # tile width/height in degrees
PIXEL_SIZE_M: int = 1_000            # 1 km
EPSG_WGS84: int = 4326               # WGS 84
EPSG_EQUAL_AREA: int = 6933          # WGS 84 / NSIDC EASE-Grid 2.0 Global
TARGET_CRS_WKT: str = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]'
)

# H3 resolution
H3_RES: int = 7

# ── Processing limits ────────────────────────────────────────────────
# RAM guard: if a tile would require more than this many pixels for rasterization,
# split it into smaller sub-tiles before rasterizing.
MAX_RASTER_PIXELS: int = 200_000_000  # ~200M pixels ≈ 200 km² at 1 km res (safe on 32 GB)

# Denmark bounding box (WGS 84) used for validation
DENMARK_BBOX: tuple[float, float, float, float] = (8.0, 54.5, 15.5, 57.8)

# ── Thread pool ────────────────────────────────────────────────────
MAX_WORKERS: int = min(10, int(os.cpu_count() or 1))

# ── Files ───────────────────────────────────────────────────────────
# Script 01: unified spatial table
UNIFIED_DB: Path = TEMP_DIR / "unified_spatial.duckdb"
UNIFIED_TABLE: str = "unified_spatial"

# Script 02: tiles
TILES_DB: Path = TEMP_DIR / "tiles.duckdb"
TILES_TABLE: str = "tiles"
TILES_STATUS_TABLE: str = "tile_status"

# Script 03: raw raster → H3 pairs (directory of Parquet chunks)
RAW_PAIRS_DIR: Path = TEMP_DIR / "raw_pairs"
RAW_PAIRS_SCHEMA = ["h3_index", "gbif_accepted_id"]

# Script 05: aggregated DuckDB
AGG_DB: Path = OUT_DIR / "h3_aggregated.duckdb"
AGG_TABLE: str = "h3_species"

# Script 04: final GeoParquet (res7)
FINAL_GEOPARQUET: Path = DATA_GLOBAL / "h3_res7_species.parquet"

# Script 05: derived res3 GeoParquet (from res7 parents)
FINAL_RES3_GEOPARQUET: Path = DATA_GLOBAL / "h3_res3_species.parquet"

# Script 06: filtered merged_gbif (tabular species matching H3 cells)
FINAL_MERGED_GBIF: Path = DATA_GLOBAL / "h3_merged_gbif.parquet"

# Script 07: ToL entries matching Denmark lineages
FINAL_TOL: Path = DATA_GLOBAL / "h3_tol.parquet"


def ensure_dirs() -> None:
    """Ensure all working directories exist."""
    for p in (RAW_PAIRS_DIR,):
        p.mkdir(parents=True, exist_ok=True)
