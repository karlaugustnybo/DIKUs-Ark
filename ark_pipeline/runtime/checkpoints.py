"""Atomic dashboard checkpoints, keyed by source and invocation identity."""

from __future__ import annotations

import json
from pathlib import Path

from ark_pipeline.runtime.provenance import code_fingerprint, sha256


def pipeline_code(repository: Path):
    return code_fingerprint([
        *sorted((repository / "ark_pipeline").rglob("*.py")),
        repository / "justfile",
    ])


def source_identity(root: Path):
    path = root / "acquisition/current.json"
    return sha256(path) if path.is_file() else None


def read_checkpoint(directory: Path, identity: dict):
    try:
        data = json.loads((directory / "dashboard-state.json").read_text())
        if data["schema_version"] == 1 and data["identity"] == identity and data["status"] != "passed":
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def find_checkpoint(parent: Path, identity: dict):
    for path in sorted(parent.glob("*/dashboard-state.json"), reverse=True):
        if read_checkpoint(path.parent, identity):
            return path.parent
    return None


def artifact_inventory(output: Path):
    """Cheap change detection for validated benchmark outputs between stages.

    Downstream production readers still perform their normal receipt/checksum
    validation; this inventory never grants permission to publish an output.
    """
    excluded = {"logs", "scratch", "progress.jsonl", "dashboard-state.json", "dashboard-state.json.tmp", ".run.lock",
                "benchmark-report.json", "benchmark-report.md", "benchmark-report.json.tmp", "polygon-timings.jsonl"}
    return {str(path.relative_to(output)): [path.stat().st_size, path.stat().st_mtime_ns]
            for path in output.rglob("*") if path.is_file() and not path.is_symlink()
            and not any(part in excluded for part in path.relative_to(output).parts)}


def validate_inventory(output: Path, inventory: dict):
    for relative, expected in inventory.items():
        path = output / relative
        if not path.resolve().is_relative_to(output.resolve()) or not path.is_file() or [path.stat().st_size, path.stat().st_mtime_ns] != expected:
            raise ValueError(f"Saved benchmark output changed or is missing: {relative}; start a fresh benchmark with --fresh")
