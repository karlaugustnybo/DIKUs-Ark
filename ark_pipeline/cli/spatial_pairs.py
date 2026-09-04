#!/usr/bin/env python3
"""Build IUCN range, point, and HydroBASINS relationships at H3 resolution 7."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import multiprocessing
import os
import time
import tomllib
import zipfile
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import duckdb
import numpy as np
import psutil
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pyogrio
import shapely
from h3ronpy.vector import coordinates_to_cells

from ark_pipeline.runtime.progress import emit, tracked_stage
from ark_pipeline.runtime.provenance import (
    atomic_json,
    code_fingerprint,
    dependency_identity,
    iso_now,
    receipt_is_current,
    runtime_identity,
    sha256,
)
from ark_pipeline.runtime.resources import automatic_workers, configured_count
from ark_pipeline.spatial.coverage import (
    GeometryCoverageError,
    NativeCoverageResult,
    SpatialProfile,
    exact_intersecting_cells_native,
    load_spatial_profile,
)
from ark_pipeline.spatial.tile_parallel import TileBudget

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = REPOSITORY_ROOT / "config" / "spatial_semantics_iucn_richness_v3.toml"
REQUIRED_FIELDS = {"id_no", "presence", "origin", "seasonal"}
PAIR_SCHEMA = pa.schema(
    [
        ("h3_index", pa.uint64()),
        ("iucn_sis_id", pa.int64()),
    ]
)
AUDIT_SCHEMA = pa.schema(
    [
        ("source_logical_name", pa.string()),
        ("source_layer", pa.string()),
        ("source_row", pa.int64()),
        ("iucn_sis_id", pa.int64()),
        ("decision", pa.string()),
        ("presence", pa.int64()),
        ("origin", pa.int64()),
        ("seasonal", pa.int64()),
        ("original_valid", pa.bool_()),
        ("repair_method", pa.string()),
        ("validity_issue", pa.string()),
        ("relative_planar_area_change", pa.float64()),
        ("candidate_cells", pa.int64()),
        ("output_cells", pa.int64()),
        ("geometry_wall_seconds", pa.float64()),
        ("decision_simplification_applied", pa.bool_()),
        ("decision_simplification_bound_metres", pa.float64()),
        ("decision_simplification_audit", pa.string()),
    ]
)
SUPPLEMENTAL_AUDIT_SCHEMA = pa.schema(
    [
        ("source_logical_name", pa.string()),
        ("source_member", pa.string()),
        ("source_format", pa.string()),
        ("decision", pa.string()),
        ("source_rows", pa.int64()),
        ("output_relationships", pa.int64()),
    ]
)
BASIN_RELATION_SCHEMA = pa.schema(
    [
        ("hybas_id", pa.int64()),
        ("iucn_sis_id", pa.int64()),
    ]
)
BASIN_CELL_SCHEMA = pa.schema(
    [
        ("hybas_id", pa.int64()),
        ("h3_index", pa.uint64()),
    ]
)
BASIN_AUDIT_SCHEMA = pa.schema(
    [
        ("hybas_id", pa.int64()),
        ("region", pa.string()),
        ("level", pa.int8()),
        ("original_valid", pa.bool_()),
        ("repair_method", pa.string()),
        ("validity_issue", pa.string()),
        ("relative_planar_area_change", pa.float64()),
        ("candidate_cells", pa.int64()),
        ("output_cells", pa.int64()),
        ("geometry_wall_seconds", pa.float64()),
        ("decision_simplification_applied", pa.bool_()),
        ("decision_simplification_bound_metres", pa.float64()),
        ("decision_simplification_audit", pa.string()),
    ]
)

SUPPLEMENTAL_REQUIRED_FIELDS = {
    "point": {"id_no", "presence", "origin", "seasonal", "dec_lat", "dec_long"},
    "hydrobasin": {"hybas_id", "id_no", "presence", "origin", "seasonal"},
}
HYDROBASIN_REGION_CODES = {
    1: "af",
    2: "eu",
    3: "si",
    4: "as",
    5: "au",
    6: "sa",
    7: "na",
    8: "ar",
    9: "gr",
}

_WORKER_PROFILE: SpatialProfile | None = None
_WORKER_SLOTS = None
_TILE_WORKERS = 1
GIB = 1024**3


@dataclass(frozen=True)
class GeometryWorkerFailure:
    message: str
    wall_seconds: float


@dataclass(frozen=True)
class GeometryWorkerSuccess:
    coverage: NativeCoverageResult
    wall_seconds: float


def _initialize_worker(profile: SpatialProfile, slots=None, tile_workers: int = 1) -> None:
    global _WORKER_PROFILE, _WORKER_SLOTS, _TILE_WORKERS
    _WORKER_PROFILE = profile
    _WORKER_SLOTS, _TILE_WORKERS = slots, tile_workers


def _polyfill_wkb(wkb: bytes | tuple) -> GeometryWorkerSuccess | GeometryWorkerFailure:
    with _WORKER_SLOTS if _WORKER_SLOTS is not None else contextlib.nullcontext():
        return _polyfill_reserved(wkb)


def _polyfill_reserved(wkb: bytes | tuple) -> GeometryWorkerSuccess | GeometryWorkerFailure:
    from ark_pipeline.runtime.progress import emit, scope

    context = {}
    if isinstance(wkb, tuple):
        wkb, context = wkb
    started = time.perf_counter()
    if _WORKER_PROFILE is None:
        return GeometryWorkerFailure(
            "spatial worker has no semantic profile", time.perf_counter() - started
        )
    try:
        geometry = shapely.from_wkb(wkb)
        from bisect import bisect_right

        from ark_pipeline.runtime.benchmark_estimates import SIZE_BREAKS

        bounds = geometry.bounds
        area = max(0, bounds[2] - bounds[0]) * max(0, bounds[3] - bounds[1])
        context = {"id": f"{os.getpid()}:{time.monotonic_ns()}", "size_bin": bisect_right(SIZE_BREAKS, area),
                   "forced_extreme": False, **context}
        with scope(task=f"geometry:{os.getpid()}", **context):
            emit("geometry_start", phase=f"Polygon {context['id']} · size band {context['size_bin']}")
            budget = TileBudget(_WORKER_SLOTS, _TILE_WORKERS) if _TILE_WORKERS > 1 else None
            coverage = exact_intersecting_cells_native(geometry, _WORKER_PROFILE, tile_budget=budget)
            elapsed = time.perf_counter() - started
            emit("geometry_done", kernel_seconds=elapsed, output_pairs=int(coverage.cells.size),
                 tile_workers=budget.peak_workers if budget else 1,
                 simplification=coverage.decision_simplification_audit)
        return GeometryWorkerSuccess(coverage, elapsed)
    except Exception as error:
        return GeometryWorkerFailure(
            f"{type(error).__name__}: {error}", time.perf_counter() - started
        )


def load_acquisition_manifest(data_root: Path) -> dict[str, Any]:
    path = data_root / "acquisition" / "current.json"
    if not path.is_file():
        raise ValueError(f"active acquisition manifest is missing: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported acquisition manifest schema")
    return manifest


def resolve_input(data_root: Path, stored_path: str) -> Path:
    root = data_root.resolve()
    path = Path(stored_path)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"manifest path escapes data root: {stored_path}")
    return resolved


def spatial_files(data_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    source = manifest.get("sources", {}).get("iucn-spatial")
    if source is None or source.get("validation_status") != "passed":
        raise ValueError("iucn-spatial is not registered with passed validation")
    records = []
    for item in source.get("files", []):
        path = resolve_input(data_root, item["path"])
        records.append(
            {
                **item,
                "resolved_path": path,
                "release": source.get("release"),
                "source_id": "iucn-spatial",
                "format": "polygon",
            }
        )
    if not records:
        raise ValueError("iucn-spatial contains no files")
    return sorted(records, key=lambda item: item["logical_name"])


def _configured_inventory_path(source_id: str) -> Path:
    catalogue_path = REPOSITORY_ROOT / "config/data_sources.toml"
    catalogue = tomllib.loads(catalogue_path.read_text(encoding="utf-8"))
    configured_source = next(
        (item for item in catalogue["sources"] if item["id"] == source_id), None
    )
    if configured_source is None or not configured_source.get("inventory_file"):
        raise ValueError(f"{source_id} has no configured inventory")
    path = (REPOSITORY_ROOT / configured_source["inventory_file"]).resolve()
    if not path.is_relative_to(REPOSITORY_ROOT):
        raise ValueError(f"{source_id} inventory escapes the repository")
    return path


def supplemental_files(data_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve configured IUCN point and HydroBASINS relationship tables."""
    source = manifest.get("sources", {}).get("iucn-spatial-tables")
    if source is None:
        return []
    if source.get("validation_status") != "passed":
        raise ValueError("iucn-spatial-tables is not registered with passed validation")
    inventory_path = _configured_inventory_path("iucn-spatial-tables")
    inventory = tomllib.loads(inventory_path.read_text(encoding="utf-8"))
    formats = {item["logical_name"]: item["format"] for item in inventory["files"]}
    records = []
    for item in source.get("files", []):
        logical_name = item["logical_name"]
        source_format = item.get("format") or formats.get(logical_name)
        if source_format not in SUPPLEMENTAL_REQUIRED_FIELDS:
            raise ValueError(f"unconfigured IUCN spatial-table archive: {logical_name}")
        records.append(
            {
                **item,
                "resolved_path": resolve_input(data_root, item["path"]),
                "release": source.get("release"),
                "source_id": "iucn-spatial-tables",
                "format": source_format,
            }
        )
    if not records:
        raise ValueError("iucn-spatial-tables contains no files")
    return sorted(records, key=lambda item: item["logical_name"])


