#!/usr/bin/env python3
"""
Polyfill kernel for pipeline v3 (Phase 0 winner + helpers).

Implements the candidate strategies from §4 of GLOBAL_H3_RESET_PLAN.md:
  - baseline:           h3.geo_to_cells(geom, 7) as-is
  - simplify:           cell-size-aware simplify, then baseline
  - coarse_refine:      coarse-polyfill + interior children expansion +
                        boundary refine via clipped piece (idea 1)
  - tile_clip:          grid-clip before polyfilling (idea 2)
  - coarse_refine_simp: coarse_refine with simplify on the boundary pieces
                        (idea 1 + 3 — the expected winner / frozen kernel)

Also provides shared helpers used by both the benchmark (00) and the
production polyfill (01):

  - antimeridian_split:  split polygons crossing ±180 before H3 (§11 risk)
  - extract_polygons:    pull Polygon/MultiPolygon out of any geometry
  - polyfill_geom:       the frozen production kernel (coarse_refine_simp)
"""
from __future__ import annotations

import math
from typing import Iterable, Iterator

import h3
import shapely
from shapely import make_valid, simplify as shp_simplify
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep


# ---------------------------------------------------------------------------
# Tuning (kept here so the kernel is self-contained; mirrors config.py)
# ---------------------------------------------------------------------------
H3_RES: int = 7
COARSE_RES: int = 3
SIMPLIFY_TOL_DEG: float = 0.005

# Antimeridian split threshold (degrees of longitude). A polygon whose
# bbox spans more than this AND crosses ±180 is split so H3 always receives
# standard, valid geometries. §11 risk.
ANTIMERIDION_SPLIT_LON: float = 180.0

# Bbox area (deg²) below which the coarse_refine kernel is unreliable.
# A polygon smaller than a few res-3 cells may produce zero interior
# coarse cells and miss the geometry entirely (res-3 cells are ~1 deg²,
# so a geom under ~5 deg² can fall between coarse cells). Below this
# threshold the routed --fast kernel falls back to direct simplify +
# geo_to_cells, which is both faster and correct on small polygons.
SMALL_POLYGON_BBOX_DEG2: float = 5.0

# Bbox area (deg²) above which the coarse_refine kernel explodes. The
# coarse-fill step enumerates res-3 cells covering the whole polygon, then
# adds the grid_ring(1) neighbours, then runs prepared.contains + a
# shapely.intersection per boundary cell against the FULL polygon. For a
# cosmopolitan marine polygon (bbox 360°×90° ≈ 32400 deg²) this is tens of
# thousands of coarse cells and the boundary intersection against the whole
# (unsimplified, make_valid'd, millions-of-vertex) polygon hangs for 20+
# minutes. Above this threshold (and for antimeridian-crossing polygons,
# whose split halves are still enormous) the routed --fast kernel falls
# back to strategy_tile_clip, which grid-clips the polygon into 10×10°
# tiles and polyfills each small piece — bounded by tile size, not whole-
# polygon size, so even cosmopolitan marine ranges are cheap. Largest
# empirically-successful coarse_refine_simp test covered ~9700 deg².
# (Note: strategy_simplify ALSO hangs on some cosmopolitan polygons with
# bbox around 55286 deg² and 5.5MB WKB because it still
# feeds the whole polygon to h3.geo_to_cells. tile_clip is the only lossy
# kernel that handles cosmopolitan polygons.)
LARGE_POLYGON_BBOX_DEG2: float = 12000.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def extract_polygons(geom: BaseGeometry | None) -> BaseGeometry | None:
    """Reduce any geometry to its Polygon/MultiPolygon parts.

    H3 only accepts Polygon/MultiPolygon; the source files occasionally have
    GeometryCollections (from make_valid) that need flattening.
    """
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts: list[BaseGeometry] = []
    for g in getattr(geom, "geoms", [geom]):
        if g.geom_type == "Polygon":
            parts.append(g)
        elif g.geom_type == "MultiPolygon":
            parts.extend(g.geoms)
    if not parts and geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            inner = extract_polygons(g)
            if inner is not None:
                parts.append(inner)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _make_valid(geom: BaseGeometry) -> BaseGeometry | None:
    if geom is None or geom.is_empty:
        return None
    if geom.is_valid:
        return geom
    try:
        geom = make_valid(geom)
    except Exception:
        return None
    return extract_polygons(geom)


