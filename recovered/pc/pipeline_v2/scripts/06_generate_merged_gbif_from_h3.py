#!/usr/bin/env python3
"""
Script 06: Generate filtered IUCN + GOAT merge for species present in an H3 GeoParquet.

Reads **raw** source data and exports **all** original columns from both IUCN
and GOAT, plus computed Tree-of-Life DNA-sample booleans (genus→kingdom)
so downstream users know whether any member at that taxonomic rank has DNA
sequence data in GOAT (based on ebp_standard_criteria).

Usage (global)
    uv run python pipeline_v2/scripts/06_generate_merged_gbif_from_h3.py \
        --h3 data/global/h3_res3_species.parquet

Usage (Denmark test)
    uv run python pipeline_v2/scripts/06_generate_merged_gbif_from_h3.py \
        --h3 data/denmark/h3_res3_species.parquet \
        --out data/denmark/h3_merged_raw.parquet
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = ROOT / "BioDatasets"
DEFAULT_H3 = ROOT / "data" / "global" / "h3_res3_species.parquet"
DEFAULT_OUT = ROOT / "data" / "global" / "h3_merged_raw.parquet"

READ_CHUNK = 10_000
FILTER_BATCH = 50_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _collect_species_ids(h3_path: Path) -> set[int]:
    """Collect every integer found in the ``gbif_accepted_ids`` column."""
    logger.info("Scanning H3 species ids from %s ...", h3_path)
    t0 = time.time()
    pf = pq.ParquetFile(h3_path)
    species_ids: set[int] = set()
    n_rows = 0
    for i, batch in enumerate(pf.iter_batches(batch_size=READ_CHUNK)):
        pyd = batch.to_pydict()
        for lst in pyd["gbif_accepted_ids"]:
            for raw in lst:
                species_ids.add(int(raw))
            n_rows += 1
        del pyd, batch
        if (i + 1) % 10 == 0:
            logger.info("  ... %d rows read -> %d unique ids", n_rows, len(species_ids))
    pf.close()
    logger.info("Collected %d unique species ids (%.1fs)", len(species_ids), time.time() - t0)
    return species_ids


def _load_filter_table(con: duckdb.DuckDBPyConnection, species_ids: set[int]) -> None:
    """Load the species-id set into a temporary DuckDB table."""
    con.execute("CREATE TEMP TABLE _species_filter (id BIGINT)")
    n = 0
    batch: list[tuple[int]] = []
    for x in species_ids:
        batch.append((x,))
        if len(batch) >= FILTER_BATCH:
            con.executemany("INSERT INTO _species_filter VALUES (?)", batch)
            n += len(batch)
            batch.clear()
    if batch:
        con.executemany("INSERT INTO _species_filter VALUES (?)", batch)
        n += len(batch)
    logger.info("  temp table populated: %d ids", n)


def _build_pipeline(
    h3_path: Path,
    out_path: Path,
    raw_dir: Path,
    threads: int = 4,
) -> dict[str, Any]:
    if not raw_dir.exists():
        raise FileNotFoundError(f"Raw data directory not found: {raw_dir}")
    if not h3_path.exists():
        raise FileNotFoundError(f"H3 input not found: {h3_path}")

    species_ids = _collect_species_ids(h3_path)
    if not species_ids:
        raise RuntimeError("No species IDs found in H3 file")

    t0 = time.time()
    con = duckdb.connect(":memory:")
    con.execute(f"PRAGMA threads={threads}")
    con.execute(f"PRAGMA memory_limit='8GB'")

    # -- Phase 0: species filter table ---------------------------------
    _load_filter_table(con, species_ids)

    _tab = chr(9)

    # -----------------------------------------------------------------
    # Phase 1: GBIF name map (deterministic via _seq secondary sort)
    # -----------------------------------------------------------------
    logger.info("Building GBIF name map ...")
    taxon_tsv = raw_dir / "backbone" / "backbone" / "Taxon.tsv"
    con.execute(f"""
        CREATE TEMP TABLE gbif_backbone AS
        SELECT
            taxonID::VARCHAR            AS taxonID,
            acceptedNameUsageID::VARCHAR AS acceptedNameUsageID,
            canonicalName               AS canonicalName,
            taxonRank                   AS taxonRank,
            taxonomicStatus             AS taxonomicStatus,
            ROW_NUMBER() OVER (ORDER BY canonicalName, taxonID) AS _seq
        FROM read_csv_auto(
            '{taxon_tsv}',
            delim = '{_tab}',
            all_varchar = true,
            strict_mode = false
        )
        WHERE taxonRank = 'species'
          AND canonicalName IS NOT NULL
          AND canonicalName != ''
    """)
    con.execute("""
        CREATE TEMP TABLE gbif_name_map AS
        WITH ranked AS (
            SELECT
                canonicalName,
                COALESCE(acceptedNameUsageID, taxonID) AS gbif_id,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(canonicalName)
                    ORDER BY
                        CASE taxonomicStatus WHEN 'accepted' THEN 0 ELSE 1 END,
                        taxonomicStatus,
                        _seq
                ) AS rn
            FROM gbif_backbone
            WHERE taxonomicStatus IN (
                'accepted', 'synonym', 'homotypic synonym',
                'heterotypic synonym', 'proparte synonym'
            )
        )
        SELECT LOWER(canonicalName) AS canonical_lower, gbif_id
        FROM ranked
        WHERE rn = 1
    """)

    # -----------------------------------------------------------------
    # Phase 2: IUCN raw data (taxonomy + assessments) + GBIF match
    # -----------------------------------------------------------------
    logger.info("Loading IUCN raw data ...")
    tax_csv = raw_dir / "IUCN_Red_List" / "taxonomy.csv"
    ass_csv = raw_dir / "IUCN_Red_List" / "assessments.csv"
    con.execute(f"CREATE TEMP TABLE iucn_tax AS SELECT * FROM read_csv_auto('{tax_csv}')")
    con.execute(f"CREATE TEMP TABLE iucn_assm AS SELECT * FROM read_csv_auto('{ass_csv}')")

    tax_cols = [r[0] for r in con.execute("DESCRIBE iucn_tax").fetchall()]
    assm_cols = [r[0] for r in con.execute("DESCRIBE iucn_assm").fetchall()]
    overlap = {"internalTaxonId", "scientificName"}

    tax_select = [f'a."{c}"' for c in tax_cols]
    assm_select = [f'b."{c}"' for c in assm_cols if c not in overlap]
    gbif_match = """
        COALESCE(
            (SELECT gbif_id FROM gbif_name_map
             WHERE LOWER(a.genusName || ' ' || a.speciesName) = canonical_lower),
            NULL
        ) AS gbif_accepted_id
    """
    final_select = ", ".join(tax_select + assm_select + [gbif_match])

    con.execute(f"""
        CREATE TEMP TABLE iucn_merged AS
        SELECT {final_select}
        FROM iucn_tax a
        INNER JOIN iucn_assm b USING (internalTaxonId)
        ORDER BY internalTaxonId
    """)
    logger.info("  IUCN merged: %d rows",
                con.execute("SELECT COUNT(*) FROM iucn_merged").fetchone()[0])

    con.execute("""
        CREATE TEMP TABLE iucn_filtered AS
        SELECT * FROM iucn_merged
        WHERE CAST(gbif_accepted_id AS BIGINT) IN (SELECT id FROM _species_filter)
        ORDER BY internalTaxonId
    """)
    iucn_filt_n = con.execute("SELECT COUNT(*) FROM iucn_filtered").fetchone()[0]
    logger.info("  IUCN filtered: %d rows", iucn_filt_n)

    # -----------------------------------------------------------------
    # Phase 3: GOAT raw data → first row per species, filtered by H3
    # -----------------------------------------------------------------
    logger.info("Loading GOAT raw data ...")
    goat_tsv = raw_dir / "GoaT" / "goat_dataset.tsv"
    con.execute(f"""
        CREATE TEMP TABLE goat_raw AS
        SELECT *, ROW_NUMBER() OVER (ORDER BY taxon_id) AS _seq
        FROM read_csv_auto(
            '{goat_tsv}',
            delim = '{_tab}',
            all_varchar = true,
            strict_mode = false
        )
    """)

    con.execute("""
        ALTER TABLE goat_raw ADD COLUMN gbif_accepted_id VARCHAR;
        UPDATE goat_raw SET gbif_accepted_id = (
            SELECT gbif_id FROM gbif_name_map
            WHERE LOWER(goat_raw.species) = canonical_lower
        )
    """)

    con.execute("""
        CREATE TEMP TABLE goat_first AS
        SELECT * EXCLUDE (_seq, rn)
        FROM (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY gbif_accepted_id
                    ORDER BY _seq
                ) AS rn
            FROM goat_raw
            WHERE gbif_accepted_id IS NOT NULL
              AND CAST(gbif_accepted_id AS BIGINT) IN (SELECT id FROM _species_filter)
        )
        WHERE rn = 1
        ORDER BY gbif_accepted_id
    """)
    goat_n = con.execute("SELECT COUNT(*) FROM goat_first").fetchone()[0]
    logger.info("  GOAT first-row: %d rows", goat_n)

    # -----------------------------------------------------------------
    # Phase 3b: EDGE raw data -> first row per species, filtered by H3
    # -----------------------------------------------------------------
    logger.info("Loading EDGE raw data ...")
    edge_tsv = raw_dir / "2024_EDGE_species_external_with_gbif.tsv"
    if edge_tsv.exists():
        con.execute(f"""
            CREATE TEMP TABLE edge_raw AS
            SELECT *, ROW_NUMBER() OVER (ORDER BY group_name, species) AS _seq
            FROM read_csv_auto(
                '{edge_tsv}',
                delim = '{_tab}',
                all_varchar = true,
                strict_mode = false
            )
        """)
        con.execute(f"""
            CREATE TEMP TABLE edge_first AS
            SELECT
                gbif_accepted_id,
                "group_name"     AS "edge_group_name",
                "rl_id"          AS "edge_rl_id",
                "order_"         AS "edge_order",
                "family"         AS "edge_family",
                "species"        AS "edge_species",
                "common_names"   AS "edge_common_names",
                "rl_category"    AS "edge_rl_category",
                "tbl_median"     AS "edge_tbl_median",
                "ed_median"      AS "edge_ed_median",
                "edge_median"    AS "edge_median",
                "edge_rank"      AS "edge_rank",
                "tier"           AS "edge_tier",
                "countries"      AS "edge_countries",
                "common_name"    AS "edge_common_name",
                "class_"         AS "edge_class",
                "kew_name"       AS "edge_kew_name",
                "edge_rank_alt"  AS "edge_rank_alt",
                "distribution_code"    AS "edge_distribution_code",
                "distribution_name"    AS "edge_distribution_name",
                "systems"        AS "edge_systems",
                "clade"          AS "edge_clade",
                "suborder"       AS "edge_suborder",
                "genus"          AS "edge_genus",
                "fishbase_id"    AS "edge_fishbase_id"
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY gbif_accepted_id
                        ORDER BY _seq
                    ) AS rn
                FROM edge_raw
                WHERE gbif_accepted_id IS NOT NULL
                  AND CAST(gbif_accepted_id AS BIGINT) IN (SELECT id FROM _species_filter)
            )
            WHERE rn = 1
            ORDER BY gbif_accepted_id
        """)
        edge_n = con.execute("SELECT COUNT(*) FROM edge_first").fetchone()[0]
        logger.info("  EDGE first-row: %d rows", edge_n)
    else:
        logger.warning("  EDGE file not found: %s — skipping.", edge_tsv)
        con.execute("""
            CREATE TEMP TABLE edge_first (
                gbif_accepted_id VARCHAR,
                edge_group_name VARCHAR,
                edge_rl_id VARCHAR,
                edge_order VARCHAR,
                edge_family VARCHAR,
                edge_species VARCHAR,
                edge_common_names VARCHAR,
                edge_rl_category VARCHAR,
                edge_tbl_median VARCHAR,
                edge_ed_median VARCHAR,
                edge_median VARCHAR,
                edge_rank VARCHAR,
                edge_tier VARCHAR,
                edge_countries VARCHAR,
                edge_common_name VARCHAR,
                edge_class VARCHAR,
                edge_kew_name VARCHAR,
                edge_rank_alt VARCHAR,
                edge_distribution_code VARCHAR,
                edge_distribution_name VARCHAR,
                edge_systems VARCHAR,
                edge_clade VARCHAR,
                edge_suborder VARCHAR,
                edge_genus VARCHAR,
                edge_fishbase_id VARCHAR
            )
        """)
        edge_n = 0

    # -----------------------------------------------------------------
    # Phase 4: FULL OUTER JOIN and export (deterministic order)
    # -----------------------------------------------------------------
    logger.info("Building full-outer-join and exporting ...")
    fi_cols = [r[0] for r in con.execute("DESCRIBE iucn_filtered").fetchall()]
    fg_cols = [r[0] for r in con.execute("DESCRIBE goat_first").fetchall()]

    select_parts: list[str] = [
        "COALESCE(fi.gbif_accepted_id, fg.gbif_accepted_id, fe.gbif_accepted_id) AS gbif_accepted_id"
    ]
    for c in fi_cols:
        if c == "gbif_accepted_id":
            continue
        select_parts.append(f'fi."{c}" AS "{c}"')
    for c in fg_cols:
        if c == "gbif_accepted_id":
            continue
        select_parts.append(f'fg."{c}" AS "{c}"')
    fe_cols = [r[0] for r in con.execute("DESCRIBE edge_first").fetchall()]
    for c in fe_cols:
        if c == "gbif_accepted_id":
            continue
        select_parts.append(f'fe."{c}" AS "{c}"')

    final_select_sql = "\n, ".join(select_parts)

    export_sql = f"""
        SELECT {final_select_sql}
        FROM iucn_filtered fi
        FULL OUTER JOIN goat_first fg
            ON fi.gbif_accepted_id = fg.gbif_accepted_id
        FULL OUTER JOIN edge_first fe
            ON COALESCE(fi.gbif_accepted_id, fg.gbif_accepted_id) = fe.gbif_accepted_id
        ORDER BY gbif_accepted_id
    """

    con.execute(f"COPY ({export_sql}) TO '{out_path}' (FORMAT 'parquet', COMPRESSION 'zstd')")
    n_rows = con.execute(f"SELECT COUNT(*) FROM ({export_sql})").fetchone()[0]
    con.close()
    elapsed = time.time() - t0
    logger.info("Done: %d rows written to %s (%.1fs)", n_rows, out_path, elapsed)
    return {
        "output_path": str(out_path),
        "h3_species_ids": len(species_ids),
        "iucn_matched": iucn_filt_n,
        "goat_matched": goat_n,
        "edge_matched": edge_n,
        "rows_exported": n_rows,
        "elapsed_sec": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build raw IUCN+GOAT merge filtered by H3 species"
    )
    parser.add_argument(
        "--h3",
        type=Path,
        default=DEFAULT_H3,
        help=f"Input H3 GeoParquet (default: {DEFAULT_H3})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output Parquet (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=RAW_DIR,
        help=f"Source data root (default: {RAW_DIR})",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="DuckDB threads (default: 4)",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("SCRIPT 06: RAW IUCN+GOAT MERGE FILTERED BY H3")
    logger.info("=" * 60)
    logger.info("  H3        : %s", args.h3)
    logger.info("  Raw dir   : %s", args.raw_dir)
    logger.info("  Output    : %s", args.out)

    result = _build_pipeline(
        h3_path=args.h3,
        out_path=args.out,
        raw_dir=args.raw_dir,
        threads=args.threads,
    )
    logger.info("Result: %s", result)


if __name__ == "__main__":
    main()
