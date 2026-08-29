#!/usr/bin/env python3
"""
06_export_tol.py  —  Tree-of-Life export for species in the H3 layer.

§5 Step H of GLOBAL_H3_RESET_PLAN.md.

The v2 script 07 (pipeline_v2/scripts/07_export_tol.py) is already
validated and operates on the merged_gbif parquet produced by script 05.
This is a thin wrapper that:
  1. Runs script 05 (raw merge) if its output is missing.
  2. Delegates to the v2 script 07 to filter the ToL TSV dataset to
     entries whose lowercased scientific_name matches a name in the
     merged output (kingdom → genus ranks).

Globally the matched set is much larger than Denmark but still
manageable with DuckDB batch inserts. Expect the kingdom/phylum gaps
documented in data/denmark/README.md.

Usage:
    uv run python pipeline_v3/scripts/06_export_tol.py
    uv run python pipeline_v3/scripts/06_export_tol.py --merged data/global/h3_merged_raw.parquet
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
    parser = argparse.ArgumentParser(
        description="Export Tree-of-Life entries for H3 species lineages"
    )
    parser.add_argument("--merged", type=Path, default=config.H3_MERGED_RAW,
                        help=f"Input merged parquet from script 05 (default: {config.H3_MERGED_RAW})")
    parser.add_argument("--out", type=Path, default=config.H3_TOL,
                        help=f"Output Parquet (default: {config.H3_TOL})")
    parser.add_argument("--raw-dir", type=Path,
                        default=config.ROOT / "BioDatasets",
                        help="Source data root (BioDatasets/)")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    v2_scripts = config.ROOT / "pipeline_v2" / "scripts"
    sys.path.insert(0, str(v2_scripts))
    import importlib
    mod = importlib.import_module("07_export_tol")

    logger.info("=" * 60)
    logger.info("SCRIPT 06 (v3): ToL EXPORT — delegates to v2 script 07")
    logger.info("=" * 60)
    logger.info("  Merged   : %s", args.merged)
    logger.info("  Raw dir  : %s", args.raw_dir)
    logger.info("  Output   : %s", args.out)

    if not args.merged.exists():
        raise SystemExit(
            f"Merged input not found: {args.merged}. Run "
            f"05_optional_raw_merge.py first to produce it."
        )

    result = mod._build_pipeline(
        merged_path=args.merged,
        out_path=args.out,
        raw_dir=args.raw_dir,
        threads=args.threads,
    )
    logger.info("Result: %s", result)


if __name__ == "__main__":
    main()
