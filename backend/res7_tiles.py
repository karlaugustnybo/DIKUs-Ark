"""On-demand resolution-7 H3 tiles backed by partitioned Parquet.

The fine global layer contains roughly 96 million H3 cells. Serving only the
few thousand cells intersecting the visible web tile avoids materializing a
second, tens-of-gigabytes PMTiles copy of the same aggregate data. The wire
format deliberately contains compact H3-index/metric arrays instead of GeoJSON
polygons: deck.gl can instance H3 cells directly, avoiding geometry transfer,
JSON object churn and polygon triangulation on the browser's main thread.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import duckdb
import h3

from app.build_cache import METRICS
from app.jurisdictions import load_jurisdiction_index

SYSTEM_NAMES = {
    "all": "all",
    "terrestrial": "Terrestrial",
    "freshwater": "Freshwater",
    "marine": "Marine",
}
REQUIRED_PARTITION_COLUMNS = {"h3_index"} | {
    f"{metric}__{system.lower()}"
    for system in SYSTEM_NAMES.values()
    for metric in METRICS
}


@lru_cache(maxsize=512)
def _partition_schema_is_current(
    path_string: str, modified_ns: int, size: int
) -> bool:
    """Validate a partition once per immutable file version."""
    del modified_ns, size  # Both values intentionally participate in the cache key.
    connection = duckdb.connect()
    try:
        columns = {
            row[0]
            for row in connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [path_string]
            ).fetchall()
        }
    except duckdb.Error:
        return False
    finally:
        connection.close()
    return REQUIRED_PARTITION_COLUMNS <= columns


def partition_schema_is_current(path: Path) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    return _partition_schema_is_current(
        str(path), metadata.st_mtime_ns, metadata.st_size
    )


@lru_cache(maxsize=8)
def _available_base_cells_at_version(
    parts_dir_string: str, directory_modified_ns: int,
) -> tuple[int, ...]:
    del directory_modified_ns  # It intentionally invalidates the directory scan.
    parts_dir = Path(parts_dir_string)
    cells: list[int] = []
    for path in parts_dir.glob("base_*.parquet"):
        suffix = path.stem.removeprefix("base_")
        if suffix.isdigit() and partition_schema_is_current(path):
            cells.append(int(suffix))
    return tuple(sorted(cells))


def aggregate_coverage(parts_dir: Path | None) -> tuple[tuple[int, ...], int]:
    """Return validated partitions and a cheap immutable-publication version.

    Aggregate parts are published with an atomic rename. The directory mtime
    therefore changes both when coverage grows and when a partition is
    replaced, allowing requests to avoid re-statting all 121 global files.
    """
    if parts_dir is None:
        return (), 0
    try:
        directory_modified_ns = parts_dir.stat().st_mtime_ns
    except OSError:
        return (), 0
    cells = _available_base_cells_at_version(
        str(parts_dir), directory_modified_ns
    )
    return cells, directory_modified_ns


def available_base_cells(parts_dir: Path | None) -> list[int]:
    return list(aggregate_coverage(parts_dir)[0])


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    scale = 2**z
    west = x / scale * 360 - 180
    east = (x + 1) / scale * 360 - 180

    def latitude(tile_y: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * tile_y / scale))))

    return west, latitude(y + 1), east, latitude(y)


def cells_for_tile(z: int, x: int, y: int) -> list[str]:
    west, south, east, north = tile_bounds(z, x, y)
    polygon = h3.LatLngPoly(
        [(south, west), (south, east), (north, east), (north, west)]
    )
    # Assign by centre so every H3 cell belongs to exactly one web tile. The
    # polygon itself may extend across that tile boundary without leaving gaps.
    return sorted(h3.polygon_to_cells(polygon, 7))


def base_cell(h3_index: str) -> int:
    return (h3.str_to_int(h3_index) >> 45) & 127


@lru_cache(maxsize=64)
def render_tile(
    parts_dir_string: str,
    z: int,
    x: int,
    y: int,
    system: str,
    coverage_version: int,
    jurisdiction_path_string: str = "",
    jurisdictions: tuple[str, ...] = (),
    admin1_path_string: str = "",
    admin1_boundaries: tuple[str, ...] = (),
    municipality_path_string: str = "",
    municipalities: tuple[str, ...] = (),
    eez_path_string: str = "",
    eezs: tuple[str, ...] = (),
    conservation_path_string: str = "",
    conservation_frameworks: tuple[str, ...] = (),
) -> bytes:
    del coverage_version  # It participates in the cache key as parts appear.
    if system not in SYSTEM_NAMES:
        raise ValueError(f"Unknown ecosystem system: {system}")
    cells = cells_for_tile(z, x, y)
    if not cells:
        return b'{"cells":[]}'
    parts_dir = Path(parts_dir_string)
    paths = [
        parts_dir / f"base_{cell}.parquet"
        for cell in sorted({base_cell(h3_index) for h3_index in cells})
    ]
    paths = [path for path in paths if partition_schema_is_current(path)]
    if not paths:
        return b'{"cells":[]}'

    suffix = SYSTEM_NAMES[system].lower()
    projection = ", ".join(
        f'"{metric}__{suffix}"' for metric in METRICS
    )
    connection = duckdb.connect()
    try:
        rows = connection.execute(
            f"SELECT h3_index, {projection} FROM read_parquet(?) "
            "WHERE h3_index BETWEEN ? AND ? AND h3_index = ANY(?) "
            "ORDER BY h3_index",
            [[str(path) for path in paths], cells[0], cells[-1], cells],
        ).fetchall()
    finally:
        connection.close()
    boundary_filters = {
        "admin0": (jurisdiction_path_string, frozenset(jurisdictions)),
        "admin1": (admin1_path_string, frozenset(admin1_boundaries)),
        "municipality": (municipality_path_string, frozenset(municipalities)),
        "eez": (eez_path_string, frozenset(eezs)),
        "conservation_framework": (
            conservation_path_string,
            frozenset(conservation_frameworks),
        ),
    }
    # Boundary membership is only needed to evaluate active filters. Loading
    # every geometry catalogue and intersecting every visible H3 cell made the
    # unfiltered map pay almost all of the filtered-map cost.
    active_boundaries = {
        framework: (load_jurisdiction_index(path), selected_codes)
        for framework, (path, selected_codes) in boundary_filters.items()
        if path and selected_codes
    }
    compact_cells: list[list[str | int]] = []
    for row in rows:
        if row[1] <= 0:
            continue
        codes = {
            framework: index.codes_for_cell(row[0])
            for framework, (index, _) in active_boundaries.items()
        }
        if any(
            selected_codes.isdisjoint(codes.get(framework, ()))
            for framework, (_, selected_codes) in active_boundaries.items()
        ):
            continue
        # The metric order is the stable order of app.build_cache.METRICS.
        # Sending values positionally removes dozens of repeated JSON keys per
        # cell. Geometry is reconstructed by H3HexagonLayer on the GPU-friendly
        # instanced path.
        compact_cells.append([row[0], *(int(value) for value in row[1:])])
    return json.dumps(
        {"cells": compact_cells},
        separators=(",", ":"),
    ).encode()
