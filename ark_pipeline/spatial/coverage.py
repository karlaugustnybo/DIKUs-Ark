"""Polygon-to-H3 kernels used by the replacement spatial pipeline.

The current semantic profiles use any-touch intersection, with bounded decision
simplification where configured. Historical kernels live in
archive/spatial/legacy_kernels.py; optional hierarchy experiments live in
archive/research/spatial/kernels.py.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator

import h3.api.numpy_int as h3_int
import numpy as np
import shapely
from h3ronpy import ContainmentMode
from h3ronpy.vector import cells_to_wkb_polygons, geometry_to_cells
from shapely import affinity, make_valid
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry


class GeometryCoverageError(ValueError):
    """Raised when a source geometry cannot safely produce coverage."""


MAX_WGS84_LOCAL_METRES_PER_DEGREE = 111_693.98


@dataclass(frozen=True)
class SpatialProfile:
    schema_version: int
    profile_id: str
    resolution: int
    candidate_mode: str
    candidate_tile_degrees: float
    large_candidate_tile_degrees: float
    large_candidate_bbox_degrees2: float
    production_kernel: str
    coarse_resolution: int
    hierarchy_start_resolution: int
    small_polygon_bbox_degrees2: float
    antimeridian_kernel: str
    candidate_simplification_degrees: float
    candidate_buffer_factor: float
    decision_simplification_degrees: float
    decision_simplification_min_bbox_degrees2: float
    max_decision_displacement_metres: float
    presence: tuple[int, ...]
    origin: tuple[int, ...]
    seasonality: tuple[int, ...]
    repair_method: str
    max_relative_planar_area_change: float
    fail_when_original_area_is_zero: bool
    geometry_batch_rows: int
    pair_write_rows: int
    digest: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class RepairAudit:
    original_valid: bool
    original_validity_issue: str | None
    method: str | None
    original_planar_area: float
    repaired_planar_area: float
    relative_planar_area_change: float


@dataclass(frozen=True)
class CoverageResult:
    cells: tuple[str, ...]
    candidate_cells: int
    repair: RepairAudit


@dataclass(frozen=True)
class NativeCoverageResult:
    """Compact production result; H3 indexes remain native uint64 values."""

    cells: np.ndarray
    candidate_cells: int
    repair: RepairAudit
    decision_simplification_applied: bool = False
    decision_simplification_bound_metres: float = 0.0
    decision_simplification_audit: dict | None = None


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_spatial_profile(path: Path) -> SpatialProfile:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    coverage = payload["coverage"]
    policy = payload["row_policy"]
    repair = payload["geometry_repair"]
    runtime = payload["runtime"]
    algorithm = payload["algorithm"]
    candidate_simplification_degrees = float(
        algorithm.get("candidate_simplification_degrees", 0.0)
    )
    candidate_buffer_factor = float(algorithm.get("candidate_buffer_factor", 1.05))
    decision_simplification_degrees = float(
        algorithm.get("decision_simplification_degrees", 0.0)
    )
    decision_simplification_min_bbox_degrees2 = float(
        algorithm.get("decision_simplification_min_bbox_degrees2", 0.0)
    )
    max_decision_displacement_metres = float(
        algorithm.get(
            "max_decision_displacement_metres",
            decision_simplification_degrees * MAX_WGS84_LOCAL_METRES_PER_DEGREE,
        )
    )
    candidate_tile_degrees = float(coverage["candidate_tile_degrees"])
    large_candidate_tile_degrees = float(
        coverage.get("large_candidate_tile_degrees", candidate_tile_degrees)
    )
    large_candidate_bbox_degrees2 = float(
        coverage.get("large_candidate_bbox_degrees2", math.inf)
    )
    if coverage["candidate_mode"] != "bbox_overlap":
        raise ValueError(
            "The exact profile requires conservative H3 bbox_overlap candidates"
        )
    if coverage["resolution"] != 7:
        raise ValueError("The authoritative profile must calculate resolution 7")
    if coverage["res3_derivation"] != "parent of an included resolution-7 cell":
        raise ValueError("Resolution 3 must be derived from resolution-7 membership")
    if candidate_simplification_degrees < 0:
        raise ValueError("candidate_simplification_degrees cannot be negative")
    if candidate_buffer_factor < 1:
        raise ValueError("candidate_buffer_factor must be at least 1")
    if decision_simplification_degrees < 0:
        raise ValueError("decision_simplification_degrees cannot be negative")
    if decision_simplification_min_bbox_degrees2 < 0:
        raise ValueError(
            "decision_simplification_min_bbox_degrees2 cannot be negative"
        )
    if max_decision_displacement_metres < 0:
        raise ValueError("max_decision_displacement_metres cannot be negative")
    displacement_bound = (
        decision_simplification_degrees * MAX_WGS84_LOCAL_METRES_PER_DEGREE
    )
    if displacement_bound > max_decision_displacement_metres + 1e-9:
        raise ValueError(
            "decision simplification tolerance exceeds the configured metre budget"
        )
    if not 0 < candidate_tile_degrees <= 180:
        raise ValueError("candidate_tile_degrees must be in (0, 180]")
    if not 0 < large_candidate_tile_degrees <= 180:
        raise ValueError("large_candidate_tile_degrees must be in (0, 180]")
    if large_candidate_bbox_degrees2 < 0:
        raise ValueError("large_candidate_bbox_degrees2 cannot be negative")
    return SpatialProfile(
        schema_version=int(payload["schema_version"]),
        profile_id=str(payload["profile_id"]),
        resolution=int(coverage["resolution"]),
        candidate_mode=str(coverage["candidate_mode"]),
        candidate_tile_degrees=candidate_tile_degrees,
        large_candidate_tile_degrees=large_candidate_tile_degrees,
        large_candidate_bbox_degrees2=large_candidate_bbox_degrees2,
        production_kernel=str(algorithm["production_kernel"]),
        coarse_resolution=int(algorithm["coarse_resolution"]),
        hierarchy_start_resolution=int(algorithm["hierarchy_start_resolution"]),
        small_polygon_bbox_degrees2=float(
            algorithm["small_polygon_bbox_degrees2"]
        ),
        antimeridian_kernel=str(algorithm["antimeridian_kernel"]),
        candidate_simplification_degrees=candidate_simplification_degrees,
        candidate_buffer_factor=candidate_buffer_factor,
        decision_simplification_degrees=decision_simplification_degrees,
        decision_simplification_min_bbox_degrees2=(
            decision_simplification_min_bbox_degrees2
        ),
        max_decision_displacement_metres=max_decision_displacement_metres,
        presence=tuple(int(value) for value in policy["presence"]),
        origin=tuple(int(value) for value in policy["origin"]),
        seasonality=tuple(int(value) for value in policy.get("seasonality", [])),
        repair_method=str(repair["method"]),
        max_relative_planar_area_change=float(
            repair["max_relative_planar_area_change"]
        ),
        fail_when_original_area_is_zero=bool(repair["fail_when_original_area_is_zero"]),
        geometry_batch_rows=int(runtime["geometry_batch_rows"]),
        pair_write_rows=int(runtime["pair_write_rows"]),
        digest=digest,
        raw=payload,
    )


def polygonal_parts(geometry: BaseGeometry | None) -> BaseGeometry | None:
    """Return only polygonal members, recursively preserving their union."""
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    parts: list[Polygon] = []
    for member in getattr(geometry, "geoms", ()):
        polygonal = polygonal_parts(member)
        if isinstance(polygonal, Polygon):
            parts.append(polygonal)
        elif isinstance(polygonal, MultiPolygon):
            parts.extend(polygonal.geoms)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def prepare_geometry(
    geometry: BaseGeometry,
    profile: SpatialProfile,
) -> tuple[BaseGeometry, RepairAudit]:
    """Apply the profile's deterministic repair policy or fail explicitly."""
    polygonal = polygonal_parts(geometry)
    if polygonal is None:
        raise GeometryCoverageError("geometry has no non-empty polygonal component")
    original_area = float(polygonal.area)
    if polygonal.is_valid:
        return polygonal, RepairAudit(
            original_valid=True,
            original_validity_issue=None,
            method=None,
            original_planar_area=original_area,
            repaired_planar_area=original_area,
            relative_planar_area_change=0.0,
        )

    from shapely.validation import explain_validity

    issue = explain_validity(polygonal)
    if profile.repair_method != "shapely.make_valid":
        raise GeometryCoverageError(f"unsupported repair method: {profile.repair_method}")
    repaired = polygonal_parts(make_valid(polygonal))
    if repaired is None or repaired.is_empty or not repaired.is_valid:
        raise GeometryCoverageError(f"make_valid did not produce valid polygonal coverage: {issue}")
    repaired_area = float(repaired.area)
    if original_area == 0:
        relative_change = math.inf if repaired_area else 0.0
    else:
        relative_change = abs(repaired_area - original_area) / abs(original_area)
    if profile.fail_when_original_area_is_zero and original_area == 0 and repaired_area > 0:
        raise GeometryCoverageError(
            f"repair needs review because original planar area is zero: {issue}"
        )
    if relative_change > profile.max_relative_planar_area_change:
        raise GeometryCoverageError(
            "repair needs review because planar area changed by "
            f"{relative_change:.3%}: {issue}"
        )
    return repaired, RepairAudit(
        original_valid=False,
        original_validity_issue=issue,
        method=profile.repair_method,
        original_planar_area=original_area,
        repaired_planar_area=repaired_area,
        relative_planar_area_change=relative_change,
    )


