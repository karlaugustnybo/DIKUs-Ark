"""Bounded feature batches with prepared, shared boundary intersections."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

import h3
import numpy as np
import shapely

from ark_pipeline.builders.coarse_cache import (
    BOUNDARY_TILE_PROPERTIES,
    METRIC_TILE_NAMES,
    METRICS,
    SYSTEM_TILE_PREFIXES,
    SYSTEMS,
    TILE_ZOOM_RANGES,
    antimeridian_safe_polygon,
)
from ark_pipeline.spatial.boundaries import JurisdictionIndex, cell_geometry

METRIC_COLUMNS = tuple(f"{metric}__{system.lower()}" for system in SYSTEMS for metric in METRICS)
PROPERTY_KEYS = tuple(
    f"{SYSTEM_TILE_PREFIXES[system]}_{METRIC_TILE_NAMES[metric]}"
    for system in SYSTEMS for metric in METRICS
)


class BoundaryBatchIndex:
    """Prepare the complex boundaries once; retain catalogue order for ties."""

    def __init__(self, index: JurisdictionIndex):
        self.codes = index.codes
        self.geometries = np.asarray(index._geometries, dtype=object)
        self.tree = index._tree
        shapely.prepare(self.geometries)

    @classmethod
    def from_path(cls, path: Path) -> BoundaryBatchIndex:
        return cls(JurisdictionIndex(path))

    def codes_for_geometries(self, cells: np.ndarray) -> list[tuple[str, ...]]:
        left, right = self.tree.query(cells)
        hits = shapely.intersects(self.geometries[right], cells[left])
        left, right = left[hits], right[hits]
        order = np.lexsort((right, left))
        codes: list[list[str]] = [[] for _ in cells]
        for cell, boundary in zip(left[order], right[order], strict=True):
            codes[cell].append(self.codes[boundary])
        return [tuple(value) for value in codes]


def geometries_for_cells(ids: list[str]) -> tuple[list[dict], np.ndarray]:
    """Build each ring once; preserve the established dateline lookup policy."""
    geojson = []
    groups: dict[int, list[int]] = defaultdict(list)
    polygons = np.empty(len(ids), dtype=object)
    for index, cell in enumerate(ids):
        ring = [[lon, lat] for lat, lon in h3.cell_to_boundary(cell)]
        geometry = antimeridian_safe_polygon(ring)
        geojson.append(geometry)
        if geometry["type"] == "Polygon":
            groups[len(geometry["coordinates"][0])].append(index)
        else:
            # This rare branch uses exactly the original clipping implementation.
            polygons[index] = cell_geometry(cell)
    for indexes in groups.values():
        polygons[indexes] = shapely.polygons([geojson[i]["coordinates"][0] for i in indexes])
    return geojson, polygons


def feature_batch(
    rows: list[tuple], resolution: int, indexes: dict[str, BoundaryBatchIndex],
) -> Iterable[dict]:
    ids = [row[0] for row in rows]
    geometry, polygons = geometries_for_cells(ids)
    codes = {key: index.codes_for_geometries(polygons) for key, index in indexes.items()}
    zoom = TILE_ZOOM_RANGES[resolution]
    for offset, row in enumerate(rows):
        properties = {"h3_index": row[0], "resolution": resolution}
        for framework, key in BOUNDARY_TILE_PROPERTIES.items():
            properties[key] = "|".join(codes[framework][offset]) if framework in codes else ""
        properties.update(zip(PROPERTY_KEYS, (int(value) for value in row[1:]), strict=True))
        yield {
            "type": "Feature",
            "tippecanoe": {"layer": f"res{resolution}", "minzoom": zoom["min"], "maxzoom": zoom["max"]},
            "properties": properties,
            "geometry": geometry[offset],
        }


def stream_query(connection, query: str, stream: TextIO, resolution: int,
                 indexes: dict[str, BoundaryBatchIndex], batch_size: int = 2048, progress=None) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    count = 0
    cursor = connection.execute(query)
    while rows := cursor.fetchmany(batch_size):
        for feature in feature_batch(rows, resolution, indexes):
            stream.write(json.dumps(feature, separators=(",", ":")) + "\n")
        count += len(rows)
        if progress:
            progress(count)
    return count
