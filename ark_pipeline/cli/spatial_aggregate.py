"""Aggregate verified archive pairs directly into resumable serving lists."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import duckdb
from rich.console import Console

from ark_pipeline.aggregation.species_lists import export_code_identity, export_serving_lists
from ark_pipeline.cli.spatial_pairs import (
    DEFAULT_PROFILE,
    _validated_archive_pairs,
    default_duckdb_memory_limit,
    default_duckdb_threads,
    doctor,
    load_acquisition_manifest,
    pair_stage_identities,
    pipeline_status,
)
from ark_pipeline.runtime.progress import tracked_stage
from ark_pipeline.runtime.provenance import atomic_json
from ark_pipeline.runtime.status_view import print_status
from ark_pipeline.spatial.coverage import load_spatial_profile


def archive_identities(root, profile) -> dict:
    _, identities = pair_stage_identities(root, load_acquisition_manifest(root), profile)
    return identities


def aggregation_status(root: Path, output: Path, profile) -> dict:
    """Extend the existing cheap status reader for direct archive aggregation."""
    report = pipeline_status(root, output, profile)
    legacy_status = dict(report)
    archives_current = bool(report["archives"]) and all(
        item["status"] == "present-unverified" for item in report["archives"]
    )
    report["aggregation"] = "missing" if archives_current else "blocked-by-archives"
    report["serving_lists"] = report["aggregation"]
    report["relations"] = "not-required-for-direct-aggregation"
    receipt_path = output / "serving/current/receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text())
        identity = receipt["identity"]
        if identity.get("source_mode") != "archive-pairs":
            legacy_status["aggregation"] = legacy_status["serving_lists"]
            return legacy_status
        if archives_current:
            expected = {"archive_stages": archive_identities(root, profile)}
            actual_inputs = {}
            for item in report["archives"]:
                path = output / "archives" / Path(item["archive"]).stem / "receipt.json"
                stage = json.loads(path.read_text())
                actual_inputs[item["archive"]] = stage["outputs"]["pairs"]["sha256"]
            stored_inputs = {item["logical_name"]: item["sha256"] for item in identity["inputs"]}
            current = (
                receipt.get("status") == "passed" and identity["relations"] == expected
                and actual_inputs == stored_inputs
                and all(identity.get(key) == value for key, value in export_code_identity().items())
                and all((receipt_path.parent / item["filename"]).is_file()
                        and (receipt_path.parent / item["filename"]).stat().st_size == item["bytes"]
                        for item in receipt["outputs"].values())
            )
            report["aggregation"] = "present-unverified" if current else "stale"
        report["serving_lists"] = report["aggregation"]
        ready = report["aggregation"] == "present-unverified" and all(
            item["status"] == "present" for item in report["sources"]
        )
        report["status"] = "present-unverified" if ready else "needs-work"
    except (OSError, KeyError, ValueError):
        if receipt_path.exists():
            report["aggregation"] = "stale"
            report["serving_lists"] = "stale"
    if archives_current and report["aggregation"] != "present-unverified":
        report["next_command"] = "just data-aggregate"
    elif report["aggregation"] == "present-unverified":
        report["next_command"] = (
            "just data-prepare (with a reviewed crosswalk)"
            if all(item["status"] == "present" for item in report["sources"])
            else "just download"
        )
    return report


@tracked_stage("lists")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "status"], nargs="?", default="run")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("GLOBAL_DATA_ROOT", "data/external")))
    parser.add_argument("--profile", type=Path, default=Path(os.environ.get("SPATIAL_PROFILE", str(DEFAULT_PROFILE))))
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--scratch-dir", type=Path, default=os.environ.get("DUCKDB_SCRATCH_DIR"))
    parser.add_argument("--memory-limit", default=os.environ.get("DUCKDB_MEMORY_LIMIT") or default_duckdb_memory_limit())
    parser.add_argument("--threads", type=int, default=os.environ.get("DUCKDB_THREADS") or default_duckdb_threads())
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ui", choices=("auto", "rich", "plain"), default="auto", help="Status presentation: Rich in a terminal, JSON when redirected.")
    parser.add_argument("--json", action="store_true", help="Print the status report as JSON, including every source error.")
    args = parser.parse_args(argv)
    try:
        root = args.root.expanduser().resolve()
        profile = load_spatial_profile(args.profile)
        if profile.production_kernel != "direct-any-touch-v2":
            raise ValueError("select an active any-touch profile")
        output = (args.output_root or root / "derived" / profile.profile_id).expanduser().resolve()
        if args.command == "status":
            report = aggregation_status(root, output, profile)
            console = Console()
            if not args.json and (args.ui == "rich" or args.ui == "auto" and console.is_terminal):
                print_status(report, console)
            else:
                print(json.dumps(report, indent=2))
            return 0
        preflight = doctor(root, profile, deep=False)
        if preflight["status"] != "ready":
            print(json.dumps(preflight, indent=2))
            return 1
        expected = archive_identities(root, profile)
        inputs = _validated_archive_pairs(output, profile, expected)
        if {item["logical_name"] for item in inputs} != set(expected):
            raise ValueError("archive output set is incomplete; run just spatial-build")
        result = export_serving_lists(
            output, scratch_dir=args.scratch_dir or root / "scratch/spatial",
            memory_limit=args.memory_limit, threads=args.threads, force=args.force,
            archive_inputs=inputs, archive_identity={"archive_stages": expected},
        )
        atomic_json(output / "aggregation-report.json", result)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, duckdb.Error) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