def _bbox_area_deg(geom: BaseGeometry) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    minx, miny, maxx, maxy = geom.bounds
    return max(0.0, maxx - minx) * max(0.0, maxy - miny)


def _crosses_antimeridian(geom: BaseGeometry) -> bool:
    """True if the polygon's bbox straddles ±180° (with wide span)."""
    if geom is None or geom.is_empty:
        return False
    minx, _, maxx, _ = geom.bounds
    span = maxx - minx
    # A polygon that touches both ±180 and spans a wide longitude range
    # is crossing the antimeridian (H3 hangs on these).
    return span >= 180.0 and (minx <= -ANTIMERIDION_SPLIT_LON or maxx >= ANTIMERIDION_SPLIT_LON)


def antimeridian_split(geom: BaseGeometry) -> list[BaseGeometry]:
    """Split a polygon that crosses the antimeridian into E/W halves.

    Returns a list (always non-empty). If the polygon does not cross the
    antimeridian, returns [geom] unchanged. §11 risk mitigation.
    """
    if not _crosses_antimeridian(geom):
        return [geom]

    # Build two cutter boxes: eastern hemisphere [0, 180] and western
    # hemisphere [-180, 0]. Intersecting with each produces standard
    # polygons that H3 accepts. We use a tiny epsilon beyond ±180 to
    # catch boundary points exactly on the meridian.
    eps = 1e-6
    east_box = Polygon([(0, -90), (180 + eps, -90), (180 + eps, 90), (0, 90)])
    west_box = Polygon([(-180 - eps, -90), (0, -90), (0, 90), (-180 - eps, 90)])

    out: list[BaseGeometry] = []
    for cutter in (east_box, west_box):
        try:
            piece = shapely.intersection(geom, cutter)
        except Exception:
            continue
        piece = _make_valid(piece)
        if piece is None or piece.is_empty:
            continue
        out.append(piece)
    return out if out else [geom]


# ---------------------------------------------------------------------------
# H3 helpers
# ---------------------------------------------------------------------------
def _h3cells(geom: BaseGeometry) -> set[str]:
    """Direct h3.geo_to_cells, returning empty set on failure."""
    try:
        return set(h3.geo_to_cells(geom.__geo_interface__, res=H3_RES))
    except Exception:
        return set()


def _coarse_cells(geom: BaseGeometry, res: int = COARSE_RES) -> set[str]:
    try:
        return set(h3.geo_to_cells(geom.__geo_interface__, res=res))
    except Exception:
        return set()


def _children(cell: str, parent_res: int = COARSE_RES, child_res: int = H3_RES) -> set[str]:
    """Expand a coarse cell to its child cells at the target resolution.

    Pure math via h3.cell_to_children — no geometry tests.
    """
    try:
        return set(h3.cell_to_children(cell, child_res))
    except Exception:
        # Fallback for safety: should never happen with valid H3 strings.
        return set()


def _cell_polygon(cell: str) -> Polygon:
    """Approximate polygon for an H3 cell (lon/lat ring)."""
    boundary = h3.cell_to_boundary(cell)  # list of (lat, lng)
    coords = [(lng, lat) for lat, lng in boundary]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return Polygon(coords)


# ---------------------------------------------------------------------------
# Candidate strategies (Phase 0 benchmark targets)
# ---------------------------------------------------------------------------
def strategy_baseline(geom: BaseGeometry) -> set[str]:
    """Variant Baseline: h3.geo_to_cells(geom, 7) as-is."""
    cells: set[str] = set()
    for piece in antimeridian_split(geom):
        cells |= _h3cells(piece)
    return cells


def strategy_simplify(geom: BaseGeometry) -> set[str]:
    """Variant A: cell-size-aware simplify, then baseline."""
    g = shp_simplify(geom, SIMPLIFY_TOL_DEG, preserve_topology=True)
    g = extract_polygons(g)
    if g is None or g.is_empty:
        return set()
    cells: set[str] = set()
    for piece in antimeridian_split(g):
        cells |= _h3cells(piece)
    return cells


