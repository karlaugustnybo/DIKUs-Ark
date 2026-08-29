#!/usr/bin/env python3
"""
01_create_unified_spatial.py
────────────────────────────────
Creates a manifest of all spatial_*.parquet parts.
The actual data remains in Parquet files — no unified DuckDB needed.

Output: pipeline_v2/temp/unified_manifest.parquet
Columns: table_name (str), parquet_path (str), row_count (int)
"""
from __future__ import annotations
import sys
import time
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent.parent))

from pipeline_v2.config import TEMP_DIR, ensure_dirs


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ensure_dirs()
    parquet_dir = TEMP_DIR / "_unified_parts"
    manifest_path = TEMP_DIR / "unified_manifest.parquet"

    log("Building manifest from Parquet parts ...")
    t0 = time.time()
    rows = {"table_name": [], "parquet_path": [], "row_count": []}
    for pq_file in sorted(parquet_dir.glob("spatial_*.parquet")):
        pf = pq.ParquetFile(pq_file)
        rows["table_name"].append(pq_file.stem)
        rows["parquet_path"].append(str(pq_file))
        rows["row_count"].append(pf.metadata.num_rows)

    schema = pa.schema([
        pa.field("table_name", pa.string()),
        pa.field("parquet_path", pa.string()),
        pa.field("row_count", pa.int64()),
    ])
    table = pa.Table.from_pydict(rows, schema=schema)
    pq.write_table(table, manifest_path)

    total = sum(rows["row_count"])
    log(f"Manifest: {len(rows['table_name'])} files, {total:,} total rows")
    log(f"Written to {manifest_path} in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
