"""Content-addressed receipts for safely resumable pipeline stages."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq

from ark_pipeline.spatial.coverage import canonical_json


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    from ark_pipeline.runtime.progress import emit, enabled

    digest = hashlib.sha256()
    progress = enabled()
    total = path.stat().st_size if progress else 0
    done = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
            if progress:
                done += len(chunk)
                emit(task=f"hash:{os.getpid()}", phase=f"Hash {path.name}", completed=done, total=total,
                     fraction=done / max(1, total), unit="file bytes")
    if progress:
        emit("task_end", task=f"hash:{os.getpid()}")
    return digest.hexdigest()


def code_fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((path.resolve() for path in paths), key=str):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git_state(repository: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    try:
        return {
            "commit": run("rev-parse", "HEAD"),
            "dirty": bool(run("status", "--porcelain")),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def runtime_identity(repository: Path) -> dict[str, Any]:
    dependencies = dependency_identity()
    return {
        "git": git_state(repository),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "dependencies": dependencies,
    }


def dependency_identity() -> dict[str, str | None]:
    dependencies: dict[str, str | None] = {}
    for package in ("duckdb", "h3", "h3ronpy", "pyarrow", "pyogrio", "shapely"):
        try:
            dependencies[package] = version(package)
        except Exception:  # pragma: no cover - packaging metadata is environment-specific
            dependencies[package] = None
    return dependencies


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def receipt_is_current(
    receipt_path: Path,
    expected_identity: dict[str, Any],
    outputs: dict[str, Path],
    schemas: dict[str, list[list[str]]],
) -> bool:
    if not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("status") != "passed":
            return False
        if receipt.get("identity") != expected_identity:
            return False
        records = receipt["outputs"]
        for name, path in outputs.items():
            record = records[name]
            if not path.is_file() or path.stat().st_size != record["bytes"]:
                return False
            if sha256(path) != record["sha256"]:
                return False
            actual_schema = [[field.name, str(field.type)] for field in pq.read_schema(path)]
            if actual_schema != schemas[name]:
                return False
        return True
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return False


def identity_digest(identity: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(identity)).hexdigest()
