#!/usr/bin/env python3
"""
05_optional_raw_merge.py  —  Optional raw IUCN+GOAT detail dump filtered by H3.

§5 Step G of GLOBAL_H3_RESET_PLAN.md.

This is a thin wrapper around the validated v2 script 06
(pipeline_v2/scripts/06_generate_merged_gbif_from_h3.py). It reads the
partitioned res-7 species-list dataset (or a res-3 parquet) to collect the
set of species IDs present in H3 cells, then rebuilds the raw IUCN + GOAT
+ EDGE merge filtered to those species from the BioDatasets/ raw files.

If the app still needs a raw IUCN+GOAT row dump for detail panels, run
this. It is SEPARATE from the H3 metrics output (script 02) and depends
on the res-3 species parquet because the v2 script expects that input
shape (h3_index, gbif_accepted_ids list column).

Usage:
    uv run python pipeline_v3/scripts/05_optional_raw_merge.py
    uv run python pipeline_v3/scripts/05_optional_raw_merge.py --h3 data/global/h3_res3_metrics.parquet
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> None:
    # We import argparse here to keep the module light if not used.
    parser = argparse.ArgumentParser(
        description="Optional raw IUCN+GOAT merge filtered by H3 species"
    )
    parser.add_argument("--h3", type=Path, default=config.H3_RES3_METRICS,
                        help="Input H3 metrics parquet (must carry the species ids "
                             "or the partitioned species-list dataset is used)")
    parser.add_argument("--out", type=Path, default=config.H3_MERGED_RAW,
                        help=f"Output Parquet (default: {config.H3_MERGED_RAW})")
    parser.add_argument("--raw-dir", type=Path,
                        default=config.ROOT / "BioDatasets",
                        help="Source data root (BioDatasets/)")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    # Delegate to the validated v2 script 06 implementation. We import it
    # as a module so its functions are reusable without a subprocess.
    v2_scripts = config.ROOT / "pipeline_v2" / "scripts"
    sys.path.insert(0, str(v2_scripts))
    import importlib
    mod = importlib.import_module("06_generate_merged_gbif_from_h3")

    logger.info("=" * 60)
    logger.info("SCRIPT 05 (v3): OPTIONAL RAW MERGE — delegates to v2 script 06")
    logger.info("=" * 60)
    logger.info("  H3 input : %s", args.h3)
    logger.info("  Raw dir  : %s", args.raw_dir)
    logger.info("  Output   : %s", args.out)

    # v2 script 06 expects an H3 parquet with a `gbif_accepted_ids` list
    # column. The v3 metrics parquet does NOT carry that (it's aggregated
    # counts). Point the user at the partitioned species-list dataset or
    # the v2 res3 species parquet if available.
    if "gbif_accepted_ids" not in _parquet_columns(args.h3):
        v2_res3 = config.ROOT / "data" / "global" / "h3_res3_species.parquet"
        if v2_res3.exists() and "gbif_accepted_ids" in _parquet_columns(v2_res3):
            logger.info("  metrics parquet has no id list — falling back to %s", v2_res3)
            args.h3 = v2_res3
        else:
            raise SystemExit(
                f"{args.h3} has no `gbif_accepted_ids` list column, and "
                f"no v2 res3 species parquet found at {v2_res3}. Run "
                f"04_partition_species_lists.py first or pass --h3 to a "
                f"parquet carrying the id lists."
            )

    result = mod._build_pipeline(
        h3_path=args.h3,
        out_path=args.out,
        raw_dir=args.raw_dir,
        threads=args.threads,
    )
    logger.info("Result: %s", result)


def _parquet_columns(path: Path) -> list[str]:
    import pyarrow.parquet as pq
    if not path.exists():
        return []
    try:
        return pq.ParquetFile(path).schema_arrow.names
    except Exception:
        return []


if __name__ == "__main__":
    main()