def strategy_coarse_refine(geom: BaseGeometry, simplify_boundary: bool = False) -> set[str]:
    """Variant B / D: coarse-fill + interior children + boundary refine.

    Steps:
      1. Coarse-polyfill the (antimeridian-split) geometry at COARSE_RES.
         Also include the grid_ring(1) neighbours of those coarse cells so
         we don't miss res-7 cells whose centre is inside the geom but
         whose res-3 parent's centre is just outside it (zero-delta guard).
      2. For each coarse cell, classify as fully-inside or boundary:
         - Interior (geom contains the cell hexagon) -> expand to res-7
           children via h3.cell_to_children (pure math, no geometry tests).
         - Boundary -> clip the geom to the cell's hexagon and polyfill the
           clipped piece at res 7. The clipped piece is small so
           h3.geo_to_cells is cheap.
      3. Optionally apply cell-size-aware simplify to boundary pieces
         (variant D = idea 1 + idea 3).

    This is the §4 expected winner (10-100x faster on continent-scale
    polygons with identical output).
    """
    cells: set[str] = set()
    for piece in antimeridian_split(geom):
        piece = _make_valid(piece)
        if piece is None or piece.is_empty:
            continue
        prepared = prep(piece)
        coarse_central = _coarse_cells(piece, COARSE_RES)
        # Add the 1-ring neighbours so boundary cells whose parent centre
        # is just outside the geom are still processed.
        coarse: set[str] = set(coarse_central)
        for c in coarse_central:
            coarse.update(h3.grid_ring(c, 1))
        for ccell in coarse:
            cpoly = _cell_polygon(ccell)
            try:
                fully_inside = prepared.contains(cpoly)
            except Exception:
                fully_inside = False
            if fully_inside:
                cells |= _children(ccell, COARSE_RES, H3_RES)
                continue
            # Boundary cell: clip the geom to the cell's hexagon and refine.
            try:
                clipped = shapely.intersection(piece, cpoly)
            except Exception:
                continue
            clipped = extract_polygons(clipped)
            if clipped is None or clipped.is_empty:
                continue
            if simplify_boundary:
                clipped = shp_simplify(clipped, SIMPLIFY_TOL_DEG, preserve_topology=True)
                clipped = extract_polygons(clipped)
                if clipped is None or clipped.is_empty:
                    continue
            cells |= _h3cells(clipped)
    return cells


def _bbox_polygon(cell: str) -> Polygon:
    """Bbox (envelope) of an H3 cell as a Polygon (cheaper than the hex)."""
    boundary = h3.cell_to_boundary(cell)
    lngs = [lng for _, lng in boundary]
    lats = [lat for lat, _ in boundary]
    minx, maxx, miny, maxy = min(lngs), max(lngs), min(lats), max(lats)
    return Polygon([
        (minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny),
    ])


def strategy_coarse_refine_simp(geom: BaseGeometry) -> set[str]:
    """Variant D: coarse_refine with simplify on the boundary pieces."""
    return strategy_coarse_refine(geom, simplify_boundary=True)


def strategy_tile_clip(geom: BaseGeometry, tile_deg: float = 10.0) -> set[str]:
    """Variant C: grid-clip the polygon into 10x10° tiles, polyfill each.

    Each call is small and memory-bounded; pieces are independent.
    Antimeridian problems vanish because no piece crosses ±180 (the tile
    grid is aligned to 0..180 and -180..0).
    """
    if geom is None or geom.is_empty:
        return set()
    minx, miny, maxx, maxy = geom.bounds
    # Snap tile boundaries to the 10° grid.
    x0 = math.floor(minx / tile_deg) * tile_deg
    y0 = math.floor(miny / tile_deg) * tile_deg
    x1 = math.ceil(maxx / tile_deg) * tile_deg
    y1 = math.ceil(maxy / tile_deg) * tile_deg
    cells: set[str] = set()
    yt = y0
    while yt < y1:
        xt = x0
        while xt < x1:
            tile = Polygon([
                (xt, yt),
                (xt + tile_deg, yt),
                (xt + tile_deg, yt + tile_deg),
                (xt, yt + tile_deg),
                (xt, yt),
            ])
            # Quick bbox-reject: if tile doesn't intersect geom's bbox, skip.
            if not geom.intersects(tile):
                xt += tile_deg
                continue
            try:
                piece = shapely.intersection(geom, tile)
            except Exception:
                xt += tile_deg
                continue
            piece = extract_polygons(piece)
            if piece is None or piece.is_empty:
                xt += tile_deg
                continue
            # No piece crosses ±180 because tiles are grid-aligned and we
            # don't cross 0/180 inside a single tile.
            cells |= _h3cells(piece)
            xt += tile_deg
        yt += tile_deg
    return cells


