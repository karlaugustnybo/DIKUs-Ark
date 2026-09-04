#!/usr/bin/env python3
"""Compare versioned IUCN row policies without loading polygon geometry."""

from __future__ import annotations

import argparse
import itertools
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pyogrio

from ark_pipeline.cli.spatial_pairs import (
    REPOSITORY_ROOT,
    load_acquisition_manifest,
    spatial_files,
)
from ark_pipeline.runtime.provenance import atomic_json, iso_now, runtime_identity
from ark_pipeline.spatial.coverage import SpatialProfile, load_spatial_profile

DEFAULT_PROFILES = (
    REPOSITORY_ROOT / "config" / "spatial_semantics_any_touch_v2.toml",
    REPOSITORY_ROOT / "config" / "spatial_semantics_iucn_richness_v3.toml",
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "data"
    / "spatial-test"
    / "benchmark-diagnostics"
    / "row-policy-audit.json"
)


def _decision(
    profile: SpatialProfile,
    iucn_sis_id: int | None,
    presence: int | None,
    origin: int | None,
    seasonal: int | None,
) -> str:
    # Geometry is deliberately absent from this fast audit. The build performs
    # its null-geometry check before these attribute-policy checks.
    if iucn_sis_id is None:
        return "null_iucn_sis_id"
    if presence not in profile.presence:
        return "presence_policy"
    if origin not in profile.origin:
        return "origin_policy"
    if profile.seasonality and seasonal not in profile.seasonality:
        return "seasonality_policy"
    return "eligible_by_attributes"


def audit(
    data_root: Path,
    profile_paths: list[Path],
    output: Path,
) -> dict[str, Any]:
    started = time.perf_counter()
    profiles = [load_spatial_profile(path) for path in profile_paths]
    records = spatial_files(data_root, load_acquisition_manifest(data_root))
    decisions = {profile.profile_id: Counter() for profile in profiles}
    profile_pairs = list(itertools.combinations(profiles, 2))
    transitions = {
        (left.profile_id, right.profile_id): Counter()
        for left, right in profile_pairs
    }
    combinations: Counter[tuple[int | None, int | None, int | None]] = Counter()
    archives = []
    total_rows = 0
    for position, record in enumerate(records, start=1):
        archive_started = time.perf_counter()
        archive_rows = 0
        archive_decisions = {profile.profile_id: Counter() for profile in profiles}
        for layer_name, _ in pyogrio.list_layers(record["resolved_path"]):
            with pyogrio.open_arrow(
                record["resolved_path"],
                layer=str(layer_name),
                columns=["id_no", "presence", "origin", "seasonal"],
                read_geometry=False,
                batch_size=65_536,
                use_pyarrow=True,
            ) as (_, reader):
                for batch in reader:
                    rows = batch.to_pylist()
                    archive_rows += len(rows)
                    for row in rows:
                        sis_id = row.get("id_no")
                        presence = row.get("presence")
                        origin = row.get("origin")
                        seasonal = row.get("seasonal")
                        combinations[(presence, origin, seasonal)] += 1
                        row_decisions = {}
                        for profile in profiles:
                            decision = _decision(
                                profile, sis_id, presence, origin, seasonal
                            )
                            row_decisions[profile.profile_id] = decision
                            decisions[profile.profile_id][decision] += 1
                            archive_decisions[profile.profile_id][decision] += 1
                        for left, right in profile_pairs:
                            left_eligible = (
                                row_decisions[left.profile_id]
                                == "eligible_by_attributes"
                            )
                            right_eligible = (
                                row_decisions[right.profile_id]
                                == "eligible_by_attributes"
                            )
                            label = (
                                "both_eligible"
                                if left_eligible and right_eligible
                                else "left_only"
                                if left_eligible
                                else "right_only"
                                if right_eligible
                                else "neither_eligible"
                            )
                            transitions[(left.profile_id, right.profile_id)][label] += 1
        total_rows += archive_rows
        archives.append(
            {
                "logical_name": record["logical_name"],
                "source_rows": archive_rows,
                "profiles": {
                    profile_id: dict(sorted(counts.items()))
                    for profile_id, counts in archive_decisions.items()
                },
                "wall_seconds": time.perf_counter() - archive_started,
            }
        )
        print(
            f"policy-audit {position}/{len(records)} "
            f"{record['logical_name']} rows={archive_rows}",
            flush=True,
        )
    profile_reports = []
    for path, profile in zip(profile_paths, profiles, strict=True):
        counts = decisions[profile.profile_id]
        if sum(counts.values()) != total_rows:
            raise RuntimeError(f"row reconciliation failed for {profile.profile_id}")
        profile_reports.append(
            {
                "path": str(path),
                "id": profile.profile_id,
                "sha256": profile.digest,
                "row_policy": {
                    "presence": list(profile.presence),
                    "origin": list(profile.origin),
                    "seasonality": list(profile.seasonality),
                },
                "decisions": dict(sorted(counts.items())),
            }
        )
    report = {
        "schema_version": 1,
        "created_at": iso_now(),
        "scope": "attribute-only; geometry nulls are checked by the build audit",
        "source_rows": total_rows,
        "profiles": profile_reports,
        "profile_pair_comparisons": [
            {
                "left_profile_id": left_id,
                "right_profile_id": right_id,
                "transitions": dict(sorted(counts.items())),
            }
            for (left_id, right_id), counts in transitions.items()
        ],
        "attribute_combinations": [
            {
                "presence": key[0],
                "origin": key[1],
                "seasonality": key[2],
                "rows": count,
            }
            for key, count in sorted(
                combinations.items(),
                key=lambda item: (
                    -item[1],
                    *(value if value is not None else -1 for value in item[0]),
                ),
            )
        ],
        "archives": archives,
        "runtime": runtime_identity(REPOSITORY_ROOT),
        "wall_seconds": time.perf_counter() - started,
    }
    atomic_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    report = audit(
        arguments.data_root,
        arguments.profile or list(DEFAULT_PROFILES),
        arguments.output,
    )
    print(
        f"policy-audit-complete rows={report['source_rows']} "
        f"profiles={len(report['profiles'])} "
        f"seconds={report['wall_seconds']:.2f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
