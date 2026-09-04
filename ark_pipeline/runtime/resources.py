"""Shared process/thread settings for the sequential data-build stages."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Mapping

import psutil


def positive_int(value: str | int) -> int:
    number = int(value)
    if number < 1:
        raise ValueError("worker/thread counts must be positive integers")
    return number


def worker_count(value: str | int) -> str | int:
    return "auto" if value == "auto" else positive_int(value)


def automatic_workers() -> int:
    """Reserve memory for the coordinator and desktop before starting workers."""
    memory = psutil.virtual_memory()
    gib = 1024**3
    memory_workers = max(1, min(int(memory.total // (4 * gib)), int(memory.available // (2 * gib))))
    return max(1, min(8, os.cpu_count() or 1, memory_workers))


def configured_count(name: str, *, default: int | None = None) -> int:
    """Stage environment override, shared budget, then memory-aware auto default.

    Supplying default makes this a per-worker/helper setting that must not
    multiply the shared budget (for example RES7_THREADS defaults to one).
    """
    value = os.environ.get(name)
    if value:
        return positive_int(value)
    if default is not None:
        return default
    budget = os.environ.get("PIPELINE_WORKERS", "auto") or "auto"
    return automatic_workers() if budget == "auto" else positive_int(budget)


@dataclass(frozen=True)
class Resources:
    workers: int
    spatial_workers: int
    metric_workers: int
    duckdb_threads: int
    metric_threads: int
    tile_threads: int
    tile_duckdb_threads: int

    def environment(self) -> dict[str, str]:
        return {name: str(value) for name, value in {
            "PIPELINE_WORKERS": self.workers,
            "SPATIAL_WORKERS": self.spatial_workers,
            "RES7_WORKERS": self.metric_workers,
            "DUCKDB_THREADS": self.duckdb_threads,
            "RES7_THREADS": self.metric_threads,
            "TIPPECANOE_MAX_THREADS": self.tile_threads,
            "TILE_DUCKDB_THREADS": self.tile_duckdb_threads,
        }.items()}

    def report(self) -> dict[str, int]:
        return asdict(self)


def resolve_resources(*, workers: str | int | None = None,
                      spatial_workers: int | None = None, metric_workers: int | None = None,
                      duckdb_threads: int | None = None, metric_threads: int | None = None,
                      tile_threads: int | None = None, tile_duckdb_threads: int | None = None,
                      environ: Mapping[str, str] | None = None) -> Resources:
    env = os.environ if environ is None else environ
    requested = workers if workers is not None else env.get("PIPELINE_WORKERS") or "auto"
    budget = automatic_workers() if requested == "auto" else positive_int(requested)

    def stage(cli: int | None, name: str, default: int) -> int:
        # A command-line shared count overrides stale .env stage settings.
        # Explicit stage flags are always the most specific choice.
        value = cli if cli is not None else (env.get(name) if workers is None else None)
        return positive_int(value) if value not in (None, "") else default

    return Resources(
        workers=budget,
        spatial_workers=stage(spatial_workers, "SPATIAL_WORKERS", budget),
        metric_workers=stage(metric_workers, "RES7_WORKERS", budget),
        duckdb_threads=stage(duckdb_threads, "DUCKDB_THREADS", budget),
        metric_threads=stage(metric_threads, "RES7_THREADS", 1),
        tile_threads=stage(tile_threads, "TIPPECANOE_MAX_THREADS", budget),
        tile_duckdb_threads=stage(tile_duckdb_threads, "TILE_DUCKDB_THREADS", 1),
    )


def configure_duckdb(connection, *, threads: int | None = None) -> None:
    """Apply the same thread setting to the metadata/coarse/validation builders."""
    connection.execute("SET threads=?", [configured_count("DUCKDB_THREADS") if threads is None else positive_int(threads)])