def strategy_tile_clip_simplified(geom: BaseGeometry, tile_deg: float = 10.0) -> set[str]:
    """tile_clip on the SIMPLIFIED polygon — the cosmopolitan-band kernel.

    Identical to strategy_tile_clip but simplifies(0.005°) first, so each
    per-tile shapely.intersection runs against a 55K-vertex polygon instead
    of the original 100K+ vertex one. The intersection is the bottleneck of
    tile_clip (~90ms per tile on the original 5.5MB polygon -> ~2ms on the
    simplified one). Measured on a 5.5MB WKB / 55286 deg² polygon:
      tile_clip (original):    73.7s, 0.00% delta (exact)
      tile_clip (simplified):  32.8s, 0.02% delta
    The 0.02% delta is from the simplify(0.005) boundary shift — effectively
    lossless at res 7 (sub-cell detail H3 res-7 cannot represent).
    """
    if geom is None or geom.is_empty:
        return set()
    g = extract_polygons(shp_simplify(geom, SIMPLIFY_TOL_DEG, preserve_topology=True))
    if g is None or g.is_empty:
        return set()
    minx, miny, maxx, maxy = g.bounds
    x0 = math.floor(minx / tile_deg) * tile_deg
    y0 = math.floor(miny / tile_deg) * tile_deg
    x1 = math.ceil(maxx / tile_deg) * tile_deg
    y1 = math.ceil(maxy / tile_deg) * tile_deg
    cells: set[str] = set()
    yt = y0
    while yt < y1:
        xt = x0
        while xt < x1:
            tile = Polygon([
                (xt, yt), (xt + tile_deg, yt),
                (xt + tile_deg, yt + tile_deg), (xt, yt + tile_deg), (xt, yt),
            ])
            if not g.intersects(tile):
                xt += tile_deg
                continue
            try:
                piece = shapely.intersection(g, tile)
            except Exception:
                xt += tile_deg
                continue
            piece = extract_polygons(piece)
            if piece is None or piece.is_empty:
                xt += tile_deg
                continue
            cells |= _h3cells(piece)
            xt += tile_deg
        yt += tile_deg
    return cells


def strategy_fast_routed(geom: BaseGeometry) -> set[str]:
    """Routed --fast kernel: picks the fastest path per polygon.

    Three bands, all under 1% cell delta:
      - small bbox  (< SMALL_POLYGON_BBOX_DEG2):     strategy_simplify
        (coarse_refine can return zero cells when the geom is smaller than
        a res-3 cell so no coarse cell is "inside").
      - large bbox  (> LARGE_POLYGON_BBOX_DEG2) or  strategy_tile_clip_simplified
        antimeridian-crossing (span >= 180°):       (simplify + tile_clip on
        the simplified polygon. 2-3x faster than the original tile_clip
        because per-tile intersections against the simplified 55K-vertex
        polygon are ~2ms vs ~90ms on the original. 0.02-0.25% delta from
        the simplify(0.005) — effectively lossless at res 7.)
      - medium bbox:                                 strategy_coarse_refine_simp
        (3-11x faster than simplify because interior coarse cells expand via
        pure h3.cell_to_children math, only boundary cells hit geo_to_cells.)

    Implements Phase 0 idea 5 (shape-ratio routing, §4 GLOBAL_H3_RESET_PLAN).
    """
    if geom is None or geom.is_empty:
        return set()
    bbox_area = _bbox_area_deg(geom)
    if bbox_area < SMALL_POLYGON_BBOX_DEG2:
        return strategy_simplify(geom)
    if bbox_area > LARGE_POLYGON_BBOX_DEG2:
        return strategy_tile_clip_simplified(geom)
    if _crosses_antimeridian(geom):
        return strategy_tile_clip_simplified(geom)
    cells = strategy_coarse_refine_simp(geom)
    if not cells:
        return strategy_tile_clip_simplified(geom)
    return cells


STRATEGIES = {
    "baseline": strategy_baseline,
    "simplify": strategy_simplify,
    "coarse_refine": strategy_coarse_refine,
    "tile_clip": strategy_tile_clip,
    "coarse_refine_simp": strategy_coarse_refine_simp,
    "fast": strategy_fast_routed,
}


