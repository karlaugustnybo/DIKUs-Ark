#!/usr/bin/env python3
"""
02_generate_tiles.py
─────────────────────
Generate a global grid of 10x10 degree tiles, a status table for
resumable processing, and a special Denmark validation bbox.

Output: pipeline_v2/temp/tiles.duckdb
Tables:
  tiles (tile_id, minx, miny, maxx, maxy, area_km2)
  tile_status (tile_id, status)   -- 'pending' | 'running' | 'done' | 'failed'
"""
from __future__ import annotations
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent.parent))

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from pipeline_v2.config import (
    TILES_DB, TILES_TABLE, TILES_STATUS_TABLE, TILE_SIZE_DEG,
    DENMARK_BBOX, TEMP_DIR, ensure_dirs,
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def generate_tiles(tile_size: float) -> list[dict]:
    tiles = []
    tile_id = 0
    for lon in range(-180, 180, int(tile_size)):
        for lat in range(-90, 90, int(tile_size)):
            minx = float(lon)
            miny = float(lat)
            maxx = float(min(lon + tile_size, 180))
            maxy = float(min(lat + tile_size, 90))
            # Approx area in km2 (rough estimate at middle latitude)
            mid_lat = (miny + maxy) / 2
            deg_km_lat = 111.0
            deg_km_lon = 111.32 * abs(__import__('math').cos(__import__('math').radians(mid_lat)))
            area_km2 = (maxx - minx) * deg_km_lon * (maxy - miny) * deg_km_lat
            tiles.append({
                "tile_id": tile_id,
                "minx": minx,
                "miny": miny,
                "maxx": maxx,
                "maxy": maxy,
                "area_km2": round(area_km2, 2),
            })
            tile_id += 1
    return tiles


def main() -> int:
    ensure_dirs()
    log(f"Tiles DB: {TILES_DB}")

    # Remove existing
    for f in TILES_DB.parent.glob(f"{TILES_DB.name}*"):
        import subprocess
        subprocess.run(["cmd", "/c", "del", "/q", "/f", str(f)], check=False)

    # Generate global tiles + Denmark special tile
    log("Generating tiles (10x10 degrees) ...")
    t0 = time.time()
    tiles = generate_tiles(TILE_SIZE_DEG)

    # Add Denmark validation tile
    dm_minx, dm_miny, dm_maxx, dm_maxy = DENMARK_BBOX
    dm_mid_lat = (dm_miny + dm_maxy) / 2
    dm_deg_km_lon = 111.32 * abs(__import__('math').cos(__import__('math').radians(dm_mid_lat)))
    dm_area = (dm_maxx - dm_minx) * dm_deg_km_lon * (dm_maxy - dm_miny) * 111.0
    tiles.append({
        "tile_id": 99999,  # sentinel
        "minx": dm_minx,
        "miny": dm_miny,
        "maxx": dm_maxx,
        "maxy": dm_maxy,
        "area_km2": round(dm_area, 2),
    })

    log(f"Total tiles: {len(tiles)} ({len(tiles)-1} global + 1 Denmark)")

    # Write tiles table
    tile_schema = pa.schema([
        pa.field("tile_id", pa.int32()),
        pa.field("minx", pa.float64()),
        pa.field("miny", pa.float64()),
        pa.field("maxx", pa.float64()),
        pa.field("maxy", pa.float64()),
        pa.field("area_km2", pa.float64()),
    ])
    tile_tbl = pa.Table.from_pylist(tiles, schema=tile_schema)

    con = duckdb.connect(str(TILES_DB))
    tile_parq = TEMP_DIR / "tiles.parquet"
    pq.write_table(tile_tbl, tile_parq)
    con.execute(f"CREATE TABLE {TILES_TABLE} AS SELECT * FROM read_parquet('{tile_parq.as_posix()}')")
    con.execute(f"CREATE UNIQUE INDEX idx_tile_id ON {TILES_TABLE} (tile_id)")

    # Write status table (all pending)
    statuses = [{"tile_id": t["tile_id"], "status": "pending"} for t in tiles]
    status_schema = pa.schema([
        pa.field("tile_id", pa.int32()),
        pa.field("status", pa.string()),
    ])
    # Mark Denmark as a special "validate" status
    for s in statuses:
        if s["tile_id"] == 99999:
            s["status"] = "validate"
    status_tbl = pa.Table.from_pylist(statuses, schema=status_schema)
    status_parq = TEMP_DIR / "tile_status.parquet"
    pq.write_table(status_tbl, status_parq)
    con.execute(f"CREATE TABLE {TILES_STATUS_TABLE} AS SELECT * FROM read_parquet('{status_parq.as_posix()}')")
    con.execute(f"CREATE UNIQUE INDEX idx_status_tile_id ON {TILES_STATUS_TABLE} (tile_id)")

    con.close()
    log(f"Tile database ready in {time.time()-t0:.1f}s")
    log(f"  DB: {TILES_DB}")
    log(f"  Global tiles: {len(tiles)-1}")
    log(f"  Denmark bbox: {DENMARK_BBOX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