def _unwrap_ring(coords: Iterable[tuple[float, ...]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    previous: float | None = None
    for coordinate in coords:
        longitude, latitude = float(coordinate[0]), float(coordinate[1])
        if previous is not None:
            while longitude - previous > 180:
                longitude -= 360
            while longitude - previous < -180:
                longitude += 360
        result.append((longitude, latitude))
        previous = longitude
    return result


def _ring_crosses_dateline(ring) -> bool:
    coordinates = np.asarray(ring.coords)
    return bool(np.any(np.abs(np.diff(coordinates[:, 0])) > 180))


def _unwrap_polygon(polygon: Polygon) -> Polygon:
    # Already split/provider-normalized polygons need no transformation. In a
    # wide global shell, a hole can legitimately be >180 degrees away from the
    # vertex-average longitude. Shifting it toward that average moves it outside
    # the shell, even though neither ring crosses the date line.
    if not _ring_crosses_dateline(polygon.exterior) and not any(
        _ring_crosses_dateline(ring) for ring in polygon.interiors
    ):
        return polygon
    shell = _unwrap_ring(polygon.exterior.coords)
    shell_polygon = Polygon(shell)
    if not shell_polygon.is_valid:
        raise GeometryCoverageError(
            "Longitude unwrapping produced an invalid shell: "
            + shapely.is_valid_reason(shell_polygon)
        )
    shapely.prepare(shell_polygon)
    shell_min, _, shell_max, _ = shell_polygon.bounds
    holes = []
    for index, interior in enumerate(polygon.interiors):
        hole = Polygon(_unwrap_ring(interior.coords))
        if not hole.is_valid:
            raise GeometryCoverageError(
                f"Longitude unwrapping produced an invalid hole {index}: "
                + shapely.is_valid_reason(hole)
            )
        hole_min, _, hole_max, _ = hole.bounds
        first = math.ceil((shell_min - hole_min) / 360)
        last = math.floor((shell_max - hole_max) / 360)
        candidates = []
        for turns in range(first, last + 1):
            shifted = affinity.translate(hole, xoff=turns * 360) if turns else hole
            if shell_polygon.covers(shifted):
                candidates.append(shifted)
        if len(candidates) != 1:
            raise GeometryCoverageError(
                f"Cannot place unwrapped hole {index} unambiguously inside its shell "
                f"({len(candidates)} placements)"
            )
        holes.append(candidates[0].exterior.coords)
    result = Polygon(shell, holes)
    if not result.is_valid:
        raise GeometryCoverageError(
            "Longitude unwrapping produced invalid geometry: "
            + shapely.is_valid_reason(result)
        )
    return result


def unwrap_antimeridian(geometry: BaseGeometry) -> BaseGeometry:
    """Represent each ring continuously so dateline intersections are local."""
    if isinstance(geometry, Polygon):
        return _unwrap_polygon(geometry)
    if isinstance(geometry, MultiPolygon):
        original = list(geometry.geoms)
        polygons = [_unwrap_polygon(polygon) for polygon in original]
        if all(a is b for a, b in zip(original, polygons)):
            return geometry
        result = MultiPolygon(polygons)
        if not result.is_valid:
            raise GeometryCoverageError(
                "Longitude unwrapping produced invalid components: "
                + shapely.is_valid_reason(result)
            )
        return result
    raise GeometryCoverageError(f"expected polygonal geometry, got {geometry.geom_type}")


def _iter_polygons(geometry: BaseGeometry) -> Iterator[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms


def _canonical_piece(piece: BaseGeometry) -> BaseGeometry:
    centre = (piece.bounds[0] + piece.bounds[2]) / 2
    turns = math.floor((centre + 180) / 360)
    return affinity.translate(piece, xoff=-360 * turns)


def _iter_longitude_normalized_pieces(
    geometry: BaseGeometry,
) -> Iterator[BaseGeometry]:
    """Yield polygon pieces whose longitudes are all within [-180, 180].

    Unwrapped antimeridian rings may extend into an adjacent 360-degree world.
    Clipping each component against the world windows it actually intersects
    and translating those pieces back to the canonical interval avoids both
    world-spanning polygons and h3ronpy hangs without dropping either side of
    the antimeridian.
    """
    world = box(-180, -90, 180, 90)
    for polygon in _iter_polygons(geometry):
        min_x, _, max_x, _ = polygon.bounds
        first_turn = math.floor((min_x + 180) / 360)
        final_turn = math.ceil((max_x + 180) / 360)
        for turn in range(first_turn, final_turn):
            window = affinity.translate(world, xoff=turn * 360)
            clipped = polygonal_parts(polygon.intersection(window))
            if clipped is None or clipped.is_empty:
                continue
            yield affinity.translate(clipped, xoff=-turn * 360)


def _safe_for_polyfill(geometry: BaseGeometry) -> BaseGeometry:
    """Clip geometry to [-180, 180] so h3ronpy polyfill cannot hang.

    h3ronpy's Rust ``geometry_to_cells`` may hang on coordinates outside
    the valid longitude range. Antimeridian unwrapping can produce such
    coordinates for global-range geometries. This function clips to the
    normal range and returns only the polygonal parts, so every cell
    whose center is inside the clipped geometry is also a candidate for
    the original. The clipping is conservative — it can only remove
    area, never add it — so no valid candidate cells are lost.
    """
    min_x, _, max_x, _ = geometry.bounds
    if min_x >= -180 and max_x <= 180:
        return geometry
    clipped = geometry.intersection(box(-180, -90, 180, 90))
    result = polygonal_parts(clipped)
    if result is None or result.is_empty:
        return geometry
    return result


def _sorted_unique_chunks(chunks: list[np.ndarray]) -> np.ndarray:
    """Deduplicate uint64 chunks without NumPy's high-overhead hash table."""
    if not chunks:
        return np.empty(0, dtype=np.uint64)
    if len(chunks) == 1:
        values = np.asarray(chunks.pop(), dtype=np.uint64)
    else:
        values = np.concatenate(chunks)
        chunks.clear()
    if values.size < 2:
        return values
    values.sort(kind="quicksort")
    keep = np.empty(values.size, dtype=bool)
    keep[0] = True
    np.not_equal(values[1:], values[:-1], out=keep[1:])
    return values[keep]


def _candidate_tile_size(geometry: BaseGeometry, profile: SpatialProfile) -> float:
    """Choose the measured large-range partition without changing semantics."""
    if _bbox_area(geometry) >= profile.large_candidate_bbox_degrees2:
        return profile.large_candidate_tile_degrees
    return profile.candidate_tile_degrees


def _candidate_tile_jobs(geometry, tile_size):
    for polygon in _iter_polygons(geometry):
        min_x, min_y, max_x, max_y = polygon.bounds
        x = math.floor(min_x / tile_size) * tile_size
        while x < max_x or math.isclose(x, max_x):
            y = math.floor(min_y / tile_size) * tile_size
            while y < max_y or math.isclose(y, max_y):
                yield polygon, x, y, tile_size
                y += tile_size
            x += tile_size


def _fill_candidate_tile(job, resolution, include_guaranteed):
    polygon, x, y, tile_size = job
    clipped = polygon.intersection(box(x, y, x + tile_size, y + tile_size))
    polygonal = polygonal_parts(clipped)
    empty = np.empty(0, dtype=np.uint64)
    if polygonal is None or polygonal.is_empty:
        return empty, empty
    canonical = _canonical_piece(polygonal)
    candidates = np.asarray(geometry_to_cells(
        canonical, resolution, ContainmentMode.IntersectsBoundary,
    ), dtype=np.uint64)
    guaranteed = np.asarray(geometry_to_cells(
        canonical, resolution, ContainmentMode.ContainsBoundary,
    ), dtype=np.uint64) if include_guaranteed else empty
    return candidates, guaranteed


def _iter_candidate_cell_batches(
    geometry: BaseGeometry,
    profile: SpatialProfile,
    resolution: int,
    *,
    include_guaranteed: bool = True,
    tile_budget=None,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield bounded tile results; helpers share the existing spatial budget."""
    from functools import partial

    from ark_pipeline.runtime.progress import emit, enabled

    tile_size = _candidate_tile_size(geometry, profile)
    tiles_done = tiles_total = candidates_done = 0
    if enabled() or tile_budget is not None:
        for component in _iter_polygons(geometry):
            x0, y0, x1, y1 = component.bounds
            tiles_total += (math.floor((x1 - math.floor(x0 / tile_size) * tile_size) / tile_size) + 1) * (math.floor((y1 - math.floor(y0 / tile_size) * tile_size) / tile_size) + 1)
    jobs = _candidate_tile_jobs(geometry, tile_size)
    fill = partial(_fill_candidate_tile, resolution=resolution, include_guaranteed=include_guaranteed)
    parallel = tile_budget is not None and tiles_total >= 2 * tile_budget.workers
    results = tile_budget.map(fill, jobs) if parallel else map(fill, jobs)
    try:
        for candidates, guaranteed in results:
            candidates_done += int(candidates.size)
            if candidates.size or guaranteed.size:
                yield candidates, guaranteed
            tiles_done += 1
            emit(phase=f"Polygon grid · {candidates_done:,} candidate cells", completed=tiles_done, total=tiles_total,
                 fraction=min(0.99, tiles_done / tiles_total) if tiles_total else None, unit="polygon grid tiles",
                 tile_workers=tile_budget.peak_workers if parallel else 1)
    finally:
        if hasattr(results, "close"):
            results.close()


def _candidate_cells_at_resolution(
    geometry: BaseGeometry,
    profile: SpatialProfile,
    resolution: int,
    *,
    include_guaranteed: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    candidate_chunks: list[np.ndarray] = []
    guaranteed_chunks: list[np.ndarray] = []
    for candidates, guaranteed in _iter_candidate_cell_batches(
        geometry,
        profile,
        resolution,
        include_guaranteed=include_guaranteed,
    ):
        candidate_chunks.append(candidates)
        if guaranteed.size:
            guaranteed_chunks.append(guaranteed)
    return (
        _sorted_unique_chunks(candidate_chunks),
        _sorted_unique_chunks(guaranteed_chunks),
    )


def _candidate_cells_untiled(
    geometry: BaseGeometry,
    profile: SpatialProfile,
    resolution: int,
) -> tuple[np.ndarray, np.ndarray]:
    canonical = _canonical_piece(geometry)
    return (
        np.unique(
            np.asarray(
                geometry_to_cells(
                    canonical,
                    resolution,
                    ContainmentMode.IntersectsBoundary,
                ),
                dtype=np.uint64,
            )
        ),
        np.unique(
            np.asarray(
                geometry_to_cells(
                    canonical,
                    resolution,
                    ContainmentMode.ContainsBoundary,
                ),
                dtype=np.uint64,
            )
        ),
    )


def cell_polygon(cell: str) -> Polygon:
    native = np.asarray([h3_int.str_to_int(cell)], dtype=np.uint64)
    return _native_cell_polygons(native)[0]


def _native_cell_polygons(cells: np.ndarray) -> np.ndarray:
    """Build local, valid H3 boundaries via h3ronpy's vectorized Rust path.

    H3 serializes coordinates in the conventional [-180, 180] interval. A
    cell crossing the antimeridian therefore arrives as a world-spanning
    polygon unless its ring is unwrapped. Only the handful of affected cells
    take the Python repair path; all other cells remain vectorized.
    """
    if not cells.size:
        return np.empty(0, dtype=object)
    wkbs = cells_to_wkb_polygons(np.asarray(cells, dtype=np.uint64))
    polygons = np.asarray(shapely.from_wkb(wkbs), dtype=object)
    bounds = np.asarray(shapely.bounds(polygons), dtype=np.float64)
    antimeridian = np.flatnonzero(bounds[:, 2] - bounds[:, 0] > 180)
    for index in antimeridian:
        polygons[index] = _unwrap_polygon(polygons[index])
    return polygons


def _intersection_across_antimeridian(
    left: BaseGeometry, right: BaseGeometry
) -> BaseGeometry | None:
    pieces: list[BaseGeometry] = []
    for turn in (-1, 0, 1):
        shifted = affinity.translate(right, xoff=turn * 360)
        try:
            if not left.intersects(shifted):
                continue
            intersection = left.intersection(shifted)
        except shapely.errors.GEOSException:
            continue
        polygonal = polygonal_parts(intersection)
        if polygonal is not None and not polygonal.is_empty:
            pieces.append(polygonal)
    if not pieces:
        return None
    try:
        unioned = shapely.union_all(np.asarray(pieces, dtype=object))
    except shapely.errors.GEOSException:
        return polygonal_parts(pieces[0]) if len(pieces) == 1 else None
    return polygonal_parts(unioned)


def _one_ring(cells: np.ndarray) -> np.ndarray:
    """Return cells plus their immediate neighbours as sorted uint64 values."""
    if not cells.size:
        return cells
    chunks = [h3_int.grid_disk(cell, 1) for cell in cells]
    return np.unique(np.concatenate(chunks))


def _children_at_resolution(cells: np.ndarray, resolution: int) -> np.ndarray:
    if not cells.size:
        return cells
    return np.unique(
        np.concatenate(
            [h3_int.cell_to_children(cell, resolution) for cell in cells]
        )
    )


def _safe_full_cells(full_cells: np.ndarray, frontier: np.ndarray) -> np.ndarray:
    """Find cells whose complete one-ring safety envelope is fully covered."""
    if not full_cells.size or not frontier.size:
        return np.empty(0, dtype=np.uint64)
    full_lookup = {int(cell) for cell in full_cells}
    full_frontier = np.intersect1d(frontier, full_cells, assume_unique=True)
    safe = [
        cell
        for cell in full_frontier
        if all(int(neighbour) in full_lookup for neighbour in h3_int.grid_disk(cell, 1))
    ]
    return np.asarray(safe, dtype=np.uint64)


def direct_any_touch_intersecting_cells_native(
    geometry: BaseGeometry,
    profile: SpatialProfile,
    *, tile_budget=None,
) -> NativeCoverageResult:
    """Return the exact resolution-7 H3 overlap set, including point touches.

    ``ContainmentMode.IntersectsBoundary`` is H3's overlap-at-any-point mode.
    It already includes cells wholly contained by the source, so unlike the
    legacy positive-area kernel it needs neither GEOS cell construction nor a
    second topological predicate pass.
    """
    from ark_pipeline.runtime.progress import emit

    emit(phase="Validate & unwrap polygon", fraction=None, force=True)
    prepared, repair = prepare_geometry(geometry, profile)
    unwrapped = unwrap_antimeridian(prepared)
    decision = unwrapped
    simplification_applied = False
    simplification_audit = {"method": "not-requested", "rejections": []}
    if (
        profile.decision_simplification_degrees > 0
        and _bbox_area(unwrapped)
        >= profile.decision_simplification_min_bbox_degrees2
    ):
        emit(phase=f"Simplify polygon · {profile.decision_simplification_degrees}°", fraction=None, force=True)
        decision = _simplified_decision_geometry(
            unwrapped, profile.decision_simplification_degrees, audit=simplification_audit
        )
        simplification_applied = decision is not unwrapped
        if not simplification_applied:
            reasons = "; ".join(f"{attempt['method']}: {', '.join(attempt['reasons'])}"
                                for attempt in simplification_audit["rejections"])
            emit("message", message=f"Simplification kept original geometry · {reasons}")
    result = _direct_any_touch_prepared(decision, profile, repair, tile_budget=tile_budget)
    return replace(
        result,
        decision_simplification_applied=simplification_applied,
        decision_simplification_audit=simplification_audit,
        decision_simplification_bound_metres=(
            profile.decision_simplification_degrees
            * MAX_WGS84_LOCAL_METRES_PER_DEGREE
            if simplification_applied
            else 0.0
        ),
    )


def _direct_any_touch_prepared(
    unwrapped: BaseGeometry,
    profile: SpatialProfile,
    repair: RepairAudit,
    *, tile_budget=None,
) -> NativeCoverageResult:
    """Direct any-touch kernel for an already prepared, unwrapped geometry."""
    chunks: list[np.ndarray] = []
    candidate_count = 0
    for overlap, _ in _iter_candidate_cell_batches(
        unwrapped,
        profile,
        profile.resolution,
        include_guaranteed=False,
        tile_budget=tile_budget,
    ):
        candidate_count += int(overlap.size)
        if overlap.size:
            chunks.append(overlap)
    from ark_pipeline.runtime.progress import emit

    emit(phase="Deduplicating polygon cells", fraction=None, force=True)
    cells = _sorted_unique_chunks(chunks)
    if not unwrapped.is_empty and not cells.size:
        raise GeometryCoverageError(
            "valid non-empty geometry produced no resolution-7 coverage"
        )
    return NativeCoverageResult(
        cells=cells,
        candidate_cells=candidate_count,
        repair=repair,
    )


def _bbox_area(geometry: BaseGeometry) -> float:
    min_x, min_y, max_x, max_y = geometry.bounds
    return max(0.0, max_x - min_x) * max(0.0, max_y - min_y)


def _simplified_decision_geometry(
    source: BaseGeometry,
    tolerance: float,
    *, audit: dict | None = None,
) -> BaseGeometry:
    """Return the explicit approximate decision geometry for a profile.

    The fast non-topological path is accepted when it remains valid and keeps
    every disconnected component. If it drops a component, a
    topology-preserving simplification is tried instead. Holes may collapse
    only at the configured tolerance scale; remote components may not vanish.
    """
    if tolerance <= 0:
        return source
    if audit is None:
        audit = {}
    audit.update(method="original", rejections=[], original_coordinates=int(shapely.get_num_coordinates(source)),
                 original_validity=shapely.is_valid_reason(source))
    source_polygons = list(_iter_polygons(source))

    def accepted(candidate, method):
        reasons = []
        if candidate is None or candidate.is_empty:
            reasons.append("empty or non-polygonal result")
        else:
            if not candidate.is_valid:
                reasons.append("invalid: " + shapely.is_valid_reason(candidate))
            components = len(list(_iter_polygons(candidate)))
            if components != len(source_polygons):
                reasons.append(f"component count changed: {len(source_polygons)} -> {components}")
        if reasons:
            audit["rejections"].append({"method": method, "reasons": reasons})
            return False
        audit.update(method=method, result_coordinates=int(shapely.get_num_coordinates(candidate)))
        return True

    simplified = polygonal_parts(
        shapely.simplify(source, tolerance, preserve_topology=False)
    )
    if accepted(simplified, "fast"):
        return simplified
    topology_preserved = polygonal_parts(
        shapely.simplify(source, tolerance, preserve_topology=True)
    )
    if not accepted(topology_preserved, "topology-preserving"):
        audit["result_coordinates"] = audit["original_coordinates"]
        return source
    return topology_preserved


def exact_intersecting_cells_native(
    geometry: BaseGeometry,
    profile: SpatialProfile,
    *, tile_budget=None,
) -> NativeCoverageResult:
    """Calculate coverage using the active any-touch semantic profiles.

    Bounded decision simplification is controlled and audited by the profile.
    Historical positive-area/centroid profiles belong to archive.spatial.
    """
    if profile.production_kernel != "direct-any-touch-v2":
        raise ValueError(
            f"unsupported production kernel: {profile.production_kernel}; "
            "historical kernels are available in archive.spatial.legacy_kernels"
        )
    return direct_any_touch_intersecting_cells_native(geometry, profile, tile_budget=tile_budget)


def exact_intersecting_cells(
    geometry: BaseGeometry,
    profile: SpatialProfile,
) -> CoverageResult:
    """Calculate resolution-7 membership using the semantic profile."""
    native = exact_intersecting_cells_native(geometry, profile)
    return CoverageResult(
        cells=tuple(h3_int.int_to_str(cell) for cell in native.cells),
        candidate_cells=native.candidate_cells,
        repair=native.repair,
    )


def native_parents(cells: np.ndarray, resolution: int) -> np.ndarray:
    """Derive H3 parents with vectorized uint64 operations.

    H3 stores the resolution in four bits at offset 52 and fifteen base-7
    child digits in three-bit fields. Parent derivation changes the resolution
    and marks every lower digit as the unused value 7. This is equivalent to
    ``cell_to_parent`` without a Python call per relationship.
    """
    if resolution < 0 or resolution > 15:
        raise ValueError("H3 resolution must be in [0, 15]")
    values = np.asarray(cells, dtype=np.uint64)
    resolution_mask = np.uint64(0xF) << np.uint64(52)
    lower_bits = 3 * (15 - resolution)
    unused_digits = (
        np.uint64(0)
        if lower_bits == 0
        else (np.uint64(1) << np.uint64(lower_bits)) - np.uint64(1)
    )
    return (
        (values & ~resolution_mask)
        | (np.uint64(resolution) << np.uint64(52))
        | unused_digits
    )


def res3_parents(res7_cells: Iterable[str]) -> tuple[str, ...]:
    native = np.fromiter(
        (h3_int.str_to_int(cell) for cell in res7_cells), dtype=np.uint64
    )
    return tuple(h3_int.int_to_str(cell) for cell in np.unique(native_parents(native, 3)))