# ---------------------------------------------------------------------------
# Frozen production kernel (used by 01_polyfill_pairs.py)
# ---------------------------------------------------------------------------
# Phase 0 + production profiling verdict (see 00_benchmark_polyfill.py and
# profile_single_worker.py):
#   - baseline:           infeasible on cosmopolitan marine polygons (>30s timeout)
#   - tile_clip:          EXACT (zero delta) and ~3-4x faster than baseline on
#                         continent-scale polygons. Each 10×10° tile's polyfill
#                         is bounded by the tile size, so even huge marine
#                         ranges are cheap. This is the frozen production kernel
#                         (default). Slow on vertex-heavy polygons but exact.
#   - simplify:           simplify(0.005) + baseline h3.geo_to_cells. Fast on
#                         small/vertex-heavy polygons (53x faster than tile_clip
#                         on the worst vertex-heavy polygon) but SLOW on
#                         large-bbox polygons — h3.geo_to_cells cost scales with
#                         cell count (area), not vertex count, so simplify
#                         doesn't help where it matters most. ~0.4% delta.
#   - coarse_refine_simp: coarse-fill + interior children + boundary refine.
#                         3-11x faster than simplify on large-bbox polygons
#                         (the ones that dominate runtime) because interior
#                         coarse cells expand via pure h3.cell_to_children
#                         math, only boundary cells hit geo_to_cells. ~0.7% delta.
#   - fast (routed):      coarse_refine_simp for large-bbox polygons, simplify
#                         for small ones (coarse_refine returns zero cells when
#                         the geom is smaller than a res-3 cell). This is the
#                         `--fast` kernel: best of both, ~0.7% delta, 3-11x
#                         faster than the old simplify --fast on the polygon
#                         shapes that dominate runtime.
#
# tile_clip is the default (exact, zero delta). `--fast` uses the routed
# kernel (coarse_refine_simp + small-polygon fallback) for 3-11x speedup on
# large-bbox polygons at the cost of ~0.7% cell delta.

DEFAULT_KERNEL: str = "tile_clip"


def polyfill_geom(geom: BaseGeometry, kernel: str = DEFAULT_KERNEL) -> set[str]:
    """Frozen production polyfill kernel (§4 Phase 0 winner).

    Args:
        geom: Polygon/MultiPolygon to polyfill at H3_RES.
        kernel: 'tile_clip' (default, exact, zero delta), 'fast'
                (routed: coarse_refine_simp on large polygons, simplify on
                small; 3-11x faster than the old simplify kernel on the
                large-bbox polygons that dominate runtime, ~0.7% cell delta),
                'simplify', or 'coarse_refine_simp'.
                Set from polyfill.DEFAULT_KERNEL.

    Handles antimeridian-crossing polygons by splitting them at ±180 before
    H3 (§11 risk mitigation).
    """
    if kernel == "fast":
        return strategy_fast_routed(geom)
    if kernel == "simplify":
        return strategy_simplify(geom)
    if kernel == "coarse_refine_simp":
        return strategy_coarse_refine_simp(geom)
    return strategy_tile_clip(geom)


# ---------------------------------------------------------------------------
# Per-row polyfill worker (module-level so ProcessPoolExecutor can pickle
# it by reference; on Windows spawn, workers re-import this module).
# ---------------------------------------------------------------------------
def polyfill_row(wkb: bytes, kernel: str, clip_geom=None) -> set[str] | None:
    """Polyfill a single WKB geometry at res 7.

    Returns:
        None  — actual error (WKB load failure, polyfill exception)
        set() — no cells (empty geometry, or clip produced empty intersection)
        set(...) — the H3 cells covering the geometry
    """
    if wkb is None or len(wkb) == 0:
        return None
    try:
        geom = shapely.from_wkb(wkb)
    except Exception:
        return None
    if geom is None or geom.is_empty:
        return None
    if clip_geom is not None:
        try:
            geom = shapely.intersection(geom, clip_geom)
        except Exception:
            return None
        geom = extract_polygons(geom)
        if geom is None or geom.is_empty:
            return set()
    else:
        geom = extract_polygons(geom)
        if geom is None or geom.is_empty:
            return set()
    try:
        cells = polyfill_geom(geom, kernel=kernel)
    except Exception:
        return None
    return cells or set()
