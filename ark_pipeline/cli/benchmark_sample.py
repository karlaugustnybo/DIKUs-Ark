#!/usr/bin/env python3
"""Build a deterministic, size-stratified IUCN polygon benchmark fixture."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import math
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from ark_pipeline.cli.spatial_pairs import (
    REPOSITORY_ROOT,
    exclusion_reason_values,
    iter_arrow_batches,
    load_acquisition_manifest,
    spatial_files,
)
from ark_pipeline.runtime.benchmark_estimates import SIZE_BREAKS
from ark_pipeline.runtime.progress import emit
from ark_pipeline.runtime.provenance import atomic_json, iso_now, runtime_identity
from ark_pipeline.spatial.coverage import (
    load_spatial_profile,
)

DEFAULT_PROFILE = (
    REPOSITORY_ROOT / "config" / "spatial_semantics_iucn_richness_v3.toml"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "data"
    / "spatial-test"
    / "benchmark-samples"
    / "iucn-polygons-stratified-1000.parquet"
)

# Half-decade-ish bands keep the tiny-range end well represented without
# dedicating most of the fixture to the overwhelmingly common small polygons.



@dataclass
class Candidate:
    priority: int
    logical_name: str
    source_sha256: str
    source_layer: str
    source_row: int
    iucn_sis_id: int
    presence: int
    origin: int
    seasonal: int | None
    bbox_min_x: float
    bbox_min_y: float
    bbox_max_x: float
    bbox_max_y: float
    bbox_area_degrees2: float
    planar_area_degrees2: float
    coordinate_count: int
    component_count: int
    geometry_valid: bool
    size_bin: int
    wkb_path: Path


def _size_bin(area: float) -> int:
    for index, boundary in enumerate(SIZE_BREAKS):
        if area < boundary:
            return index
    return len(SIZE_BREAKS)


def _priority(logical_name: str, layer: str, row: int, sis_id: int) -> int:
    identity = f"{logical_name}|{layer}|{row}|{sis_id}".encode()
    return int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")


def _store_wkb(directory: Path, key: str, wkb: bytes) -> Path:
    path = directory / f"{key}.wkb"
    path.write_bytes(wkb)
    return path


def _candidate(
    directory: Path,
    record: dict[str, Any],
    layer: str,
    source_row: int,
    sis_id: int,
    presence: int,
    origin: int,
    seasonal: int | None,
    wkb: bytes,
    sequence: int,
) -> Candidate:
    geometry = shapely.from_wkb(wkb)
    min_x, min_y, max_x, max_y = map(float, geometry.bounds)
    bbox_area = max(0.0, max_x - min_x) * max(0.0, max_y - min_y)
    priority = _priority(record["logical_name"], layer, source_row, sis_id)
    path = _store_wkb(directory, f"candidate-{sequence}", wkb)
    return Candidate(
        priority=priority,
        logical_name=str(record["logical_name"]),
        source_sha256=str(record["sha256"]),
        source_layer=layer,
        source_row=source_row,
        iucn_sis_id=sis_id,
        presence=presence,
        origin=origin,
        seasonal=seasonal,
        bbox_min_x=min_x,
        bbox_min_y=min_y,
        bbox_max_x=max_x,
        bbox_max_y=max_y,
        bbox_area_degrees2=bbox_area,
        planar_area_degrees2=float(shapely.area(geometry)),
        coordinate_count=int(shapely.get_num_coordinates(geometry)),
        component_count=int(shapely.get_num_geometries(geometry)),
        geometry_valid=bool(shapely.is_valid(geometry)),
        size_bin=_size_bin(bbox_area),
        wkb_path=path,
    )


def _delete_candidate(candidate: Candidate | None) -> None:
    if candidate is not None:
        candidate.wkb_path.unlink(missing_ok=True)


def _push_bounded(
    heap: list[tuple[int, int, Candidate]],
    candidate: Candidate,
    capacity: int,
    sequence: int,
) -> bool:
    item = (-candidate.priority, sequence, candidate)
    if len(heap) < capacity:
        heapq.heappush(heap, item)
        return True
    if candidate.priority >= -heap[0][0]:
        return False
    _, _, removed = heapq.heapreplace(heap, item)
    _delete_candidate(removed)
    return True


def _select_evenly(
    pools: list[list[Candidate]],
    limit: int,
    smallest: Candidate,
    largest: Candidate,
) -> list[Candidate]:
    ordered = [sorted(pool, key=lambda item: item.priority) for pool in pools if pool]
    selected: list[Candidate] = []
    seen: set[tuple[str, str, int]] = set()

    def add(item: Candidate) -> None:
        key = (item.logical_name, item.source_layer, item.source_row)
        if key not in seen:
            seen.add(key)
            selected.append(item)

    add(smallest)
    add(largest)
    depth = 0
    while len(selected) < limit:
        progressed = False
        for pool in ordered:
            if depth < len(pool):
                add(pool[depth])
                progressed = True
                if len(selected) == limit:
                    break
        if not progressed:
            break
        depth += 1
    return selected


def build_sample(
    data_root: Path,
    output: Path,
    profile_path: Path,
    limit: int,
    candidates_per_bin: int,
    logical_names: set[str],
) -> dict[str, Any]:
    started = time.perf_counter()
    profile = load_spatial_profile(profile_path)
    manifest = load_acquisition_manifest(data_root)
    records = spatial_files(data_root, manifest)
    if logical_names:
        records = [r for r in records if r["logical_name"] in logical_names]
        missing = logical_names - {r["logical_name"] for r in records}
        if missing:
            raise ValueError("unknown logical archive(s): " + ", ".join(sorted(missing)))
    output.parent.mkdir(parents=True, exist_ok=True)
    pools: list[list[tuple[int, int, Candidate]]] = [
        [] for _ in range(len(SIZE_BREAKS) + 1)
    ]
    archive_stats: list[dict[str, Any]] = []
    sequence = 0
    smallest: Candidate | None = None
    largest: Candidate | None = None

    with tempfile.TemporaryDirectory(prefix="spatial-sample-", dir=output.parent) as raw_tmp:
        temporary_dir = Path(raw_tmp)
        for archive_index, record in enumerate(records, start=1):
            archive_started = time.perf_counter()
            scanned = eligible = rejected = 0
            print(
                f"archive {archive_index}/{len(records)} {record['logical_name']}",
                flush=True,
            )
            for layer, geometry_name, fid_name, batch in iter_arrow_batches(
                record["resolved_path"], 512
            ):
                emit(phase=f"Sample {record['logical_name']} · {eligible:,} eligible polygons", completed=scanned, unit="archive rows")
                for index in range(batch.num_rows):
                    scanned += 1
                    sis_id = batch["id_no"][index].as_py()
                    wkb = batch[geometry_name][index].as_py()
                    presence = batch["presence"][index].as_py()
                    origin = batch["origin"][index].as_py()
                    seasonal = batch["seasonal"][index].as_py()
                    if exclusion_reason_values(
                        sis_id,
                        wkb,
                        presence,
                        origin,
                        profile,
                        seasonal=seasonal,
                    ) is not None:
                        rejected += 1
                        continue
                    try:
                        source_row = int(batch[fid_name][index].as_py())
                        geometry = shapely.from_wkb(wkb)
                        if geometry is None or geometry.is_empty:
                            rejected += 1
                            continue
                        min_x, min_y, max_x, max_y = map(float, geometry.bounds)
                        bbox_area = max(0.0, max_x - min_x) * max(
                            0.0, max_y - min_y
                        )
                        if not math.isfinite(bbox_area):
                            rejected += 1
                            continue
                    except Exception:
                        rejected += 1
                        continue

                    eligible += 1
                    sequence += 1
                    bin_index = _size_bin(bbox_area)
                    priority = _priority(
                        record["logical_name"], layer, source_row, int(sis_id)
                    )
                    heap = pools[bin_index]
                    could_enter_pool = (
                        len(heap) < candidates_per_bin
                        or priority < -heap[0][0]
                    )
                    is_new_smallest = (
                        smallest is None
                        or bbox_area < smallest.bbox_area_degrees2
                    )
                    is_new_largest = (
                        largest is None
                        or bbox_area > largest.bbox_area_degrees2
                    )
                    if not (could_enter_pool or is_new_smallest or is_new_largest):
                        continue
                    item = _candidate(
                        temporary_dir,
                        record,
                        layer,
                        source_row,
                        int(sis_id),
                        int(presence),
                        int(origin),
                        int(seasonal) if seasonal is not None else None,
                        wkb,
                        sequence,
                    )
                    retained = False
                    if could_enter_pool:
                        retained = _push_bounded(
                            heap, item, candidates_per_bin, sequence
                        )
                    if is_new_smallest:
                        smallest_path = _store_wkb(
                            temporary_dir, "forced-smallest", wkb
                        )
                        smallest = replace(item, wkb_path=smallest_path)
                    if is_new_largest:
                        largest_path = _store_wkb(
                            temporary_dir, "forced-largest", wkb
                        )
                        largest = replace(item, wkb_path=largest_path)
                    if not retained:
                        _delete_candidate(item)
            elapsed = time.perf_counter() - archive_started
            archive_stats.append(
                {
                    "logical_name": record["logical_name"],
                    "scanned": scanned,
                    "eligible": eligible,
                    "rejected": rejected,
                    "wall_seconds": elapsed,
                }
            )
            print(
                f"archive-complete {record['logical_name']} scanned={scanned} "
                f"eligible={eligible} seconds={elapsed:.1f}",
                flush=True,
            )

        if smallest is None or largest is None:
            raise ValueError("no eligible polygons found")
        candidates = [[entry[2] for entry in pool] for pool in pools]
        selected = _select_evenly(candidates, limit, smallest, largest)
        if len(selected) < limit:
            raise ValueError(
                f"only {len(selected)} stratified candidates available for limit {limit}"
            )
        selected.sort(
            key=lambda item: (
                item.bbox_area_degrees2,
                item.priority,
            )
        )
        rows = []
        for index, item in enumerate(selected, start=1):
            rows.append(
                {
                    "sample_id": index,
                    "logical_name": item.logical_name,
                    "source_sha256": item.source_sha256,
                    "source_layer": item.source_layer,
                    "source_row": item.source_row,
                    "iucn_sis_id": item.iucn_sis_id,
                    "presence": item.presence,
                    "origin": item.origin,
                    "seasonal": item.seasonal,
                    "geometry_wkb": item.wkb_path.read_bytes(),
                    "bbox_min_x": item.bbox_min_x,
                    "bbox_min_y": item.bbox_min_y,
                    "bbox_max_x": item.bbox_max_x,
                    "bbox_max_y": item.bbox_max_y,
                    "bbox_area_degrees2": item.bbox_area_degrees2,
                    "planar_area_degrees2": item.planar_area_degrees2,
                    "coordinate_count": item.coordinate_count,
                    "component_count": item.component_count,
                    "geometry_valid": item.geometry_valid,
                    "size_bin": item.size_bin,
                    "selection_priority_hex": f"{item.priority:016x}",
                }
            )
        table = pa.Table.from_pylist(rows)
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        pq.write_table(
            table,
            temporary_output,
            compression="zstd",
            compression_level=6,
            row_group_size=64,
        )
        temporary_output.replace(output)

    report = {
        "schema_version": 1,
        "created_at": iso_now(),
        "output": str(output),
        "rows": len(selected),
        "profile": str(profile_path),
        "profile_id": profile.profile_id,
        "profile_digest": profile.digest,
        "row_policy": {
            "presence": list(profile.presence),
            "origin": list(profile.origin),
            "seasonality": list(profile.seasonality),
        },
        "selection": {
            "method": "deterministic bottom-hash within fixed bbox-area bands; round-robin across non-empty bands",
            "size_metric": "longitude/latitude bbox area in square degrees",
            "size_breaks": list(SIZE_BREAKS),
            "candidates_per_bin": candidates_per_bin,
            "forced_extremes": True,
        },
        "minimum_bbox_area_degrees2": selected[0].bbox_area_degrees2,
        "maximum_bbox_area_degrees2": selected[-1].bbox_area_degrees2,
        "archive_stats": archive_stats,
        "runtime": runtime_identity(REPOSITORY_ROOT),
        "wall_seconds": time.perf_counter() - started,
    }
    atomic_json(output.with_suffix(".json"), report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--candidates-per-bin", type=int, default=75)
    parser.add_argument("--logical-name", action="append", default=[])
    args = parser.parse_args()
    report = build_sample(
        args.data_root,
        args.output,
        args.profile,
        args.limit,
        args.candidates_per_bin,
        set(args.logical_name),
    )
    print(
        f"sample-complete rows={report['rows']} seconds={report['wall_seconds']:.1f} "
        f"output={report['output']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
