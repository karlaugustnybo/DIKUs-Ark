#!/usr/bin/env python3
"""Validate raw or aggregated H3 list files before the species crosswalk exists."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from app.build_db import inspect_h3_input


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--res3", type=Path, help="Resolution-3 Parquet input")
    parser.add_argument("--res7", type=Path, help="Resolution-7 Parquet input")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Also scan every list for duplicate IDs and globally check duplicate cells.",
    )
    args = parser.parse_args()
    if args.res3 is None and args.res7 is None:
        parser.error("provide --res3, --res7, or both")

    connection = duckdb.connect()
    try:
        reports = {}
        if args.res3 is not None:
            reports["3"] = inspect_h3_input(
                connection, 3, args.res3.resolve(), deep=args.deep
            )
        if args.res7 is not None:
            reports["7"] = inspect_h3_input(
                connection, 7, args.res7.resolve(), deep=args.deep
            )
    finally:
        connection.close()

    failures = [
        f"resolution {resolution}: {failure}"
        for resolution, report in reports.items()
        for failure in report["failures"]
    ]
    result = {
        "version": 1,
        "status": "ok" if not failures else "failed",
        "resolutions": reports,
        "failures": failures,
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
        print(f"Wrote {args.output}")
    print(encoded, end="")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