def hydrobasin_files(data_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    source = manifest.get("sources", {}).get("hydrobasins")
    if source is None:
        return []
    if source.get("validation_status") != "passed":
        raise ValueError("hydrobasins is not registered with passed validation")
    records = [
        {
            **item,
            "resolved_path": resolve_input(data_root, item["path"]),
            "release": source.get("release"),
            "source_id": "hydrobasins",
            "format": "basin-geometry",
        }
        for item in source.get("files", [])
    ]
    if not records:
        raise ValueError("hydrobasins contains no files")
    return sorted(records, key=lambda item: item["logical_name"])


def all_pair_files(data_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [*spatial_files(data_root, manifest), *supplemental_files(data_root, manifest)]


def inspect_archive(record: dict[str, Any], *, deep: bool) -> dict[str, Any]:
    path: Path = record["resolved_path"]
    errors = []
    if not path.is_file():
        errors.append("missing")
    elif path.stat().st_size != record.get("bytes"):
        errors.append("size changed")
    elif deep and sha256(path) != record.get("sha256"):
        errors.append("checksum changed")
    layer_results: list[dict[str, Any]] = []
    if not errors:
        try:
            layers = pyogrio.list_layers(path)
            if not len(layers):
                errors.append("archive contains no spatial layers")
            for layer_name, listed_geometry_type in layers:
                info = pyogrio.read_info(path, layer=str(layer_name))
                missing_fields = sorted(REQUIRED_FIELDS - set(info["fields"]))
                layer_errors = []
                if missing_fields:
                    layer_errors.append("missing fields: " + ", ".join(missing_fields))
                if info.get("crs") != "EPSG:4326":
                    layer_errors.append(f"expected EPSG:4326, found {info.get('crs')}")
                if "Polygon" not in str(info.get("geometry_type")):
                    layer_errors.append(
                        f"expected polygonal geometry, found {info.get('geometry_type')}"
                    )
                errors.extend(f"{layer_name}: {error}" for error in layer_errors)
                layer_results.append(
                    {
                        "name": str(layer_name),
                        "status": "failed" if layer_errors else "ready",
                        "errors": layer_errors,
                        "features": int(info["features"]),
                        "fields": [str(field) for field in info["fields"]],
                        "geometry_type": str(info.get("geometry_type") or listed_geometry_type),
                        "crs": info.get("crs"),
                    }
                )
        except Exception as error:
            errors.append(f"cannot inspect archive: {error}")
    return {
        "logical_name": record["logical_name"],
        "status": "failed" if errors else "ready",
        "errors": errors,
        "features": sum(layer["features"] for layer in layer_results),
        "layers": layer_results,
        "bytes": record.get("bytes"),
        "sha256": record.get("sha256"),
    }


def _zip_csv_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(
            item.filename
            for item in archive.infolist()
            if not item.is_dir() and item.filename.lower().endswith(".csv")
        )


def inspect_supplemental_archive(
    record: dict[str, Any], *, deep: bool
) -> dict[str, Any]:
    path: Path = record["resolved_path"]
    errors: list[str] = []
    members: list[dict[str, Any]] = []
    if not path.is_file():
        errors.append("missing")
    elif path.stat().st_size != record.get("bytes"):
        errors.append("size changed")
    elif deep and sha256(path) != record.get("sha256"):
        errors.append("checksum changed")
    if not errors:
        try:
            required = SUPPLEMENTAL_REQUIRED_FIELDS[record["format"]]
            with zipfile.ZipFile(path) as archive:
                csv_members = [
                    item
                    for item in archive.infolist()
                    if not item.is_dir() and item.filename.lower().endswith(".csv")
                ]
                if not csv_members:
                    errors.append("archive contains no CSV tables")
                for item in csv_members:
                    with archive.open(item) as stream:
                        header = stream.readline().decode("utf-8-sig").strip()
                    fields = {field.strip().lower() for field in header.split(",")}
                    missing = sorted(required - fields)
                    if missing:
                        errors.append(f"{item.filename}: missing fields: {', '.join(missing)}")
                    members.append(
                        {
                            "name": item.filename,
                            "uncompressed_bytes": item.file_size,
                            "status": "failed" if missing else "ready",
                        }
                    )
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
            errors.append(f"cannot inspect archive: {error}")
    return {
        "logical_name": record["logical_name"],
        "format": record["format"],
        "status": "failed" if errors else "ready",
        "errors": errors,
        "members": members,
        "bytes": record.get("bytes"),
        "sha256": record.get("sha256"),
    }


def inspect_hydrobasin_archive(
    record: dict[str, Any], *, deep: bool
) -> dict[str, Any]:
    path: Path = record["resolved_path"]
    errors: list[str] = []
    layers: list[dict[str, Any]] = []
    if not path.is_file():
        errors.append("missing")
    elif path.stat().st_size != record.get("bytes"):
        errors.append("size changed")
    elif deep and sha256(path) != record.get("sha256"):
        errors.append("checksum changed")
    if not errors:
        try:
            available = pyogrio.list_layers(path)
            if len(available) != 12:
                errors.append(f"expected 12 HydroBASINS levels, found {len(available)}")
            for layer_name, listed_geometry_type in available:
                info = pyogrio.read_info(path, layer=str(layer_name))
                layer_errors = []
                if "HYBAS_ID" not in set(info["fields"]):
                    layer_errors.append("missing HYBAS_ID")
                if info.get("crs") != "EPSG:4326":
                    layer_errors.append(f"expected EPSG:4326, found {info.get('crs')}")
                if "Polygon" not in str(info.get("geometry_type")):
                    layer_errors.append(
                        f"expected polygonal geometry, found {info.get('geometry_type')}"
                    )
                errors.extend(f"{layer_name}: {error}" for error in layer_errors)
                layers.append(
                    {
                        "name": str(layer_name),
                        "features": int(info["features"]),
                        "geometry_type": str(
                            info.get("geometry_type") or listed_geometry_type
                        ),
                        "status": "failed" if layer_errors else "ready",
                    }
                )
        except Exception as error:
            errors.append(f"cannot inspect archive: {error}")
    return {
        "logical_name": record["logical_name"],
        "status": "failed" if errors else "ready",
        "errors": errors,
        "layers": layers,
        "bytes": record.get("bytes"),
        "sha256": record.get("sha256"),
    }


def doctor(data_root: Path, profile: SpatialProfile, *, deep: bool) -> dict[str, Any]:
    manifest = load_acquisition_manifest(data_root)
    files = spatial_files(data_root, manifest)
    results = [inspect_archive(record, deep=deep) for record in files]
    supplemental = supplemental_files(data_root, manifest)
    supplemental_results = [
        inspect_supplemental_archive(record, deep=deep) for record in supplemental
    ]
    hydro_records = hydrobasin_files(data_root, manifest) if supplemental else []
    hydro_results = [
        inspect_hydrobasin_archive(record, deep=deep) for record in hydro_records
    ]
    if any(record["format"] == "hydrobasin" for record in supplemental) and not hydro_records:
        supplemental_results.append(
            {
                "logical_name": "hydrobasins",
                "format": "basin-geometry",
                "status": "failed",
                "errors": ["HydroBASINS geometry source is not registered"],
            }
        )
    failures = sum(
        item["status"] == "failed"
        for item in [*results, *supplemental_results, *hydro_results]
    )
    return {
        "schema_version": 1,
        "checked_at": iso_now(),
        "status": "failed" if failures else "ready",
        "semantic_profile": {
            "id": profile.profile_id,
            "sha256": profile.digest,
        },
        "source_release": files[0]["release"],
        "archives": results,
        "supplemental_archives": supplemental_results,
        "hydrobasin_archives": hydro_results,
        "totals": {
            "polygon_archives": len(results),
            "supplemental_archives": len(supplemental_results),
            "hydrobasin_archives": len(hydro_results),
            "features": sum(item.get("features") or 0 for item in results),
            "bytes": sum(
                item.get("bytes") or 0
                for item in [*results, *supplemental_results, *hydro_results]
            ),
            "failures": failures,
        },
    }


def iter_arrow_batches(
    path: Path, batch_rows: int
) -> Iterator[tuple[str, str, str, pa.RecordBatch]]:
    for layer_name, _ in pyogrio.list_layers(path):
        layer = str(layer_name)
        with pyogrio.open_arrow(
            path,
            layer=layer,
            columns=["id_no", "presence", "origin", "seasonal"],
            return_fids=True,
            batch_size=batch_rows,
            use_pyarrow=True,
        ) as (metadata, reader):
            geometry_name = metadata["geometry_name"] or "wkb_geometry"
            fid_name = metadata["fid_column"]
            for batch in reader:
                yield layer, geometry_name, fid_name, batch


def exclusion_reason_values(
    iucn_sis_id: int | None,
    geometry_wkb: bytes | None,
    presence: int | None,
    origin: int | None,
    profile: SpatialProfile,
    *,
    seasonal: int | None = None,
) -> str | None:
    if iucn_sis_id is None:
        return "null_iucn_sis_id"
    if geometry_wkb is None or not geometry_wkb:
        return "null_geometry"
    if presence not in profile.presence:
        return "presence_policy"
    if origin not in profile.origin:
        return "origin_policy"
    if profile.seasonality and seasonal not in profile.seasonality:
        return "seasonality_policy"
    return None


def _schema_signature(schema: pa.Schema) -> list[list[str]]:
    return [[field.name, str(field.type)] for field in schema]


def spatial_code_fingerprint() -> str:
    return code_fingerprint(
        [
            Path(__file__),
            REPOSITORY_ROOT / "ark_pipeline/spatial/coverage.py",
            REPOSITORY_ROOT / "ark_pipeline/spatial/tile_parallel.py",
            REPOSITORY_ROOT / "config/data_sources.toml",
            _configured_inventory_path("iucn-spatial-tables"),
            _configured_inventory_path("hydrobasins"),
            REPOSITORY_ROOT / "ark_pipeline/runtime/provenance.py",
        ]
    )


def stage_identity(
    record: dict[str, Any], profile: SpatialProfile, code_sha256: str
) -> dict[str, Any]:
    return {
        "stage_schema_version": 1,
        "input": {
            "source_id": record.get("source_id", "iucn-spatial"),
            "format": record.get("format", "polygon"),
            "release": record["release"],
            "logical_name": record["logical_name"],
            "bytes": record["bytes"],
            "sha256": record["sha256"],
        },
        "semantic_profile": {
            "id": profile.profile_id,
            "sha256": profile.digest,
        },
        "code_sha256": code_sha256,
        "processing_runtime": {
            "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
            "dependencies": dependency_identity(),
        },
        "pair_schema": _schema_signature(PAIR_SCHEMA),
        "audit_schema": _schema_signature(
            AUDIT_SCHEMA
            if record.get("format", "polygon") == "polygon"
            else SUPPLEMENTAL_AUDIT_SCHEMA
        ),
    }


def stage_identity_with_hydrobasins(
    record: dict[str, Any],
    profile: SpatialProfile,
    code_sha256: str,
    basin_records: list[dict[str, Any]],
) -> dict[str, Any]:
    identity = stage_identity(record, profile, code_sha256)
    if record.get("format") == "hydrobasin":
        identity["hydrobasins"] = [
            {
                "release": item["release"],
                "logical_name": item["logical_name"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
            for item in basin_records
        ]
    return identity


def pair_stage_identities(
    data_root: Path,
    manifest: dict[str, Any],
    profile: SpatialProfile,
    code_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records = all_pair_files(data_root, manifest)
    basin_records = (
        hydrobasin_files(data_root, manifest)
        if any(record["format"] == "hydrobasin" for record in records)
        else []
    )
    code_sha256 = code_sha256 or spatial_code_fingerprint()
    identities = {
        record["logical_name"]: stage_identity_with_hydrobasins(
            record, profile, code_sha256, basin_records
        )
        for record in records
    }
    return records, identities


def _flush_rows(writer: pq.ParquetWriter, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    if rows:
        writer.write_table(pa.Table.from_pylist(rows, schema=schema))
        rows.clear()


def _write_pair_table(
    writer: pq.ParquetWriter,
    cells: np.ndarray,
    species_ids: np.ndarray,
) -> None:
    started = time.monotonic()
    writer.write_table(
        pa.Table.from_arrays(
            [
                pa.array(cells, type=pa.uint64()),
                pa.array(species_ids, type=pa.int64()),
            ],
            schema=PAIR_SCHEMA,
        )
    )
    emit("pair_write", seconds=time.monotonic() - started)
    emit(phase="Writing pair rows", task="pair-writer", completed=len(cells), unit="rows in batch")


def _flush_pair_chunks(
    writer: pq.ParquetWriter,
    cell_chunks: list[np.ndarray],
    species_ids: list[int],
) -> None:
    if not cell_chunks:
        return
    lengths = np.fromiter((chunk.size for chunk in cell_chunks), dtype=np.int64)
    cells = cell_chunks[0] if len(cell_chunks) == 1 else np.concatenate(cell_chunks)
    ids = np.repeat(np.asarray(species_ids, dtype=np.int64), lengths)
    _write_pair_table(writer, cells, ids)
    cell_chunks.clear()
    species_ids.clear()


def _write_large_coverage(
    writer: pq.ParquetWriter,
    cells: np.ndarray,
    iucn_sis_id: int,
    write_rows: int,
) -> None:
    """Write one large result without copying its complete cell array."""
    for start in range(0, cells.size, write_rows):
        chunk = cells[start : start + write_rows]
        ids = np.full(chunk.size, iucn_sis_id, dtype=np.int64)
        _write_pair_table(writer, chunk, ids)


def _process_bounded(
    executor: ProcessPoolExecutor,
    work: list[bytes],
    *,
    max_pending_factor: int = 2,
) -> Iterator[tuple[int, GeometryWorkerSuccess | GeometryWorkerFailure]]:
    """Submit geometry work with a bounded pending queue.

    Keeps all workers saturated without waiting for an entire batch to
    complete before submitting the next row. Memory stays bounded because
    at most workers * max_pending_factor tasks are in flight. Results are
    yielded immediately so multi-million-cell arrays do not accumulate in the
    parent process while a complete Arrow batch finishes.
    """
    if not work:
        return
    max_pending = min(len(work), max(1, executor._max_workers * max_pending_factor))
    task_iter = iter(enumerate(work))
    pending: dict[Any, int] = {}

    def submit_one() -> bool:
        try:
            index, wkb = next(task_iter)
        except StopIteration:
            return False
        pending[executor.submit(_polyfill_wkb, wkb)] = index
        return True

    for _ in range(max_pending):
        submit_one()
    while pending:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            index = pending.pop(future)
            yield index, future.result()
            submit_one()


def default_geometry_workers() -> int:
    return automatic_workers()


def default_duckdb_memory_limit() -> str:
    """Reserve headroom for the OS, filesystem cache, and other applications."""
    memory = psutil.virtual_memory()
    physical_gib = max(1, int(memory.total // GIB))
    available_gib = max(1, int(memory.available // GIB))
    limit_gib = max(1, min(8, physical_gib // 4, available_gib // 2))
    return f"{limit_gib}GB"


def default_duckdb_threads() -> int:
    return configured_count("DUCKDB_THREADS")


def _process_tree_rss(process: psutil.Process) -> int:
    """Best-effort aggregate RSS for the coordinator and current workers."""
    total = process.memory_info().rss
    try:
        total += sum(child.memory_info().rss for child in process.children(recursive=True))
    except (psutil.Error, OSError):
        pass
    return total


def _swap_used_bytes() -> int | None:
    """Return system swap use when the host permits the read."""
    try:
        return int(psutil.swap_memory().used)
    except (OSError, psutil.Error):
        return None


def _observed_swap_peak(previous: int | None) -> int | None:
    current = _swap_used_bytes()
    if current is None:
        return previous
    return current if previous is None else max(previous, current)


def build_archive(
    record: dict[str, Any],
    output_root: Path,
    profile: SpatialProfile,
    code_sha256: str,
    executor: ProcessPoolExecutor | None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    logical_stem = Path(record["logical_name"]).stem
    stage_dir = output_root / "archives" / logical_stem
    stage_dir.mkdir(parents=True, exist_ok=True)
    pairs_path = stage_dir / "res7_pairs.parquet"
    audit_path = stage_dir / "row_audit.parquet"
    receipt_path = stage_dir / "receipt.json"
    outputs = {"pairs": pairs_path, "audit": audit_path}
    schemas = {
        "pairs": _schema_signature(PAIR_SCHEMA),
        "audit": _schema_signature(AUDIT_SCHEMA),
    }
    identity = stage_identity(record, profile, code_sha256)
    if not force and receipt_is_current(receipt_path, identity, outputs, schemas):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        emit("archive_reused", logical_name=record["logical_name"])
        emit("message", message=f"Reused verified {record['logical_name']}")
        return {"logical_name": record["logical_name"], "status": "reused", **receipt["totals"]}

    temporary_pairs = pairs_path.with_suffix(".parquet.tmp")
    temporary_audit = audit_path.with_suffix(".parquet.tmp")
    for temporary in (temporary_pairs, temporary_audit):
        if temporary.exists():
            temporary.unlink()
    started = time.monotonic()
    process = psutil.Process()
    peak_rss = _process_tree_rss(process)
    swap_used_start = _swap_used_bytes()
    peak_swap_used = swap_used_start
    decisions: Counter[str] = Counter()
    pair_cell_chunks: list[np.ndarray] = []
    pair_species_ids: list[int] = []
    buffered_pairs = 0
    audit_rows: list[dict[str, Any]] = []
    output_relationships = 0
    candidate_relationships = 0
    repairs = 0
    source_rows = 0
    pair_writer = pq.ParquetWriter(temporary_pairs, PAIR_SCHEMA, compression="zstd")
    audit_writer = pq.ParquetWriter(temporary_audit, AUDIT_SCHEMA, compression="zstd")
    try:
        for source_layer, geometry_name, fid_name, batch in iter_arrow_batches(
            record["resolved_path"], profile.geometry_batch_rows
        ):
            source_rows += batch.num_rows
            tasks: list[tuple[dict[str, Any], bytes]] = []
            for index in range(batch.num_rows):
                source_row = batch[fid_name][index].as_py()
                iucn_sis_id = batch["id_no"][index].as_py()
                presence = batch["presence"][index].as_py()
                origin = batch["origin"][index].as_py()
                seasonal = batch["seasonal"][index].as_py()
                geometry_wkb = batch[geometry_name][index].as_py()
                reason = exclusion_reason_values(
                    iucn_sis_id,
                    geometry_wkb,
                    presence,
                    origin,
                    profile,
                    seasonal=seasonal,
                )
                base_audit = {
                    "source_logical_name": record["logical_name"],
                    "source_layer": source_layer,
                    "source_row": int(source_row),
                    "iucn_sis_id": None if iucn_sis_id is None else int(iucn_sis_id),
                    "presence": None if presence is None else int(presence),
                    "origin": None if origin is None else int(origin),
                    "seasonal": None if seasonal is None else int(seasonal),
                    "original_valid": None,
                    "repair_method": None,
                    "validity_issue": None,
                    "relative_planar_area_change": None,
                    "candidate_cells": 0,
                    "output_cells": 0,
                    "geometry_wall_seconds": 0.0,
                    "decision_simplification_applied": None,
                    "decision_simplification_bound_metres": None,
                }
                if reason is not None:
                    decisions[reason] += 1
                    audit_rows.append({**base_audit, "decision": reason})
                else:
                    tasks.append((base_audit, geometry_wkb))

            work = [task[1] for task in tasks]
            if executor is None:
                indexed_results = ((index, _polyfill_wkb(wkb)) for index, wkb in enumerate(work))
            else:
                indexed_results = _process_bounded(executor, work)
            for task_index, result in indexed_results:
                emit(phase=f"{record['logical_name']} · {source_rows:,} source rows · {output_relationships:,} pairs", force=True)
                base_audit, _ = tasks[task_index]
                iucn_sis_id = base_audit["iucn_sis_id"]
                source_row = base_audit["source_row"]
                if isinstance(result, GeometryWorkerFailure):
                    decisions["geometry_failure"] += 1
                    audit_rows.append(
                        {
                            **base_audit,
                            "decision": "geometry_failure",
                            "validity_issue": result.message,
                            "geometry_wall_seconds": result.wall_seconds,
                        }
                    )
                    _flush_rows(audit_writer, audit_rows, AUDIT_SCHEMA)
                    raise GeometryCoverageError(
                        f"{record['logical_name']} row {source_row} "
                        f"({iucn_sis_id}) failed: {result.message}"
                    )
                if not isinstance(result, GeometryWorkerSuccess):
                    raise TypeError("worker returned an invalid coverage result")
                else:
                    coverage = result.coverage
                    decisions["included"] += 1
                    repairs += int(not coverage.repair.original_valid)
                    candidate_relationships += coverage.candidate_cells
                    output_relationships += int(coverage.cells.size)
                    audit_rows.append(
                        {
                            **base_audit,
                            "decision": "included",
                            "original_valid": coverage.repair.original_valid,
                            "repair_method": coverage.repair.method,
                            "validity_issue": coverage.repair.original_validity_issue,
                            "relative_planar_area_change": coverage.repair.relative_planar_area_change,
                            "candidate_cells": coverage.candidate_cells,
                            "output_cells": int(coverage.cells.size),
                            "geometry_wall_seconds": result.wall_seconds,
                            "decision_simplification_applied": (
                                coverage.decision_simplification_applied
                            ),
                            "decision_simplification_bound_metres": (
                                coverage.decision_simplification_bound_metres
                            ),
                            "decision_simplification_audit": json.dumps(coverage.decision_simplification_audit),
                        }
                    )
                    if coverage.cells.size >= profile.pair_write_rows:
                        _flush_pair_chunks(pair_writer, pair_cell_chunks, pair_species_ids)
                        buffered_pairs = 0
                        _write_large_coverage(
                            pair_writer,
                            coverage.cells,
                            iucn_sis_id,
                            profile.pair_write_rows,
                        )
                    else:
                        pair_cell_chunks.append(coverage.cells)
                        pair_species_ids.append(iucn_sis_id)
                        buffered_pairs += int(coverage.cells.size)
                        if buffered_pairs >= profile.pair_write_rows:
                            _flush_pair_chunks(pair_writer, pair_cell_chunks, pair_species_ids)
                            buffered_pairs = 0
                peak_rss = max(peak_rss, _process_tree_rss(process))
                peak_swap_used = _observed_swap_peak(peak_swap_used)
            if len(audit_rows) >= profile.geometry_batch_rows:
                _flush_rows(audit_writer, audit_rows, AUDIT_SCHEMA)
            peak_rss = max(peak_rss, _process_tree_rss(process))
            peak_swap_used = _observed_swap_peak(peak_swap_used)
        _flush_pair_chunks(pair_writer, pair_cell_chunks, pair_species_ids)
        _flush_rows(audit_writer, audit_rows, AUDIT_SCHEMA)
    except Exception:
        pair_writer.close()
        audit_writer.close()
        failure = {
            "schema_version": 1,
            "status": "failed",
            "failed_at": iso_now(),
            "identity": identity,
            "totals": {"source_rows": source_rows, "decisions": dict(decisions)},
        }
        atomic_json(stage_dir / "failure.json", failure)
        for temporary in (temporary_pairs, temporary_audit):
            if temporary.exists():
                temporary.unlink()
        raise
    else:
        pair_writer.close()
        audit_writer.close()

    os.replace(temporary_pairs, pairs_path)
    os.replace(temporary_audit, audit_path)
    failure_path = stage_dir / "failure.json"
    if failure_path.exists():
        failure_path.unlink()
    totals = {
        "source_rows": source_rows,
        "decisions": dict(sorted(decisions.items())),
        "candidate_relationships": candidate_relationships,
        "output_relationships": output_relationships,
        "repairs": repairs,
    }
    if sum(decisions.values()) != source_rows:
        raise RuntimeError("row reconciliation failed")
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "completed_at": iso_now(),
        "identity": identity,
        "runtime": runtime_identity(REPOSITORY_ROOT),
        "metrics": {
            "wall_seconds": time.monotonic() - started,
            "peak_rss_bytes": peak_rss,
            "worker_processes": 1 if executor is None else executor._max_workers,
            "system_swap_used_bytes_start": swap_used_start,
            "system_swap_used_bytes_peak_observed": peak_swap_used,
            "system_swap_used_bytes_end": _swap_used_bytes(),
        },
        "totals": totals,
        "outputs": {
            name: {
                "filename": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for name, path in outputs.items()
        },
    }
    atomic_json(receipt_path, receipt)
    return {"logical_name": record["logical_name"], "status": "built", **totals}


def _iter_zipped_csv_batches(
    record: dict[str, Any], columns: list[str], batch_rows: int
) -> Iterator[tuple[str, pa.RecordBatch]]:
    column_types = {
        "id_no": pa.int64(),
        "presence": pa.int64(),
        "origin": pa.int64(),
        "seasonal": pa.int64(),
        "hybas_id": pa.int64(),
        "dec_lat": pa.float64(),
        "dec_long": pa.float64(),
    }
    with zipfile.ZipFile(record["resolved_path"]) as archive:
        members = [
            item
            for item in archive.infolist()
            if not item.is_dir() and item.filename.lower().endswith(".csv")
        ]
        for member in members:
            with archive.open(member) as stream:
                reader = pacsv.open_csv(
                    stream,
                    read_options=pacsv.ReadOptions(
                        block_size=max(1 << 20, batch_rows * 192),
                        use_threads=True,
                    ),
                    convert_options=pacsv.ConvertOptions(
                        include_columns=columns,
                        column_types={name: column_types[name] for name in columns},
                        strings_can_be_null=True,
                    ),
                )
                for batch in reader:
                    emit(
                        "detail",
                        task=f"csv:{record['logical_name']}",
                        phase=f"Stream {record['format']} table · {member.filename}",
                        fraction=min(1.0, stream.tell() / max(1, member.file_size)),
                        unit="uncompressed CSV",
                    )
                    yield member.filename, batch
            emit("task_end", task=f"csv:{record['logical_name']}")


def _true_mask(length: int) -> pa.Array:
    return pa.array(np.ones(length, dtype=np.bool_))


def _valid_set(values: pa.Array, allowed: tuple[int, ...]) -> pa.Array:
    return pc.fill_null(
        pc.is_in(values, value_set=pa.array(allowed, type=pa.int64())), False
    )


def _policy_mask(
    batch: pa.RecordBatch,
    profile: SpatialProfile,
    leading_conditions: list[tuple[str, pa.Array]],
) -> tuple[pa.Array, Counter[str]]:
    remaining = _true_mask(batch.num_rows)
    decisions: Counter[str] = Counter()
    conditions = [
        *leading_conditions,
        ("presence_policy", _valid_set(batch["presence"], profile.presence)),
        ("origin_policy", _valid_set(batch["origin"], profile.origin)),
        ("seasonality_policy", _valid_set(batch["seasonal"], profile.seasonality)),
    ]
    for name, valid in conditions:
        valid = pc.fill_null(valid, False)
        rejected = pc.and_(remaining, pc.invert(valid))
        decisions[name] += int(pc.sum(pc.cast(rejected, pa.int64())).as_py() or 0)
        remaining = pc.and_(remaining, valid)
    decisions["included"] += int(pc.sum(pc.cast(remaining, pa.int64())).as_py() or 0)
    return remaining, decisions


def _audit_summary_rows(
    logical_name: str,
    member: str,
    source_format: str,
    decisions: Counter[str],
    output_relationships: int,
) -> list[dict[str, Any]]:
    return [
        {
            "source_logical_name": logical_name,
            "source_member": member,
            "source_format": source_format,
            "decision": decision,
            "source_rows": count,
            "output_relationships": output_relationships if decision == "included" else 0,
        }
        for decision, count in sorted(decisions.items())
        if count
    ]


def _supplemental_receipt(
    *,
    record: dict[str, Any],
    identity: dict[str, Any],
    started: float,
    totals: dict[str, Any],
    outputs: dict[str, Path],
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "passed",
        "completed_at": iso_now(),
        "identity": identity,
        "runtime": runtime_identity(REPOSITORY_ROOT),
        "metrics": {"wall_seconds": time.monotonic() - started, **(metrics or {})},
        "totals": totals,
        "outputs": {
            name: {
                "filename": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for name, path in outputs.items()
        },
    }


def build_point_archive(
    record: dict[str, Any],
    output_root: Path,
    profile: SpatialProfile,
    identity: dict[str, Any],
    *,
    force: bool,
) -> dict[str, Any]:
    stage_dir = output_root / "archives" / Path(record["logical_name"]).stem
    stage_dir.mkdir(parents=True, exist_ok=True)
    pairs_path = stage_dir / "res7_pairs.parquet"
    audit_path = stage_dir / "row_audit.parquet"
    receipt_path = stage_dir / "receipt.json"
    outputs = {"pairs": pairs_path, "audit": audit_path}
    schemas = {
        "pairs": _schema_signature(PAIR_SCHEMA),
        "audit": _schema_signature(SUPPLEMENTAL_AUDIT_SCHEMA),
    }
    if not force and receipt_is_current(receipt_path, identity, outputs, schemas):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        emit("archive_reused", logical_name=record["logical_name"])
        return {"logical_name": record["logical_name"], "status": "reused", **receipt["totals"]}

    temporary_pairs = pairs_path.with_suffix(".parquet.tmp")
    temporary_audit = audit_path.with_suffix(".parquet.tmp")
    for temporary in (temporary_pairs, temporary_audit):
        temporary.unlink(missing_ok=True)
    started = time.monotonic()
    decisions: Counter[str] = Counter()
    audit_rows: list[dict[str, Any]] = []
    source_rows = output_relationships = 0
    pair_writer = pq.ParquetWriter(temporary_pairs, PAIR_SCHEMA, compression="zstd")
    try:
        for member, batch in _iter_zipped_csv_batches(
            record,
            ["id_no", "presence", "origin", "seasonal", "dec_lat", "dec_long"],
            max(profile.geometry_batch_rows * 64, 8192),
        ):
            source_rows += batch.num_rows
            coordinates_present = pc.and_(
                pc.is_valid(batch["dec_lat"]), pc.is_valid(batch["dec_long"])
            )
            coordinate_range = pc.and_(
                pc.and_(pc.greater_equal(batch["dec_lat"], -90), pc.less_equal(batch["dec_lat"], 90)),
                pc.and_(pc.greater_equal(batch["dec_long"], -180), pc.less_equal(batch["dec_long"], 180)),
            )
            mask, batch_decisions = _policy_mask(
                batch,
                profile,
                [
                    ("null_iucn_sis_id", pc.is_valid(batch["id_no"])),
                    ("null_coordinate", coordinates_present),
                    ("coordinate_out_of_range", coordinate_range),
                ],
            )
            decisions.update(batch_decisions)
            included = batch.filter(mask)
            count = included.num_rows
            if count:
                cells = np.asarray(
                    coordinates_to_cells(
                        included["dec_lat"], included["dec_long"], profile.resolution
                    ),
                    dtype=np.uint64,
                )
                _write_pair_table(
                    pair_writer,
                    cells,
                    included["id_no"].to_numpy(zero_copy_only=False).astype(
                        np.int64, copy=False
                    ),
                )
                output_relationships += count
            audit_rows.extend(
                _audit_summary_rows(
                    record["logical_name"], member, "point", batch_decisions, count
                )
            )
    except Exception:
        pair_writer.close()
        for temporary in (temporary_pairs, temporary_audit):
            temporary.unlink(missing_ok=True)
        raise
    pair_writer.close()
    pq.write_table(
        pa.Table.from_pylist(audit_rows, schema=SUPPLEMENTAL_AUDIT_SCHEMA),
        temporary_audit,
        compression="zstd",
    )
    if sum(decisions.values()) != source_rows:
        raise RuntimeError("point row reconciliation failed")
    os.replace(temporary_pairs, pairs_path)
    os.replace(temporary_audit, audit_path)
    totals = {
        "source_format": "point",
        "source_rows": source_rows,
        "decisions": dict(sorted(decisions.items())),
        "output_relationships": output_relationships,
    }
    receipt = _supplemental_receipt(
        record=record,
        identity=identity,
        started=started,
        totals=totals,
        outputs=outputs,
    )
    atomic_json(receipt_path, receipt)
    return {"logical_name": record["logical_name"], "status": "built", **totals}


def normalize_hydrobasin_archive(
    record: dict[str, Any],
    output_root: Path,
    profile: SpatialProfile,
    identity: dict[str, Any],
    *,
    scratch_dir: Path,
    memory_limit: str,
    threads: int,
    force: bool,
) -> dict[str, Any]:
    stage_dir = output_root / "archives" / Path(record["logical_name"]).stem
    stage_dir.mkdir(parents=True, exist_ok=True)
    relations_path = stage_dir / "basin_species.parquet"
    audit_path = stage_dir / "row_audit.parquet"
    receipt_path = stage_dir / "normalization-receipt.json"
    outputs = {"relations": relations_path, "audit": audit_path}
    schemas = {
        "relations": _schema_signature(BASIN_RELATION_SCHEMA),
        "audit": _schema_signature(SUPPLEMENTAL_AUDIT_SCHEMA),
    }
    if not force and receipt_is_current(receipt_path, identity, outputs, schemas):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return {"status": "reused", "path": relations_path, **receipt["totals"]}

    raw_path = stage_dir / "basin_species.raw.parquet.tmp"
    temporary_relations = relations_path.with_suffix(".parquet.tmp")
    temporary_audit = audit_path.with_suffix(".parquet.tmp")
    for temporary in (raw_path, temporary_relations, temporary_audit):
        temporary.unlink(missing_ok=True)
    started = time.monotonic()
    decisions: Counter[str] = Counter()
    audit_rows: list[dict[str, Any]] = []
    source_rows = included_rows = 0
    writer = pq.ParquetWriter(raw_path, BASIN_RELATION_SCHEMA, compression="zstd")
    try:
        for member, batch in _iter_zipped_csv_batches(
            record,
            ["hybas_id", "id_no", "presence", "origin", "seasonal"],
            max(profile.geometry_batch_rows * 128, 16384),
        ):
            source_rows += batch.num_rows
            valid_hybas = pc.and_(
                pc.is_valid(batch["hybas_id"]), pc.greater(batch["hybas_id"], 0)
            )
            mask, batch_decisions = _policy_mask(
                batch,
                profile,
                [
                    ("null_iucn_sis_id", pc.is_valid(batch["id_no"])),
                    ("null_hybas_id", valid_hybas),
                ],
            )
            decisions.update(batch_decisions)
            included = batch.filter(mask)
            included_rows += included.num_rows
            if included.num_rows:
                writer.write_table(
                    pa.Table.from_arrays(
                        [included["hybas_id"], included["id_no"]],
                        schema=BASIN_RELATION_SCHEMA,
                    )
                )
            audit_rows.extend(
                _audit_summary_rows(
                    record["logical_name"],
                    member,
                    "hydrobasin",
                    batch_decisions,
                    included.num_rows,
                )
            )
    except Exception:
        writer.close()
        for temporary in (raw_path, temporary_relations, temporary_audit):
            temporary.unlink(missing_ok=True)
        raise
    writer.close()
    if sum(decisions.values()) != source_rows:
        raise RuntimeError("HydroBASINS relationship row reconciliation failed")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads={int(threads)}")
        connection.execute(f"SET memory_limit='{memory_limit}'")
        connection.execute(f"SET temp_directory='{_sql_path(scratch_dir)}'")
        connection.execute(
            f"""
            COPY (
                SELECT DISTINCT hybas_id, iucn_sis_id
                FROM read_parquet('{_sql_path(raw_path)}')
            ) TO '{_sql_path(temporary_relations)}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 250000)
            """
        )
    finally:
        connection.close()
        raw_path.unlink(missing_ok=True)
    pq.write_table(
        pa.Table.from_pylist(audit_rows, schema=SUPPLEMENTAL_AUDIT_SCHEMA),
        temporary_audit,
        compression="zstd",
    )
    os.replace(temporary_relations, relations_path)
    os.replace(temporary_audit, audit_path)
    distinct_relationships = pq.read_metadata(relations_path).num_rows
    totals = {
        "source_format": "hydrobasin",
        "source_rows": source_rows,
        "decisions": dict(sorted(decisions.items())),
        "included_rows": included_rows,
        "distinct_basin_species_relationships": distinct_relationships,
        "duplicates_removed": included_rows - distinct_relationships,
    }
    receipt = _supplemental_receipt(
        record=record,
        identity=identity,
        started=started,
        totals=totals,
        outputs=outputs,
        metrics={"memory_limit": memory_limit, "threads": threads},
    )
    atomic_json(receipt_path, receipt)
    return {"status": "built", "path": relations_path, **totals}


def _hybas_region_level(hybas_id: int) -> tuple[int, int]:
    if hybas_id <= 0:
        raise ValueError(f"invalid HYBAS_ID: {hybas_id}")
    region = hybas_id // 1_000_000_000
    level = (hybas_id // 10_000_000) % 100
    if region not in HYDROBASIN_REGION_CODES or not 1 <= level <= 12:
        raise ValueError(f"unsupported HYBAS_ID encoding: {hybas_id}")
    return region, level


def _hybas_id_digest(ids: list[int]) -> str:
    digest = hashlib.sha256()
    for value in ids:
        digest.update(int(value).to_bytes(8, "big", signed=True))
    return digest.hexdigest()


def _flush_basin_cells(
    writer: pq.ParquetWriter,
    cell_chunks: list[np.ndarray],
    basin_ids: list[int],
) -> int:
    if not cell_chunks:
        return 0
    lengths = np.fromiter((chunk.size for chunk in cell_chunks), dtype=np.int64)
    cells = cell_chunks[0] if len(cell_chunks) == 1 else np.concatenate(cell_chunks)
    ids = np.repeat(np.asarray(basin_ids, dtype=np.int64), lengths)
    writer.write_table(
        pa.Table.from_arrays(
            [pa.array(ids, type=pa.int64()), pa.array(cells, type=pa.uint64())],
            schema=BASIN_CELL_SCHEMA,
        )
    )
    count = int(cells.size)
    cell_chunks.clear()
    basin_ids.clear()
    return count


def build_hydrobasin_index(
    relation_paths: list[Path],
    basin_records: list[dict[str, Any]],
    output_root: Path,
    profile: SpatialProfile,
    code_sha256: str,
    executor: ProcessPoolExecutor | None,
    *,
    scratch_dir: Path,
    memory_limit: str,
    threads: int,
    force: bool,
) -> dict[str, Any]:
    if not relation_paths:
        raise ValueError("cannot build a HydroBASINS index without relationship tables")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads={int(threads)}")
        connection.execute(f"SET memory_limit='{memory_limit}'")
        connection.execute(f"SET temp_directory='{_sql_path(scratch_dir)}'")
        paths_sql = ", ".join(f"'{_sql_path(path)}'" for path in relation_paths)
        requested_ids = [
            int(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT hybas_id FROM read_parquet([{paths_sql}]) ORDER BY hybas_id"
            ).fetchall()
        ]
    finally:
        connection.close()
    if not requested_ids:
        raise ValueError("eligible HydroBASINS relationship tables contain no basin IDs")

    index_dir = output_root / "hydrobasins"
    index_dir.mkdir(parents=True, exist_ok=True)
    cells_path = index_dir / "basin_cells.parquet"
    audit_path = index_dir / "basin_audit.parquet"
    receipt_path = index_dir / "receipt.json"
    outputs = {"cells": cells_path, "audit": audit_path}
    schemas = {
        "cells": _schema_signature(BASIN_CELL_SCHEMA),
        "audit": _schema_signature(BASIN_AUDIT_SCHEMA),
    }
    identity = {
        "stage_schema_version": 1,
        "semantic_profile": {"id": profile.profile_id, "sha256": profile.digest},
        "requested_hybas_ids": {
            "count": len(requested_ids),
            "sha256": _hybas_id_digest(requested_ids),
        },
        "hydrobasins": [
            {
                "release": record["release"],
                "logical_name": record["logical_name"],
                "bytes": record["bytes"],
                "sha256": record["sha256"],
            }
            for record in basin_records
        ],
        "code_sha256": code_sha256,
        "processing_runtime": {
            "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
            "dependencies": dependency_identity(),
        },
        "schemas": schemas,
    }
    if not force and receipt_is_current(receipt_path, identity, outputs, schemas):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return {"status": "reused", "path": cells_path, **receipt["totals"]}

    by_region_level: dict[tuple[int, int], set[int]] = {}
    for hybas_id in requested_ids:
        by_region_level.setdefault(_hybas_region_level(hybas_id), set()).add(hybas_id)
    by_code = {
        code: next(
            (
                record
                for record in basin_records
                if f"hybas_{code}_" in record["logical_name"].lower()
            ),
            None,
        )
        for code in HYDROBASIN_REGION_CODES.values()
    }
    required_codes = {
        HYDROBASIN_REGION_CODES[region] for region, _ in by_region_level
    }
    missing_sources = [
        code for code in sorted(required_codes) if by_code.get(code) is None
    ]
    if missing_sources:
        raise ValueError("missing HydroBASINS regions: " + ", ".join(missing_sources))

    temporary_cells = cells_path.with_suffix(".parquet.tmp")
    temporary_audit = audit_path.with_suffix(".parquet.tmp")
    temporary_cells.unlink(missing_ok=True)
    temporary_audit.unlink(missing_ok=True)
    started = time.monotonic()
    cells_writer = pq.ParquetWriter(temporary_cells, BASIN_CELL_SCHEMA, compression="zstd")
    audit_writer = pq.ParquetWriter(temporary_audit, BASIN_AUDIT_SCHEMA, compression="zstd")
    cell_chunks: list[np.ndarray] = []
    basin_ids: list[int] = []
    audit_rows: list[dict[str, Any]] = []
    buffered_cells = output_cells = completed = 0
    unresolved = set(requested_ids)
    try:
        for (region, level), wanted in sorted(by_region_level.items()):
            code = HYDROBASIN_REGION_CODES[region]
            record = by_code[code]
            assert record is not None
            layer = f"hybas_{code}_lev{level:02d}_v1c"
            metadata, identifiers = pyogrio.read_arrow(
                record["resolved_path"],
                layer=layer,
                columns=["HYBAS_ID"],
                read_geometry=False,
                return_fids=True,
            )
            fid_name = metadata["fid_column"]
            source_ids = identifiers["HYBAS_ID"].to_numpy(zero_copy_only=False)
            selected = pc.is_in(
                identifiers["HYBAS_ID"],
                value_set=pa.array(
                    sorted(wanted), type=identifiers["HYBAS_ID"].type
                ),
            )
            selected_ids = source_ids[np.asarray(selected, dtype=np.bool_)]
            if len(selected_ids) != len(set(map(int, selected_ids))):
                raise ValueError(f"{layer}: duplicate HYBAS_ID values in geometry source")
            fids = identifiers[fid_name].filter(selected).to_numpy(
                zero_copy_only=False
            )
            for start in range(0, len(fids), profile.geometry_batch_rows):
                _, table = pyogrio.read_arrow(
                    record["resolved_path"],
                    layer=layer,
                    columns=["HYBAS_ID"],
                    fids=fids[start : start + profile.geometry_batch_rows],
                    return_fids=False,
                )
                geometry_name = "wkb_geometry"
                ids = table["HYBAS_ID"].to_numpy(zero_copy_only=False).astype(
                    np.int64, copy=False
                )
                work = [
                    (
                        wkb,
                        {
                            "id": f"hybas:{int(hybas_id)}",
                            "source_kind": "hydrobasin",
                        },
                    )
                    for hybas_id, wkb in zip(ids, table[geometry_name].to_pylist())
                ]
                results = (
                    _process_bounded(executor, work)
                    if executor is not None
                    else ((index, _polyfill_wkb(wkb)) for index, wkb in enumerate(work))
                )
                for index, result in results:
                    hybas_id = int(ids[index])
                    if isinstance(result, GeometryWorkerFailure):
                        raise GeometryCoverageError(
                            f"HYBAS_ID {hybas_id} failed: {result.message}"
                        )
                    if not isinstance(result, GeometryWorkerSuccess):
                        raise TypeError("worker returned an invalid basin coverage result")
                    coverage = result.coverage
                    unresolved.discard(hybas_id)
                    completed += 1
                    output_cells += int(coverage.cells.size)
                    cell_chunks.append(coverage.cells)
                    basin_ids.append(hybas_id)
                    buffered_cells += int(coverage.cells.size)
                    audit_rows.append(
                        {
                            "hybas_id": hybas_id,
                            "region": code,
                            "level": level,
                            "original_valid": coverage.repair.original_valid,
                            "repair_method": coverage.repair.method,
                            "validity_issue": coverage.repair.original_validity_issue,
                            "relative_planar_area_change": coverage.repair.relative_planar_area_change,
                            "candidate_cells": coverage.candidate_cells,
                            "output_cells": int(coverage.cells.size),
                            "geometry_wall_seconds": result.wall_seconds,
                            "decision_simplification_applied": coverage.decision_simplification_applied,
                            "decision_simplification_bound_metres": coverage.decision_simplification_bound_metres,
                            "decision_simplification_audit": json.dumps(
                                coverage.decision_simplification_audit
                            ),
                        }
                    )
                    if buffered_cells >= profile.pair_write_rows:
                        _flush_basin_cells(cells_writer, cell_chunks, basin_ids)
                        buffered_cells = 0
                    if len(audit_rows) >= profile.geometry_batch_rows:
                        _flush_rows(audit_writer, audit_rows, BASIN_AUDIT_SCHEMA)
                    emit(
                        "work",
                        overall=True,
                        phase="HydroBASINS geometry → H3",
                        completed=completed,
                        total=len(requested_ids),
                        unit="referenced basins",
                    )
        _flush_basin_cells(cells_writer, cell_chunks, basin_ids)
        _flush_rows(audit_writer, audit_rows, BASIN_AUDIT_SCHEMA)
    except Exception:
        cells_writer.close()
        audit_writer.close()
        temporary_cells.unlink(missing_ok=True)
        temporary_audit.unlink(missing_ok=True)
        raise
    cells_writer.close()
    audit_writer.close()
    if unresolved:
        temporary_cells.unlink(missing_ok=True)
        temporary_audit.unlink(missing_ok=True)
        sample = ", ".join(map(str, sorted(unresolved)[:10]))
        raise ValueError(
            f"{len(unresolved)} referenced HYBAS_ID values are absent from HydroBASINS v1c; sample: {sample}"
        )
    os.replace(temporary_cells, cells_path)
    os.replace(temporary_audit, audit_path)
    totals = {
        "referenced_basins": len(requested_ids),
        "output_basin_cells": output_cells,
        "levels": sorted({level for _, level in by_region_level}),
        "regions": sorted(
            {HYDROBASIN_REGION_CODES[region] for region, _ in by_region_level}
        ),
    }
    receipt = _supplemental_receipt(
        record=basin_records[0],
        identity=identity,
        started=started,
        totals=totals,
        outputs=outputs,
        metrics={
            "worker_processes": 1 if executor is None else executor._max_workers,
            "memory_limit": memory_limit,
            "threads": threads,
        },
    )
    atomic_json(receipt_path, receipt)
    return {"status": "built", "path": cells_path, **totals}


def build_hydrobasin_pairs(
    record: dict[str, Any],
    output_root: Path,
    identity: dict[str, Any],
    normalization: dict[str, Any],
    basin_cells: Path,
    *,
    scratch_dir: Path,
    memory_limit: str,
    threads: int,
    force: bool,
) -> dict[str, Any]:
    stage_dir = output_root / "archives" / Path(record["logical_name"]).stem
    pairs_path = stage_dir / "res7_pairs.parquet"
    receipt_path = stage_dir / "receipt.json"
    outputs = {"pairs": pairs_path}
    schemas = {"pairs": _schema_signature(PAIR_SCHEMA)}
    if not force and receipt_is_current(receipt_path, identity, outputs, schemas):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        emit("archive_reused", logical_name=record["logical_name"])
        return {"logical_name": record["logical_name"], "status": "reused", **receipt["totals"]}

    temporary_pairs = pairs_path.with_suffix(".parquet.tmp")
    temporary_pairs.unlink(missing_ok=True)
    started = time.monotonic()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads={int(threads)}")
        connection.execute(f"SET memory_limit='{memory_limit}'")
        connection.execute(f"SET temp_directory='{_sql_path(scratch_dir)}'")
        connection.execute(
            f"""
            COPY (
                SELECT cells.h3_index, relations.iucn_sis_id
                FROM read_parquet('{_sql_path(normalization['path'])}') relations
                INNER JOIN read_parquet('{_sql_path(basin_cells)}') cells USING (hybas_id)
            ) TO '{_sql_path(temporary_pairs)}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 250000)
            """
        )
    except Exception:
        temporary_pairs.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
    os.replace(temporary_pairs, pairs_path)
    output_relationships = pq.read_metadata(pairs_path).num_rows
    totals = {
        key: value
        for key, value in normalization.items()
        if key not in {"status", "path"}
    }
    totals.update(
        source_format="hydrobasin", output_relationships=output_relationships
    )
    receipt = _supplemental_receipt(
        record=record,
        identity=identity,
        started=started,
        totals=totals,
        outputs=outputs,
        metrics={"memory_limit": memory_limit, "threads": threads},
    )
    atomic_json(receipt_path, receipt)
    return {"logical_name": record["logical_name"], "status": "built", **totals}


def build(
    data_root: Path,
    output_root: Path,
    profile: SpatialProfile,
    selected: set[str],
    *,
    force: bool,
    workers: int = 1,
    scratch_dir: Path | None = None,
    memory_limit: str | None = None,
    threads: int | None = None,
) -> dict[str, Any]:
    manifest = load_acquisition_manifest(data_root)
    records = all_pair_files(data_root, manifest)
    if any(record["format"] == "point" for record in records):
        point = profile.raw.get("point_coverage", {})
        if point.get("latitude_column") != "dec_lat" or point.get("longitude_column") != "dec_long":
            raise ValueError("the spatial profile does not define the supported IUCN point semantics")
    if any(record["format"] == "hydrobasin" for record in records):
        basin = profile.raw.get("hydrobasin_coverage", {})
        if basin.get("release") != "v1c" or basin.get("relationship_key") != "hybas_id":
            raise ValueError("the spatial profile does not define the supported HydroBASINS v1c semantics")
    available = {record["logical_name"] for record in records}
    unknown = selected - available
    if unknown:
        raise ValueError("unknown spatial archive(s): " + ", ".join(sorted(unknown)))
    if selected:
        records = [record for record in records if record["logical_name"] in selected]
    code_sha256 = spatial_code_fingerprint()
    scratch_dir = scratch_dir or data_root / "scratch/spatial"
    memory_limit = memory_limit or default_duckdb_memory_limit()
    threads = threads or default_duckdb_threads()
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if threads < 1:
        raise ValueError("threads must be at least 1")
    relation_records = [record for record in records if record["format"] == "hydrobasin"]
    basin_records = hydrobasin_files(data_root, manifest) if relation_records else []
    identities = {
        record["logical_name"]: stage_identity_with_hydrobasins(
            record, profile, code_sha256, basin_records
        )
        for record in records
    }

    def execute(executor: ProcessPoolExecutor | None) -> tuple[list[dict[str, Any]], dict | None]:
        results: list[dict[str, Any]] = []
        polygon_records = [record for record in records if record["format"] == "polygon"]
        for record in polygon_records:
            results.append(
                build_archive(
                    record,
                    output_root,
                    profile,
                    code_sha256,
                    executor,
                    force=force,
                )
            )
        supplemental_records = [record for record in records if record["format"] != "polygon"]
        completed_supplemental = 0
        for record in [item for item in records if item["format"] == "point"]:
            results.append(
                build_point_archive(
                    record,
                    output_root,
                    profile,
                    identities[record["logical_name"]],
                    force=force,
                )
            )
            completed_supplemental += 1
            emit(
                "work",
                overall=True,
                phase="Point and HydroBASINS source archives",
                completed=completed_supplemental,
                total=len(supplemental_records),
                unit="source archives",
            )

        normalized: dict[str, dict[str, Any]] = {}
        for record in relation_records:
            normalized[record["logical_name"]] = normalize_hydrobasin_archive(
                record,
                output_root,
                profile,
                identities[record["logical_name"]],
                scratch_dir=scratch_dir,
                memory_limit=memory_limit,
                threads=threads,
                force=force,
            )
        basin_report = None
        if relation_records:
            basin_report = build_hydrobasin_index(
                [normalized[record["logical_name"]]["path"] for record in relation_records],
                basin_records,
                output_root,
                profile,
                code_sha256,
                executor,
                scratch_dir=scratch_dir,
                memory_limit=memory_limit,
                threads=threads,
                force=force,
            )
            for record in relation_records:
                results.append(
                    build_hydrobasin_pairs(
                        record,
                        output_root,
                        identities[record["logical_name"]],
                        normalized[record["logical_name"]],
                        basin_report["path"],
                        scratch_dir=scratch_dir,
                        memory_limit=memory_limit,
                        threads=threads,
                        force=force,
                    )
                )
                completed_supplemental += 1
                emit(
                    "work",
                    overall=True,
                    phase="Point and HydroBASINS source archives",
                    completed=completed_supplemental,
                    total=len(supplemental_records),
                    unit="source archives",
                )
        return results, basin_report

    if workers == 1:
        _initialize_worker(profile)
        results, basin_report = execute(None)
    else:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            initializer=_initialize_worker,
            initargs=(profile, context.BoundedSemaphore(workers), workers),
        ) as executor:
            results, basin_report = execute(executor)
    report = {
        "schema_version": 1,
        "status": "passed",
        "completed_at": iso_now(),
        "semantic_profile": {"id": profile.profile_id, "sha256": profile.digest},
        "runtime_parameters": {
            "worker_processes": workers,
            "duckdb_threads": threads,
            "duckdb_memory_limit": memory_limit,
        },
        "archives": results,
        "hydrobasin_index": (
            {
                key: str(value) if isinstance(value, Path) else value
                for key, value in basin_report.items()
            }
            if basin_report
            else None
        ),
        "totals": {
            "archives": len(results),
            "source_rows": sum(item["source_rows"] for item in results),
            "output_relationships": sum(item["output_relationships"] for item in results),
            "formats": dict(Counter(item.get("source_format", "polygon") for item in results)),
        },
    }
    atomic_json(output_root / "build-report.json", report)
    return report


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _validated_archive_pairs(
    output_root: Path,
    profile: SpatialProfile,
    expected_identities: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for receipt_path in sorted((output_root / "archives").glob("*/receipt.json")):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("status") != "passed":
            raise ValueError(f"archive receipt is not passed: {receipt_path}")
        if receipt.get("identity", {}).get("semantic_profile") != {
            "id": profile.profile_id,
            "sha256": profile.digest,
        }:
            raise ValueError(f"archive semantic profile is stale: {receipt_path}")
        if expected_identities is not None:
            logical_name = receipt.get("identity", {}).get("input", {}).get("logical_name")
            if receipt.get("identity") != expected_identities.get(logical_name):
                raise ValueError(
                    f"archive source or code is stale: {receipt_path}; run just spatial-build"
                )
        record = receipt.get("outputs", {}).get("pairs")
        if record is None:
            raise ValueError(f"archive receipt has no pair output: {receipt_path}")
        pair_path = receipt_path.parent / record["filename"]
        if not pair_path.is_file() or pair_path.stat().st_size != record["bytes"]:
            raise ValueError(f"archive pair output size is stale: {pair_path}")
        if sha256(pair_path) != record["sha256"]:
            raise ValueError(f"archive pair output checksum is stale: {pair_path}")
        if _schema_signature(pq.read_schema(pair_path)) != _schema_signature(PAIR_SCHEMA):
            raise ValueError(f"archive pair output schema is incompatible: {pair_path}")
        inputs.append(
            {
                "logical_name": receipt["identity"]["input"]["logical_name"],
                "path": pair_path,
                "bytes": record["bytes"],
                "sha256": record["sha256"],
                "rows": pq.read_metadata(pair_path).num_rows,
            }
        )
    if not inputs:
        raise ValueError(f"no passed archive pair outputs found under {output_root}")
    return inputs


def finalize_relations(
    output_root: Path,
    profile: SpatialProfile,
    *,
    scratch_dir: Path,
    memory_limit: str,
    threads: int,
    force: bool,
    expected_archives: set[str] | None = None,
    expected_identities: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Globally deduplicate res7 pairs and derive distinct res3 parents."""
    if threads < 1:
        raise ValueError("DuckDB threads must be at least 1")
    inputs = _validated_archive_pairs(output_root, profile, expected_identities)
    actual_archives = {item["logical_name"] for item in inputs}
    if expected_archives is not None and actual_archives != expected_archives:
        missing = sorted(expected_archives - actual_archives)
        unexpected = sorted(actual_archives - expected_archives)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise ValueError("archive output set is incomplete (" + "; ".join(details) + ")")
    code_sha256 = spatial_code_fingerprint()
    identity = {
        "stage_schema_version": 1,
        "inputs": [
            {key: item[key] for key in ("logical_name", "bytes", "sha256", "rows")}
            for item in inputs
        ],
        "semantic_profile": {"id": profile.profile_id, "sha256": profile.digest},
        "code_sha256": code_sha256,
        "processing_runtime": {
            "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
            "dependencies": dependency_identity(),
        },
        "parameters": {
            "parent_resolution": 3,
            "deduplication_key": ["h3_index", "iucn_sis_id"],
        },
        "schemas": {
            "res7": _schema_signature(PAIR_SCHEMA),
            "res3": _schema_signature(PAIR_SCHEMA),
        },
    }
    relations_dir = output_root / "relations"
    relations_dir.mkdir(parents=True, exist_ok=True)
    res7_path = relations_dir / "res7_pairs.parquet"
    res3_path = relations_dir / "res3_pairs.parquet"
    receipt_path = relations_dir / "receipt.json"
    outputs = {"res7": res7_path, "res3": res3_path}
    schemas = {name: _schema_signature(PAIR_SCHEMA) for name in outputs}
    if not force and receipt_is_current(receipt_path, identity, outputs, schemas):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return {"status": "reused", **receipt["totals"]}

    scratch_dir.mkdir(parents=True, exist_ok=True)
    temporary_res7 = res7_path.with_suffix(".parquet.tmp")
    temporary_res3 = res3_path.with_suffix(".parquet.tmp")
    for temporary in (temporary_res7, temporary_res3):
        if temporary.exists():
            temporary.unlink()
    paths_sql = ", ".join(f"'{_sql_path(item['path'])}'" for item in inputs)
    started = time.monotonic()
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads={int(threads)}")
        connection.execute(f"SET memory_limit='{memory_limit}'")
        connection.execute(f"SET temp_directory='{_sql_path(scratch_dir)}'")
        connection.execute(
            f"""
            COPY (
                SELECT DISTINCT h3_index, iucn_sis_id
                FROM read_parquet([{paths_sql}])
            ) TO '{_sql_path(temporary_res7)}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 250000)
            """
        )
        parent_expression = (
            "((h3_index & ~((15::UBIGINT) << 52)) "
            "| ((3::UBIGINT) << 52) | (((1::UBIGINT) << 36) - 1))::UBIGINT"
        )
        connection.execute(
            f"""
            COPY (
                SELECT DISTINCT {parent_expression} AS h3_index, iucn_sis_id
                FROM read_parquet('{_sql_path(temporary_res7)}')
            ) TO '{_sql_path(temporary_res3)}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 250000)
            """
        )
    except Exception:
        for temporary in (temporary_res7, temporary_res3):
            if temporary.exists():
                temporary.unlink()
        raise
    finally:
        connection.close()

    os.replace(temporary_res7, res7_path)
    os.replace(temporary_res3, res3_path)
    raw_rows = sum(int(item["rows"]) for item in inputs)
    res7_rows = pq.read_metadata(res7_path).num_rows
    res3_rows = pq.read_metadata(res3_path).num_rows
    if not 0 < res7_rows <= raw_rows or not 0 < res3_rows <= res7_rows:
        raise RuntimeError("relationship reconciliation failed")
    totals = {
        "archive_pair_rows": raw_rows,
        "res7_relationships": res7_rows,
        "exact_duplicates_removed": raw_rows - res7_rows,
        "res3_relationships": res3_rows,
    }
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "completed_at": iso_now(),
        "identity": identity,
        "runtime": runtime_identity(REPOSITORY_ROOT),
        "metrics": {
            "wall_seconds": time.monotonic() - started,
            "memory_limit": memory_limit,
            "threads": threads,
        },
        "totals": totals,
        "outputs": {
            name: {
                "filename": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for name, path in outputs.items()
        },
    }
    atomic_json(receipt_path, receipt)
    atomic_json(output_root / "finalize-report.json", receipt)
    return {"status": "built", **totals}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)

    def common(target: argparse.ArgumentParser, *, inherited: bool = False) -> None:
        # Accept these flags on either side of the subcommand. Suppressed child
        # defaults must not overwrite options already parsed by the parent.
        target.add_argument(
            "--root",
            type=Path,
            default=argparse.SUPPRESS
            if inherited
            else Path(os.environ.get("GLOBAL_DATA_ROOT", "data/external")),
        )
        target.add_argument(
            "--profile",
            type=Path,
            default=argparse.SUPPRESS
            if inherited
            else Path(os.environ.get("SPATIAL_PROFILE", str(DEFAULT_PROFILE))),
        )
        target.add_argument(
            "--output-root", type=Path, default=argparse.SUPPRESS if inherited else None
        )

    def compute(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--scratch-dir",
            type=Path,
            default=Path(os.environ["DUCKDB_SCRATCH_DIR"])
            if os.environ.get("DUCKDB_SCRATCH_DIR")
            else None,
        )
        target.add_argument(
            "--memory-limit",
            default=os.environ.get("DUCKDB_MEMORY_LIMIT") or default_duckdb_memory_limit(),
        )
        target.add_argument(
            "--threads",
            type=int,
            default=os.environ.get("DUCKDB_THREADS") or default_duckdb_threads(),
        )

    common(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("status", "read-only readiness summary; no downloads or full checksum scans"),
        ("doctor", "inspect all spatial inputs before a build"),
        ("build", "build or resume resolution-7 archive stages"),
        ("finalize", "deduplicate res7 and derive res3 pairs"),
        ("export", "finalize and export H3 lists for the serving builders"),
        ("run", "validate, build, finalize and export in one resumable run"),
    ):
        child = commands.add_parser(name, help=description)
        common(child, inherited=True)
        if name == "doctor":
            child.add_argument("--deep", action="store_true")
            child.add_argument("--output", type=Path)
        if name in {"build", "run"}:
            child.add_argument(
                "--workers",
                type=int,
                default=configured_count("SPATIAL_WORKERS"),
            )
        if name == "build":
            child.add_argument("--archive", action="append", default=[])
        if name in {"build", "finalize", "run"}:
            child.add_argument("--force", action="store_true")
        if name in {"build", "finalize", "export", "run"}:
            compute(child)
        if name == "finalize":
            child.add_argument("--allow-partial", action="store_true")
    return parser


def pipeline_status(root: Path, output_root: Path, profile: SpatialProfile) -> dict[str, Any]:
    """Cheap receipt/size checks, explicitly not full integrity verification."""
    from ark_pipeline.cli.sources_acquire import load_catalogue, load_manifest
    from ark_pipeline.cli.sources_sync import configured_release, registered_errors

    catalogue = load_catalogue(REPOSITORY_ROOT / "config/data_sources.toml")
    manifest = load_manifest(root)
    sources = []
    for source_id in catalogue.profiles["authorized"]["required_sources"]:
        source = catalogue.sources[source_id]
        record = manifest.get("sources", {}).get(source_id)
        errors = registered_errors(root, source, record)
        if (
            record
            and configured_release(source)
            and record.get("release") != configured_release(source)
        ):
            errors.append("registered release differs from configured release")
        sources.append(
            {
                "source": source_id,
                "status": "needs-attention" if errors else "present",
                "errors": errors,
            }
        )

    def receipt_status(path: Path, expected_identity: dict | None = None) -> str:
        if not path.is_file():
            return "missing"
        try:
            value = json.loads(path.read_text())
            if value.get("status") != "passed" or (
                expected_identity is not None and value.get("identity") != expected_identity
            ):
                return "stale"
            for item in value["outputs"].values():
                output = path.parent / item["filename"]
                if not output.is_file() or output.stat().st_size != item["bytes"]:
                    return "stale"
            return "present-unverified"
        except (KeyError, OSError, ValueError):
            return "stale"

    archives = []
    try:
        records, expected_identities = pair_stage_identities(
            root, manifest, profile
        )
    except ValueError:
        records = []
        expected_identities = {}
    for record in records:
        path = output_root / "archives" / Path(record["logical_name"]).stem / "receipt.json"
        archives.append(
            {
                "archive": record["logical_name"],
                "format": record.get("format", "polygon"),
                "status": receipt_status(path, expected_identities[record["logical_name"]]),
            }
        )
    relations = receipt_status(output_root / "relations/receipt.json")
    serving = receipt_status(output_root / "serving/current/receipt.json")
    archive_current = bool(archives) and all(
        item["status"] == "present-unverified" for item in archives
    )
    if not archive_current:
        relations = "blocked-by-archives"
    if relations == "present-unverified":
        finalized = json.loads((output_root / "relations/receipt.json").read_text())
        final_identity = finalized.get("identity", {})
        current_pairs = {}
        for record in records:
            stage = json.loads(
                (
                    output_root / "archives" / Path(record["logical_name"]).stem / "receipt.json"
                ).read_text()
            )
            current_pairs[record["logical_name"]] = stage["outputs"]["pairs"]["sha256"]
        finalized_pairs = {
            item["logical_name"]: item["sha256"] for item in final_identity.get("inputs", [])
        }
        if (
            final_identity.get("code_sha256") != spatial_code_fingerprint()
            or final_identity.get("semantic_profile")
            != {"id": profile.profile_id, "sha256": profile.digest}
            or final_identity.get("processing_runtime", {}).get("dependencies")
            != dependency_identity()
            or current_pairs != finalized_pairs
        ):
            relations = "stale"
        elif serving == "present-unverified":
            served = json.loads((output_root / "serving/current/receipt.json").read_text())
            if served.get("identity", {}).get("relations") != final_identity or served.get(
                "identity", {}
            ).get("inputs") != finalized.get("outputs"):
                serving = "stale"
    if relations != "present-unverified":
        serving = "blocked-by-relations"
    ready = all(item["status"] == "present" for item in sources) and serving == "present-unverified"
    return {
        "status": "present-unverified" if ready else "needs-work",
        "verification": "Metadata, receipt identities and file sizes only. Build commands verify checksums before reuse.",
        "data_root": str(root),
        "output_root": str(output_root),
        "sources": sources,
        "archives": archives,
        "relations": relations,
        "serving_lists": serving,
        "next_command": "just global-prepare (with GLOBAL_H3_ROOT configured)"
        if ready
        else "just data-build",
    }


@tracked_stage("pairs")
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profile = load_spatial_profile(args.profile)
        if profile.production_kernel != "direct-any-touch-v2":
            raise ValueError(
                "use an active any-touch profile; historical kernels are in archive.spatial"
            )
        args.root = args.root.expanduser().resolve()
        output_root = (
            (args.output_root or args.root / "derived" / profile.profile_id).expanduser().resolve()
        )
        if args.command == "status":
            print(json.dumps(pipeline_status(args.root, output_root, profile), indent=2))
            return 0
        for key in ("workers", "threads"):
            if hasattr(args, key) and getattr(args, key) < 1:
                raise ValueError(f"{key} must be at least 1")
        preflight = doctor(args.root, profile, deep=getattr(args, "deep", False))
        if args.command == "doctor":
            if args.output:
                atomic_json(args.output, preflight)
            print(json.dumps(preflight, indent=2))
            return 0 if preflight["status"] == "ready" else 1
        if preflight["status"] != "ready":
            print(json.dumps(preflight, indent=2))
            return 1
        stages = {}
        if args.command in {"build", "run"}:
            stages["build"] = build(
                args.root,
                output_root,
                profile,
                set(getattr(args, "archive", [])),
                force=args.force,
                workers=args.workers,
                scratch_dir=args.scratch_dir or args.root / "scratch/spatial",
                memory_limit=args.memory_limit,
                threads=args.threads,
            )
        if args.command in {"finalize", "export", "run"}:
            records, identities = pair_stage_identities(
                args.root, load_acquisition_manifest(args.root), profile
            )
            scratch = args.scratch_dir or args.root / "scratch/spatial"
            stages["finalize"] = finalize_relations(
                output_root,
                profile,
                scratch_dir=scratch,
                memory_limit=args.memory_limit,
                threads=args.threads,
                force=getattr(args, "force", False),
                expected_archives=None
                if getattr(args, "allow_partial", False)
                else set(identities),
                expected_identities=identities,
            )
        if args.command in {"export", "run"}:
            from ark_pipeline.aggregation.species_lists import export_serving_lists

            stages["export"] = export_serving_lists(
                output_root,
                scratch_dir=scratch,
                memory_limit=args.memory_limit,
                threads=args.threads,
            )
        report = {"status": "passed", "stages": stages, "output_root": str(output_root)}
        if args.command == "run":
            atomic_json(output_root / "pipeline-report.json", report)
        print(json.dumps(report, indent=2))
        return 0
    except (OSError, ValueError, duckdb.Error) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
