#!/usr/bin/env python3
"""
Common helpers shared by 02_merge_metrics.py and 03_derive_res3_metrics.py.

Keeps the metric-count schema, the species-metadata builder, and the
lat/lng writer in one place so res-7 and res-3 aggregation stay in sync.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import duckdb
import h3
import pyarrow as pa
import pyarrow.parquet as pq

import config  # noqa: E402  (imported by callers who add pipeline_v3 to sys.path)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output schema — matches §5 Step B / build_cache.py / denmark_prototype_plan.md
# One row per unique H3 cell (res-7 for script 02, res-3 for script 03).
# ---------------------------------------------------------------------------
METRICS_SCHEMA = pa.schema([
    pa.field("h3_index", pa.string()),
    pa.field("latitude", pa.float64()),
    pa.field("longitude", pa.float64()),
    pa.field("total_species", pa.int64()),
    pa.field("crit_endangered_count", pa.int64()),
    pa.field("endangered_count", pa.int64()),
    pa.field("vulnerable_count", pa.int64()),
    pa.field("near_threatened_count", pa.int64()),
    pa.field("data_deficient_count", pa.int64()),
    pa.field("least_concern_count", pa.int64()),
    pa.field("missing_species_dna", pa.int64()),
    pa.field("missing_genus_dna", pa.int64()),
    pa.field("missing_family_dna", pa.int64()),
    pa.field("dna_coverage_score", pa.int64()),  # average per cell (fresh per res)
])


# ---------------------------------------------------------------------------
# Species metadata temp table
# ---------------------------------------------------------------------------
def build_species_metadata(con: duckdb.DuckDBPyConnection) -> str:
    """Build a temp table with one row per distinct id_no + scoring columns.

    The 7 source files already carry redlistCategory, has_dna_species_level,
    genus_has_dna, family_has_dna, dna_coverage_score joined to geometry
    (§2 finding B). We DISTINCT on id_no so each species appears once even
    if it has multiple polygons or appears in multiple class files.

    NOTE: we do NOT filter on geom_wkb IS NOT NULL here — that would force
    DuckDB to read the multi-GB WKB column. The presence/origin filter is
    sufficient for the metadata (we only need scoring columns per species).
    """
    logger.info("Building species metadata temp table from %d source files...",
                len(config.SOURCE_FILES))
    t0 = time.time()

    # Metadata-only filter: presence/origin only, no geom_wkb check.
    meta_filter = "presence = 1 AND origin IN (1, 2)"

    union_parts = []
    for f in config.SOURCE_FILES:
        union_parts.append(
            f"SELECT id_no, redlistCategory, has_dna_species_level, "
            f"genus_has_dna, family_has_dna, dna_coverage_score "
            f"FROM read_parquet('{f.as_posix()}') "
            f"WHERE {meta_filter}"
        )
    union_sql = " UNION ALL ".join(union_parts)

    con.execute("SET preserve_insertion_order=false;")
    con.execute(f"""
        CREATE TEMP TABLE species_meta AS
        SELECT
            id_no,
            any_value(redlistCategory)         AS redlist_category,
            any_value(has_dna_species_level)   AS has_dna_species_level,
            any_value(genus_has_dna)           AS genus_has_dna,
            any_value(family_has_dna)          AS family_has_dna,
            avg(dna_coverage_score)            AS dna_coverage_score
        FROM ({union_sql})
        WHERE id_no IS NOT NULL
        GROUP BY id_no
    """)
    n = con.execute("SELECT COUNT(*) FROM species_meta").fetchone()[0]
    logger.info("  species_meta: %d distinct species (%.1fs)", n, time.time() - t0)
    return "species_meta"


# ---------------------------------------------------------------------------
# Lat/lng writer — Python pass over the aggregated Parquet
# ---------------------------------------------------------------------------
def add_lat_lng(agg_path: Path, out_path: Path) -> dict:
    """Read the aggregated Parquet, add latitude/longitude per cell via
    h3.cell_to_latlng, and write the final metrics Parquet with the
    canonical schema (atomic temp + rename).

    Reading/writing in row-group batches keeps memory flat even for the full
    planet (~10-20M res-7 cells with species, ~20k res-3 cells).
    """
    logger.info("Adding lat/lng and writing %s ...", out_path)
    t0 = time.time()
    pf = pq.ParquetFile(agg_path)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    writer = pq.ParquetWriter(str(tmp_path), METRICS_SCHEMA, compression="zstd")
    rows_written = 0

    for batch in pf.iter_batches(batch_size=50_000):
        pyd = batch.to_pydict()
        h3_indices = pyd["h3_index"]
        n = len(h3_indices)
        lats = [0.0] * n
        lngs = [0.0] * n
        for i, cell in enumerate(h3_indices):
            try:
                lat, lng = h3.cell_to_latlng(cell)
                lats[i] = lat
                lngs[i] = lng
            except Exception:
                lats[i] = 0.0
                lngs[i] = 0.0

        out_cols = {
            "h3_index": h3_indices,
            "latitude": lats,
            "longitude": lngs,
        }
        for col in [
            "total_species", "crit_endangered_count", "endangered_count",
            "vulnerable_count", "near_threatened_count", "data_deficient_count",
            "least_concern_count", "missing_species_dna", "missing_genus_dna",
            "missing_family_dna", "dna_coverage_score",
        ]:
            out_cols[col] = pyd[col]

        table = pa.Table.from_pydict(out_cols, schema=METRICS_SCHEMA)
        writer.write_table(table)
        rows_written += n
        if rows_written % 500_000 == 0:
            logger.info("  ... %d cells written (%.1fs)", rows_written, time.time() - t0)

    writer.close()
    os.replace(str(tmp_path), str(out_path))
    logger.info("  wrote %d cells to %s (%.1fs total)",
                rows_written, out_path, time.time() - t0)
    return {
        "output": str(out_path),
        "cells": rows_written,
        "elapsed_sec": round(time.time() - t0, 2),
    }
