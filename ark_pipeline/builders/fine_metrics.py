#!/usr/bin/env python3
"""Build resumable resolution-7 aggregates and preview PMTiles.

The global res-7 input is already partitioned by H3 base cell. Each partition
is aggregated into one wide Parquet file containing all ecosystem metrics.
Completed files are immutable and automatically skipped, so an interrupted
global build resumes at the next base cell.

Priority sliders require exact threat-category x exclusive-DNA-level joint
counts. Marginal counts cannot reconstruct which DNA state belongs to which
threatened species. These joint counts are calculated from the raw species-ID
lists once here, allowing the web map to evaluate arbitrary weights exactly
without reading the roughly 18 GiB raw lookup dataset at runtime. The raw lists
remain necessary only for the selected-cell species-detail table.

The tile command can combine the existing resolution-3 DuckDB aggregates with
any completed resolution-7 partitions. This supports useful regional previews
before all 121 base cells have finished.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from pathlib import Path
from queue import Empty
from typing import Any, TextIO

import duckdb
import psutil
import pyarrow as pa
import pyarrow.parquet as pq
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from ark_pipeline.aggregation.metrics import aggregate_species_lists
from ark_pipeline.builders.coarse_cache import (
    DNA_SCORE_PREDICATES,
    METRICS,
    SYSTEM_PREDICATES,
    SYSTEMS,
    TILE_ZOOM_RANGES,
    iter_query_rows,
    score_expression,
    sql_path,
    wide_feature,
)
from ark_pipeline.runtime.progress import emit, tracked_stage
from ark_pipeline.runtime.progress import enabled as progress_enabled
from ark_pipeline.runtime.provenance import (
    atomic_json,
    code_fingerprint,
    dependency_identity,
    receipt_is_current,
    sha256,
)
from ark_pipeline.runtime.resources import configure_duckdb, configured_count, positive_int
from ark_pipeline.spatial.boundaries import JurisdictionIndex, load_jurisdiction_index

_WORKER_CONNECTION: duckdb.DuckDBPyConnection | None = None
_WORKER_PROGRESS_QUEUE: Any | None = None


@dataclass
class SystemSnapshot:
    cpu_percent: list[float] = field(default_factory=list)
    memory_used_gb: float = 0.0
    memory_total_gb: float = 0.0
    memory_percent: float = 0.0
    disk_read_mb: float = 0.0
    disk_write_mb: float = 0.0
    output_free_gb: float = 0.0


class SystemMonitor:
    """Sample lightweight system metrics without subprocesses or disk walks."""

    def __init__(self, output_dir: Path, interval: float = 1.0) -> None:
        self.output_dir = output_dir
        self.interval = interval
        self.snapshot = SystemSnapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_disk_io = None
        self._last_disk_time = 0.0

    def start(self) -> None:
        psutil.cpu_percent(percpu=True)
        self._last_disk_io = psutil.disk_io_counters()
        self._last_disk_time = time.monotonic()
        self._sample()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()

    def _sample(self) -> None:
        cpu_percent = psutil.cpu_percent(percpu=True)
        memory = psutil.virtual_memory()
        disk_io = psutil.disk_io_counters()
        now = time.monotonic()
        read_mb = 0.0
        write_mb = 0.0
        if self._last_disk_io is not None and disk_io is not None:
            elapsed = max(now - self._last_disk_time, 0.001)
            read_mb = (
                disk_io.read_bytes - self._last_disk_io.read_bytes
            ) / 1_000_000 / elapsed
            write_mb = (
                disk_io.write_bytes - self._last_disk_io.write_bytes
            ) / 1_000_000 / elapsed
        try:
            free_gb = shutil.disk_usage(self.output_dir).free / 1_000_000_000
        except OSError:
            free_gb = 0.0
        self.snapshot = SystemSnapshot(
            cpu_percent=cpu_percent,
            memory_used_gb=(memory.total - memory.available) / 1_000_000_000,
            memory_total_gb=memory.total / 1_000_000_000,
            memory_percent=memory.percent,
            disk_read_mb=max(0.0, read_mb),
            disk_write_mb=max(0.0, write_mb),
            output_free_gb=free_gb,
        )
        self._last_disk_io = disk_io
        self._last_disk_time = now


class BuildDisplay:
    """Rich terminal dashboard with a line-oriented non-TTY fallback."""

    def __init__(
        self,
        *,
        total: int,
        workers: int,
        memory_limit: str,
        output_dir: Path,
        work_weights: dict[int, int] | None = None,
        enabled: bool = True,
        console: Console | None = None,
    ) -> None:
        self.total = total
        self.workers = workers
        self.memory_limit = memory_limit
        self.output_dir = output_dir
        self.work_weights = work_weights or {
            base_cell: 1 for base_cell in range(total)
        }
        self.total_work = max(1, sum(self.work_weights.values()))
        self.completed_work = 0
        self.console = console or Console()
        self.enabled = enabled and self.console.is_terminal
        self.monitor = SystemMonitor(output_dir)
        self.logs: deque[str] = deque(maxlen=8)
        self.phase = "Preparing"
        self.active: dict[int, float] = {}
        self.active_phase: dict[int, str] = {}
        self.active_progress: dict[int, float] = {}
        self.completed = 0
        self.rebuilt = 0
        self.validated = 0
        self.aggregation_started_at: float | None = None
        self.aggregation_start_units = 0
        self.progress = Progress(
            TextColumn("[bold green]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TextColumn("{task.fields[eta]}"),
            expand=True,
        )
        self.task_id = self.progress.add_task(
            "Input coverage", total=self.total_work, eta="ETA calculating…"
        )
        self.live: Live | None = None

    def __enter__(self) -> BuildDisplay:
        if self.enabled:
            self.monitor.start()
            self.live = Live(
                self,
                console=self.console,
                refresh_per_second=1,
                screen=True,
            )
            self.live.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.enabled:
            self.monitor.stop()
        if self.live is not None:
            self.live.stop()
        if exc_type is KeyboardInterrupt:
            self.console.print(
                "[yellow]Resolution-7 build stopped; completed files are safe.[/]"
            )
        elif exc is not None:
            self.console.print(f"[bold red]Resolution-7 build failed:[/] {exc}")
        else:
            self.console.print(
                f"[bold green]Resolution-7 build complete:[/] "
                f"{self.completed}/{self.total} base cells validated."
            )

    def __rich_console__(self, console, options):
        yield self.render()

    def _emit(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.logs.appendleft(f"[dim]{timestamp}[/]  {message}")
        if not self.enabled:
            self.console.print(message)

    def begin_validation(self, count: int) -> None:
        suffix = "s" if count != 1 else ""
        self.phase = f"Validating {count} completed partition{suffix}"

    def validated_existing(self, base_cell: int, report: dict) -> None:
        self._remove_active(base_cell)
        self.validated += 1
        self.completed += 1
        self.completed_work += self.work_weights.get(base_cell, 1)
        self.progress.update(self.task_id, completed=self.completed_work)
        self._emit_total(force=True)
        self._emit(
            f"[green]✓[/] Base {base_cell} already valid · "
            f"{report['output_relationships']:,} relationships · "
            f"{report['validation_seconds']:.1f}s"
        )

    def invalid_existing(self, base_cell: int, error: Exception) -> None:
        self._remove_active(base_cell)
        self._emit(f"[yellow]↻[/] Base {base_cell} will be rebuilt · {error}")

    def begin_aggregation(self, pending: int) -> None:
        self.phase = (
            f"Aggregating {pending} partition{'s' if pending != 1 else ''} "
            f"with {self.workers} worker{'s' if self.workers != 1 else ''}"
        )
        if pending == 0:
            self.phase = "All requested partitions are already valid"
        self.aggregation_started_at = time.monotonic()
        self.aggregation_start_units = self.completed
        self.progress.reset(
            self.task_id,
            total=self.total_work,
            completed=self.completed_work,
            eta="ETA 0s" if pending == 0 else "ETA after first partition",
        )

    def partition_started(
        self, base_cell: int, phase: str = "Aggregating"
    ) -> None:
        self.active[base_cell] = time.monotonic()
        self.active_phase[base_cell] = phase
        self.active_progress[base_cell] = 0.0
        self.phase = f"{phase} base cell {base_cell}"
        emit(task=f"base:{base_cell}", phase=self.phase, fraction=0.0, unit="partition", force=True)

    def partition_progress(
        self, base_cell: int, phase: str, fraction: float
    ) -> None:
        """Update an active partition without producing noisy log lines."""
        if base_cell not in self.active:
            return
        self.active_phase[base_cell] = phase
        self.active_progress[base_cell] = max(
            self.active_progress.get(base_cell, 0.0),
            min(1.0, max(0.0, fraction)),
        )
        self.phase = f"{phase} base cell {base_cell}"
        emit(task=f"base:{base_cell}", phase=self.phase, fraction=fraction, unit="partition")
        self._emit_total()

    def _emit_total(self, *, force=False):
        emit("work", task="metric-total", phase="Aggregate & validate partitions", overall=True, force=force,
             completed=self.completed_work + sum(self.work_weights.get(base, 1) * value for base, value in self.active_progress.items()),
             total=self.total_work, unit="cells + relationships")

    def partition_completed(self, base_cell: int, report: dict) -> None:
        self._remove_active(base_cell)
        self.completed += 1
        self.rebuilt += 1
        self.completed_work += self.work_weights.get(base_cell, 1)
        self.progress.update(self.task_id, completed=self.completed_work)
        self._emit_total(force=True)
        self._emit(
            f"[green]✓[/] Base {base_cell} built · {report['rows']:,} cells · "
            f"{report['relationships']:,} relationships · "
            f"{report['bytes'] / 1_000_000:.1f} MB · {report['seconds']:.1f}s"
        )

    def partition_failed(self, base_cell: int, error: BaseException) -> None:
        self._remove_active(base_cell)
        self.phase = f"Base cell {base_cell} failed"
        self._emit(f"[bold red]✗ Base {base_cell} failed:[/] {error}")

    def _remove_active(self, base_cell: int) -> None:
        emit("task_end", task=f"base:{base_cell}")
        emit("task_end", task=f"base_{base_cell}")
        self.active.pop(base_cell, None)
        self.active_phase.pop(base_cell, None)
        self.active_progress.pop(base_cell, None)

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._render_header(), size=3),
            Layout(self._render_progress(), size=7),
            Layout(self._render_system(), size=7),
            Layout(self._render_logs()),
        )
        return layout

    def _render_header(self) -> Panel:
        subtitle = (
            f"[bold cyan]{self.phase}[/]  ·  workers {self.workers}  ·  "
            f"DuckDB {self.memory_limit}"
        )
        return Panel(
            Text.from_markup(subtitle, justify="center"),
            title="Ark-IV Global Resolution-7 Build",
            border_style="blue",
        )

    def _render_progress(self) -> Panel:
        live_work = self._live_completed_work()
        self.progress.update(
            self.task_id,
            completed=live_work,
            eta=self._eta_text(),
        )
        active = Table(show_header=False, box=None, expand=True)
        active.add_column(ratio=1)
        if self.active:
            now = time.monotonic()
            jobs = "  ".join(
                f"[cyan]base {base_cell}[/] · "
                f"{self.active_phase.get(base_cell, 'Working')} "
                f"{self.active_progress.get(base_cell, 0.0):.0%} · "
                f"{now - started:.0f}s"
                for base_cell, started in sorted(self.active.items())
            )
            active.add_row(f"Active  {jobs}")
        else:
            active.add_row("[dim]No active aggregation workers[/]")
        active.add_row(
            f"Validated [green]{self.completed}[/]  "
            f"Rebuilt [cyan]{self.rebuilt}[/]  "
            f"Remaining [yellow]{max(0, self.total - self.completed)}[/]"
        )
        return Panel(
            Group(self.progress, active),
            title="Progress",
            border_style="green",
            padding=(0, 1),
        )

    def _live_completed_work(self) -> float:
        return self.completed_work + sum(
            self.work_weights.get(base_cell, 1)
            * self.active_progress.get(base_cell, 0.0)
            for base_cell in self.active
        )

    def _eta_text(self) -> str:
        if self.completed >= self.total:
            return "ETA 0s"
        if self.aggregation_started_at is None:
            return "ETA calculating…"
        elapsed = time.monotonic() - self.aggregation_started_at
        live_units = self.completed + sum(self.active_progress.values())
        completed_units = live_units - self.aggregation_start_units
        if elapsed <= 0 or completed_units <= 0:
            return "ETA after first partition"
        unit_rate = completed_units / elapsed
        remaining_units = max(0, self.total - live_units)
        return f"ETA {self._format_duration(remaining_units / unit_rate)}"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"

    def _render_system(self) -> Panel:
        snapshot = self.monitor.snapshot
        cpu_average = (
            sum(snapshot.cpu_percent) / len(snapshot.cpu_percent)
            if snapshot.cpu_percent else 0.0
        )
        memory_colour = (
            "red" if snapshot.memory_percent >= 85
            else "yellow" if snapshot.memory_percent >= 70
            else "green"
        )
        table = Table(show_header=False, box=None, expand=True, padding=(0, 1))
        table.add_row(
            f"CPU [cyan]{cpu_average:.0f}%[/] average",
            f"RAM [{memory_colour}]{snapshot.memory_used_gb:.1f}/"
            f"{snapshot.memory_total_gb:.1f} GB ({snapshot.memory_percent:.0f}%)[/]",
        )
        table.add_row(
            f"Disk read [cyan]{snapshot.disk_read_mb:.0f} MB/s[/]",
            f"Disk write [cyan]{snapshot.disk_write_mb:.0f} MB/s[/]",
        )
        table.add_row(
            f"Output free [cyan]{snapshot.output_free_gb:.0f} GB[/]",
            f"Output [dim]{self.output_dir}[/]",
        )
        return Panel(table, title="System", border_style="cyan", padding=(0, 1))

    def _render_logs(self) -> Panel:
        table = Table(show_header=False, box=None, expand=True, padding=(0, 1))
        table.add_column(ratio=1)
        for message in list(self.logs)[:6]:
            table.add_row(message)
        if not self.logs:
            table.add_row("[dim]Waiting for the first partition…[/]")
        return Panel(table, title="Recent activity", border_style="dim", padding=(0, 1))


def initialize_aggregate_worker(
    species_path: Path,
    systems_path: Path,
    scratch_dir: Path,
    memory_limit: str,
    threads: int,
    progress_queue: Any | None = None,
) -> None:
    """Create one reusable DuckDB connection and species table per process."""
    global _WORKER_CONNECTION, _WORKER_PROGRESS_QUEUE
    _WORKER_PROGRESS_QUEUE = progress_queue
    _WORKER_CONNECTION = duckdb.connect()
    configure_connection(
        _WORKER_CONNECTION,
        scratch_dir=scratch_dir / f"worker-{os.getpid()}",
        memory_limit=memory_limit,
        threads=threads,
    )
    prepare_species(_WORKER_CONNECTION, species_path, systems_path)


def aggregate_worker(base_cell: int, source: Path, target: Path) -> tuple[int, dict]:
    if _WORKER_CONNECTION is None:
        raise RuntimeError("Aggregate worker was not initialized")
    progress = None
    if _WORKER_PROGRESS_QUEUE is not None:
        progress = partial(_report_worker_progress, base_cell)
    return base_cell, aggregate_part(
        _WORKER_CONNECTION,
        source,
        target,
        progress=progress,
    )


def _report_worker_progress(
    base_cell: int, phase: str, fraction: float
) -> None:
    if _WORKER_PROGRESS_QUEUE is not None:
        _WORKER_PROGRESS_QUEUE.put((base_cell, phase, fraction))


def base_cell_from_path(path: Path) -> int:
    try:
        return int(path.stem.removeprefix("base_"))
    except ValueError as exc:
        raise ValueError(f"Unexpected base-cell filename: {path.name}") from exc


def input_parts(parts_dir: Path) -> dict[int, Path]:
    return {
        base_cell_from_path(path): path
        for path in parts_dir.glob("base_*.parquet")
        if not path.name.startswith("._")
    }


def completed_parts(output_dir: Path) -> dict[int, Path]:
    completed: dict[int, Path] = {}
    expected = {"h3_index"} | {
        f"{metric}__{system.lower()}"
        for system in SYSTEMS
        for metric in METRICS
    }
    connection = duckdb.connect()
    configure_duckdb(connection)
    try:
        for path in output_dir.glob("base_*.parquet"):
            if not path.stem.removeprefix("base_").isdigit():
                continue
            try:
                row = connection.execute(
                    "SELECT sum(num_rows) FROM parquet_file_metadata(?)", [str(path)]
                ).fetchone()
                columns = {
                    description[0]
                    for description in connection.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
                    ).fetchall()
                }
            except duckdb.Error:
                continue
            if row and int(row[0]) > 0 and expected <= columns:
                completed[base_cell_from_path(path)] = path
    finally:
        connection.close()
    return completed


def aggregate_columns() -> list[str]:
    """Return summary and exact joint-priority counts for every system.

    The joint counts are deliberately stored instead of a preweighted score:
    a cell can then be recoloured for any slider values using a small dot
    product, with no runtime scan of its raw species IDs.
    """
    columns: list[str] = []
    for system, system_predicate in SYSTEM_PREDICATES.items():
        suffix = system.lower()
        for metric, metric_predicate in METRICS.items():
            columns.append(
                "COUNT(*) FILTER (WHERE "
                f"({system_predicate}) AND ({metric_predicate}))::INTEGER "
                f'AS "{metric}__{suffix}"'
            )
    return columns


def configure_connection(
    connection: duckdb.DuckDBPyConnection,
    *,
    scratch_dir: Path,
    memory_limit: str,
    threads: int,
) -> None:
    # Each metric process owns its Arrow pool as well as its DuckDB connection.
    pa.set_cpu_count(threads)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    connection.execute(f"SET temp_directory={sql_path(scratch_dir)}")
    connection.execute("SET memory_limit=?", [memory_limit])
    connection.execute("SET threads=?", [threads])
    connection.execute("SET preserve_insertion_order=false")
    _enable_query_progress(connection)


def _enable_query_progress(connection: duckdb.DuckDBPyConnection) -> None:
    """Calculate query progress without letting DuckDB print its own UI."""
    connection.execute("SET enable_progress_bar=true")
    connection.execute("SET enable_progress_bar_print=false")
    connection.execute("SET progress_bar_time=0")


def _run_with_query_progress(
    connection: duckdb.DuckDBPyConnection,
    operation: Callable[[], Any],
    *,
    phase: str,
    progress: Callable[[str, float | None], None] | None,
) -> Any:
    """Run one DuckDB operation while polling its native progress estimate.

    The direct path is deliberately retained for workers and non-interactive
    runs, so progress reporting adds no thread or polling overhead there.
    DuckDB returns -1 when a plan has no usable cardinality estimate; callers
    still receive the phase update in that case.
    """
    if progress is None:
        return operation()

    finished = threading.Event()
    result: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(operation())
        except BaseException as exception:
            error.append(exception)
        finally:
            finished.set()

    progress(phase, None)
    query_thread = threading.Thread(target=run, daemon=True)
    query_thread.start()
    try:
        while not finished.wait(0.5):
            percentage = connection.query_progress()
            fraction = None if percentage < 0 else min(1.0, percentage / 100)
            progress(phase, fraction)
    except BaseException:
        connection.interrupt()
        query_thread.join()
        raise
    query_thread.join()
    if error:
        raise error[0]
    progress(phase, 1.0)
    return result[0]


def _scaled_progress(
    progress: Callable[[str, float], None] | None,
    *,
    start: float,
    end: float,
) -> Callable[[str, float | None], None] | None:
    if progress is None:
        return None

    def update(phase: str, fraction: float | None) -> None:
        query_fraction = 0.0 if fraction is None else fraction
        progress(phase, start + (end - start) * query_fraction)

    return update


def prepare_species(
    connection: duckdb.DuckDBPyConnection,
    species_path: Path,
    systems_path: Path,
) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE species AS
        SELECT
            info.*,
            coalesce(bool_or(systems.system = 'Terrestrial'), false)
                AS is_terrestrial,
            coalesce(bool_or(systems.system = 'Freshwater'), false)
                AS is_freshwater,
            coalesce(bool_or(systems.system = 'Marine'), false)
                AS is_marine
        FROM read_parquet(?) info
        LEFT JOIN read_parquet(?) systems USING (gbif_accepted_id)
        GROUP BY ALL
        """,
        [str(species_path), str(systems_path)],
    )
    connection.execute(
        "CREATE UNIQUE INDEX preview_species_id ON species(gbif_accepted_id)"
    )


THREAT_SUMMARY_METRICS = {
    "cr": "crit_endangered_count",
    "en": "endangered_count",
    "vu": "vulnerable_count",
    "nt": "near_threatened_count",
    "dd": "data_deficient_count",
    "lc": "least_concern_count",
}


def _metric_consistency_predicate() -> str:
    """Return row-level invariants for the wide aggregate schema."""
    failures: list[str] = []
    for system in SYSTEMS:
        suffix = system.lower()
        total = f'"total_species__{suffix}"'
        threat_total = " + ".join(
            f'"{metric}__{suffix}"'
            for metric in THREAT_SUMMARY_METRICS.values()
        )
        failures.append(f"({threat_total}) > {total}")
        if system != "all":
            failures.append(f'{total} > "total_species__all"')
        for threat, summary_metric in THREAT_SUMMARY_METRICS.items():
            joint_total = " + ".join(
                f'"priority_{threat}_{dna}_count__{suffix}"'
                for dna in DNA_SCORE_PREDICATES
            )
            failures.append(
                f'({joint_total}) <> "{summary_metric}__{suffix}"'
            )
    return " OR ".join(f"({failure})" for failure in failures)


def validate_aggregate_part(
    connection: duckdb.DuckDBPyConnection,
    source: Path,
    aggregate: Path,
    *,
    expected_base_cell: int | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> dict[str, int | float]:
    """Fail unless a res-7 aggregate is a lossless summary of its source.

    These checks deliberately avoid expanding ``species_ids`` a second time.
    The upstream H3 builder already deduplicates each cell/species pair; here
    we verify the cheap invariants that can detect truncated files, missing
    species joins, misplaced H3 cells, duplicate cell rows, and inconsistent
    metric columns on every resumable build.
    """
    expected_base_cell = (
        base_cell_from_path(source)
        if expected_base_cell is None else expected_base_cell
    )
    started = time.monotonic()
    source_stats = _run_with_query_progress(
        connection,
        lambda: connection.execute(
            """
            SELECT
                count(*)::BIGINT AS cells,
                coalesce(sum(len(species_ids)), 0)::HUGEINT AS relationships,
                count(*) FILTER (WHERE species_ids IS NULL)::BIGINT AS null_lists,
                count(*) FILTER (WHERE len(species_ids) = 0)::BIGINT AS empty_lists,
                count(*) FILTER (WHERE h3_cell IS NULL)::BIGINT AS null_h3,
                count(*) FILTER (
                    WHERE ((h3_cell >> 52) & 15) <> 7
                )::BIGINT AS wrong_resolution,
                count(*) FILTER (
                    WHERE ((h3_cell >> 45) & 127) <> ?
                )::BIGINT AS wrong_base_cell,
                (count(*) - count(DISTINCT h3_cell))::BIGINT AS duplicate_cells
            FROM read_parquet(?)
            """,
            [expected_base_cell, str(source)],
        ).fetchone(),
        phase="Checking source",
        progress=_scaled_progress(progress, start=0.94, end=0.97),
    )
    encoded_h3 = "try_cast('0x' || h3_index AS UBIGINT)"
    output_stats = _run_with_query_progress(
        connection,
        lambda: connection.execute(
            f"""
            SELECT
                count(*)::BIGINT AS cells,
                coalesce(sum("total_species__all"), 0)::HUGEINT AS relationships,
                count(*) FILTER (
                    WHERE "total_species__all" <= 0
                )::BIGINT AS empty_cells,
                count(*) FILTER (
                    WHERE {encoded_h3} IS NULL
                )::BIGINT AS invalid_h3,
                count(*) FILTER (
                    WHERE {encoded_h3} IS NOT NULL
                      AND (({encoded_h3} >> 52) & 15) <> 7
                )::BIGINT AS wrong_resolution,
                count(*) FILTER (
                    WHERE {encoded_h3} IS NOT NULL
                      AND (({encoded_h3} >> 45) & 127) <> ?
                )::BIGINT AS wrong_base_cell,
                (count(*) - count(DISTINCT h3_index))::BIGINT AS duplicate_cells,
                count(*) FILTER (
                    WHERE {_metric_consistency_predicate()}
                )::BIGINT AS inconsistent_metric_cells
            FROM read_parquet(?)
            """,
            [expected_base_cell, str(aggregate)],
        ).fetchone(),
        phase="Checking output",
        progress=_scaled_progress(progress, start=0.97, end=0.995),
    )

    source_keys = (
        "source_cells",
        "source_relationships",
        "source_null_lists",
        "source_empty_lists",
        "source_null_h3",
        "source_wrong_resolution",
        "source_wrong_base_cell",
        "source_duplicate_cells",
    )
    output_keys = (
        "output_cells",
        "output_relationships",
        "output_empty_cells",
        "output_invalid_h3",
        "output_wrong_resolution",
        "output_wrong_base_cell",
        "output_duplicate_cells",
        "output_inconsistent_metric_cells",
    )
    report = {
        key: int(value)
        for key, value in zip(
            source_keys + output_keys,
            source_stats + output_stats,
            strict=True,
        )
    }
    report["dropped_cells"] = report["source_cells"] - report["output_cells"]
    report["dropped_relationships"] = (
        report["source_relationships"] - report["output_relationships"]
    )
    report["validation_seconds"] = round(time.monotonic() - started, 3)

    failures = [
        f"{key}={value}"
        for key, value in report.items()
        if key not in {
            "source_cells",
            "source_relationships",
            "output_cells",
            "output_relationships",
            "validation_seconds",
        }
        and value != 0
    ]
    if report["source_cells"] == 0:
        failures.append("source_partition_is_empty")
    if report["output_cells"] == 0:
        failures.append("output_partition_is_empty")
    if failures:
        raise RuntimeError(
            f"Resolution-7 validation failed for base {expected_base_cell}: "
            + ", ".join(failures)
        )
    return report


def validate_completed_parts(
    sources: dict[int, Path],
    completed: dict[int, Path],
    requested: list[int],
    display: BuildDisplay | None = None,
) -> dict[int, Path]:
    """Keep only resumable outputs that still pass source/output validation."""
    valid: dict[int, Path] = {}
    if display is not None:
        display.begin_validation(
            sum(base_cell in completed for base_cell in requested)
        )
    connection = duckdb.connect()
    configure_duckdb(connection)
    _enable_query_progress(connection)
    try:
        for base_cell in requested:
            target = completed.get(base_cell)
            if target is None:
                continue
            if display is not None:
                display.partition_started(base_cell, "Validating existing")
            progress = None
            if display is not None and display.enabled:
                progress = partial(display.partition_progress, base_cell)
            try:
                report = validate_aggregate_part(
                    connection,
                    sources[base_cell],
                    target,
                    expected_base_cell=base_cell,
                    progress=progress,
                )
            except (duckdb.Error, RuntimeError) as error:
                if display is not None:
                    display.invalid_existing(base_cell, error)
                else:
                    print(
                        f"base {base_cell}: existing output is invalid; rebuilding "
                        f"({error})",
                        flush=True,
                    )
                continue
            valid[base_cell] = target
            if display is not None:
                display.validated_existing(base_cell, report)
            else:
                print(
                    f"base {base_cell}: validated existing "
                    f"{report['output_relationships']:,} relationships in "
                    f"{report['validation_seconds']:.1f}s",
                    flush=True,
                )
    finally:
        connection.close()
    return valid


def aggregate_part(
    connection: duckdb.DuckDBPyConnection,
    source: Path,
    target: Path,
    *,
    progress: Callable[[str, float], None] | None = None,
) -> dict[str, float | int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    building = target.with_suffix(".building.parquet")
    if building.exists():
        building.unlink()
    started = time.monotonic()
    try:
        aggregate_species_lists(
            connection,
            source,
            building,
            progress=_scaled_progress(progress, start=0.0, end=0.94),
        )
        validation = validate_aggregate_part(
            connection,
            source,
            building,
            progress=progress,
        )
    except BaseException:
        building.unlink(missing_ok=True)
        raise
    if progress is not None:
        progress("Publishing", 0.995)
    building.replace(target)
    if progress is not None:
        progress("Complete", 1.0)
    return {
        "rows": validation["output_cells"],
        "relationships": validation["output_relationships"],
        "validation_seconds": validation["validation_seconds"],
        "seconds": round(time.monotonic() - started, 3),
        "bytes": target.stat().st_size,
    }


def aggregate_input_identity(args: argparse.Namespace, source: Path) -> dict:
    if not hasattr(args, "_aggregate_metadata_identity"):
        args._aggregate_metadata_identity = {
            "species_sha256": sha256(args.species),
            "systems_sha256": sha256(args.species_systems),
            "code_sha256": code_fingerprint([
                Path(__file__), Path(__file__).with_name("coarse_cache.py"),
                Path(__file__).resolve().parents[2] / "ark_pipeline/aggregation/metrics.py",
            ]),
            "dependencies": {**dependency_identity(), "numpy": version("numpy")},
        }
        args._aggregate_source_hashes = {}
    key = str(source.resolve())
    if key not in args._aggregate_source_hashes:
        args._aggregate_source_hashes[key] = sha256(source)
    return {"metadata": args._aggregate_metadata_identity,
            "source_sha256": args._aggregate_source_hashes[key]}


def aggregate_receipt_matches(args: argparse.Namespace, source: Path, target: Path) -> bool:
    try:
        schema = [[field.name, str(field.type)] for field in pq.read_schema(target)]
        return receipt_is_current(
            target.with_suffix(".receipt.json"), aggregate_input_identity(args, source),
            {"aggregate": target}, {"aggregate": schema},
        )
    except (OSError, ValueError):
        return False


def record_aggregate_receipt(args: argparse.Namespace, source: Path, target: Path) -> None:
    atomic_json(target.with_suffix(".receipt.json"), {
        "status": "passed", "identity": aggregate_input_identity(args, source),
        "outputs": {"aggregate": {"filename": target.name,
                    "bytes": target.stat().st_size, "sha256": sha256(target)}},
    })


@tracked_stage("fine_metrics")
def build_parts(args: argparse.Namespace) -> None:
    # Recompute once per invocation, including when a caller reuses its Namespace.
    if hasattr(args, "_aggregate_metadata_identity"):
        del args._aggregate_metadata_identity
    sources = input_parts(args.parts_dir)
    if not sources:
        raise FileNotFoundError(f"No base_*.parquet files in {args.parts_dir}")
    requested = sorted(set(args.base_cell or sources))
    unknown = [base_cell for base_cell in requested if base_cell not in sources]
    if unknown:
        raise ValueError(f"Missing input base cells: {unknown}")
    if args.limit is not None:
        requested = requested[: args.limit]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with BuildDisplay(
        total=len(requested),
        workers=args.workers,
        memory_limit=args.memory_limit,
        output_dir=args.output_dir,
        work_weights={
            base_cell: (lambda metadata: metadata.num_rows + sum(metadata.row_group(i).column(1).num_values for i in range(metadata.num_row_groups)))(pq.read_metadata(sources[base_cell]))
            for base_cell in requested
        },
        enabled=not args.no_progress,
    ) as display:
        existing = {} if args.overwrite else completed_parts(args.output_dir)
        existing = {
            base: target for base, target in existing.items()
            if base in sources and aggregate_receipt_matches(args, sources[base], target)
        }
        # Pin metadata and source identities before launching workers.
        for base in requested:
            aggregate_input_identity(args, sources[base])
        done = validate_completed_parts(
            sources,
            existing,
            requested,
            display,
        )
        pending = [base_cell for base_cell in requested if base_cell not in done]
        display.begin_aggregation(len(pending))
        if not pending:
            return

        if args.workers > 1:
            _build_parts_parallel(args, sources, pending, display)
        else:
            _build_parts_serial(args, sources, pending, display)


def _build_parts_parallel(
    args: argparse.Namespace,
    sources: dict[int, Path],
    pending: list[int],
    display: BuildDisplay,
) -> None:
    """Run a bounded worker queue and relay each DuckDB query's progress."""
    remaining = iter(pending)
    process_context = multiprocessing.get_context()
    progress_queue = process_context.Queue()
    try:
        with ProcessPoolExecutor(
            max_workers=args.workers,
            mp_context=process_context,
            initializer=initialize_aggregate_worker,
            initargs=(
                args.species,
                args.species_systems,
                args.scratch_dir,
                args.memory_limit,
                args.threads,
                progress_queue,
            ),
        ) as executor:
            futures = {}

            def submit_next() -> bool:
                try:
                    base_cell = next(remaining)
                except StopIteration:
                    return False
                display.partition_started(base_cell)
                future = executor.submit(
                    aggregate_worker,
                    base_cell,
                    sources[base_cell],
                    args.output_dir / f"base_{base_cell}.parquet",
                )
                futures[future] = base_cell
                return True

            def drain_progress() -> None:
                while True:
                    try:
                        base_cell, phase, fraction = progress_queue.get_nowait()
                    except Empty:
                        return
                    display.partition_progress(base_cell, phase, fraction)

            for _ in range(min(args.workers, len(pending))):
                submit_next()
            while futures:
                finished, _ = wait(
                    futures,
                    timeout=0.5,
                    return_when=FIRST_COMPLETED,
                )
                drain_progress()
                for future in finished:
                    expected_base_cell = futures.pop(future)
                    try:
                        base_cell, report = future.result()
                    except BaseException as error:
                        display.partition_failed(expected_base_cell, error)
                        for queued in futures:
                            queued.cancel()
                        raise
                    record_aggregate_receipt(args, sources[base_cell], args.output_dir / f"base_{base_cell}.parquet")
                    display.partition_completed(base_cell, report)
                    submit_next()
            drain_progress()
    finally:
        progress_queue.close()
        progress_queue.join_thread()


def _build_parts_serial(
    args: argparse.Namespace,
    sources: dict[int, Path],
    pending: list[int],
    display: BuildDisplay,
) -> None:
    connection = duckdb.connect()
    configure_connection(
        connection,
        scratch_dir=args.scratch_dir,
        memory_limit=args.memory_limit,
        threads=args.threads,
    )
    try:
        prepare_species(connection, args.species, args.species_systems)
        for base_cell in pending:
            target = args.output_dir / f"base_{base_cell}.parquet"
            display.partition_started(base_cell)
            try:
                progress = None
                if display.enabled or progress_enabled():
                    progress = partial(display.partition_progress, base_cell)
                report = aggregate_part(
                    connection,
                    sources[base_cell],
                    target,
                    progress=progress,
                )
            except BaseException as error:
                display.partition_failed(base_cell, error)
                raise
            record_aggregate_receipt(args, sources[base_cell], target)
            display.partition_completed(base_cell, report)
    finally:
        connection.close()


def wide_projection() -> str:
    return ", ".join(
        f'"{metric}__{system.lower()}"'
        for system in SYSTEMS
        for metric in METRICS
    )


def stream_res7_features(
    connection: duckdb.DuckDBPyConnection,
    parts: list[Path],
    stream: TextIO,
    *,
    batch_size: int = 10_000,
    jurisdiction_index: dict[str, JurisdictionIndex] | None = None,
) -> int:
    count = 0
    paths = "[" + ",".join(sql_path(path) for path in parts) + "]"
    query = (
        f"SELECT h3_index, {wide_projection()} FROM read_parquet({paths}) "
        "ORDER BY h3_index"
    )
    for row in iter_query_rows(connection, query, batch_size):
        code = {
            framework: index.codes_for_cell(row[0])
            for framework, index in (jurisdiction_index or {}).items()
        }
        stream.write(
            json.dumps(wide_feature(row, 7, code), separators=(",", ":")) + "\n"
        )
        count += 1
    return count


def stream_combined_features(
    connection: duckdb.DuckDBPyConnection,
    build_duckdb: Path,
    res7_parts: list[Path],
    stream: TextIO,
    jurisdiction_index: dict[str, JurisdictionIndex] | None = None,
) -> int:
    connection.execute(
        f"ATTACH {sql_path(build_duckdb)} AS coarse (READ_ONLY)"
    )
    count = 0
    aliases = [f"s{index}" for index in range(len(SYSTEMS))]
    projections = ["s0.h3_index"]
    for alias in aliases:
        projections.extend(f"coalesce({alias}.{metric}, 0)" for metric in METRICS)
    joins = " ".join(
        f"LEFT JOIN coarse.h3_res3_agg_{system} {alias} USING (h3_index)"
        for system, alias in zip(SYSTEMS[1:], aliases[1:], strict=True)
    )
    query = (
        f"SELECT {', '.join(projections)} FROM coarse.h3_res3_agg_all s0 "
        f"{joins} ORDER BY h3_index"
    )
    for row in iter_query_rows(connection, query):
        code = {
            framework: index.codes_for_cell(row[0])
            for framework, index in (jurisdiction_index or {}).items()
        }
        stream.write(
            json.dumps(wide_feature(row, 3, code), separators=(",", ":")) + "\n"
        )
        count += 1
    if res7_parts:
        count += stream_res7_features(
            connection, res7_parts, stream,
            jurisdiction_index=jurisdiction_index,
        )
    return count


def build_tiles(args: argparse.Namespace) -> None:
    parts = [path for _, path in sorted(completed_parts(args.parts_dir).items())]
    if args.base_cell:
        requested = set(args.base_cell)
        parts = [path for path in parts if base_cell_from_path(path) in requested]
    if not parts:
        raise FileNotFoundError("No completed resolution-7 aggregate parts")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.scratch_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    jurisdiction_index = {
        framework: load_jurisdiction_index(str(path.resolve()))
        for framework, path in {
            "admin0": args.jurisdictions,
            "admin1": args.admin1_boundaries,
            "municipality": args.municipality_boundaries,
            "eez": args.eez_boundaries,
            "conservation_framework": args.conservation_boundaries,
        }.items()
        if path.is_file()
    }
    process = subprocess.Popen(
        [
            args.tippecanoe,
            "--force",
            "--output", str(args.output),
            "--minimum-zoom", "0",
            "--maximum-zoom", "12",
            "--no-feature-limit",
            "--no-tile-size-limit",
            "--preserve-input-order",
            "--generate-ids",
            "--read-parallel",
            "--temporary-directory", str(args.scratch_dir),
            "--quiet",
        ],
        stdin=subprocess.PIPE,
        text=True,
    )
    if process.stdin is None:
        process.kill()
        raise RuntimeError("Tippecanoe did not expose stdin")
    try:
        feature_count = stream_combined_features(
            connection, args.build_duckdb, parts, process.stdin,
            jurisdiction_index,
        )
    except BaseException:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.kill()
        process.wait()
        raise
    finally:
        connection.close()
    process.stdin.close()
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, process.args)
    print(
        f"Built {args.output} from {feature_count:,} features across "
        f"{len(parts)} resolution-7 base cells"
    )
    if args.metadata_template and args.metadata_output:
        write_preview_metadata(
            connection=duckdb.connect(),
            template=args.metadata_template,
            target=args.metadata_output,
            parts=parts,
            source_parts_dir=args.source_parts_dir,
        )


def write_preview_metadata(
    *,
    connection: duckdb.DuckDBPyConnection,
    template: Path,
    target: Path,
    parts: list[Path],
    source_parts_dir: Path | None,
) -> None:
    """Extend res-3 metadata with the exact res-7 preview coverage."""
    try:
        metadata = json.loads(template.read_text())
        paths = "[" + ",".join(sql_path(path) for path in parts) + "]"
        for system in SYSTEMS:
            suffix = system.lower()
            expression = score_expression(
                lambda metric: f'cast("{metric}__{suffix}" AS DOUBLE)'
            )
            minimum, maximum, normalized_minimum, normalized_maximum = connection.execute(
                f"SELECT min(cast(({expression}) AS DOUBLE)), "
                f"max(cast(({expression}) AS DOUBLE)), "
                f"min(cast(({expression}) AS DOUBLE) / "
                f"nullif(cast(\"total_species__{suffix}\" AS DOUBLE), 0)), "
                f"max(cast(({expression}) AS DOUBLE) / "
                f"nullif(cast(\"total_species__{suffix}\" AS DOUBLE), 0)) "
                f"FROM read_parquet({paths})"
            ).fetchone()
            domain = metadata["score_domains"][suffix]
            domain["min"] = min(float(domain["min"]), float(minimum or 0))
            domain["max"] = max(float(domain["max"]), float(maximum or 0))
            normalized_domain = metadata["species_normalized_score_domains"][suffix]
            normalized_domain["min"] = min(
                float(normalized_domain["min"]), float(normalized_minimum or 0)
            )
            normalized_domain["max"] = max(
                float(normalized_domain["max"]), float(normalized_maximum or 0)
            )

        completed = sorted(base_cell_from_path(path) for path in parts)
        source = input_parts(source_parts_dir) if source_parts_dir else {}
        is_complete = bool(source) and set(completed) == set(source)
        metadata.update({
            "version": 9,
            "tile_schema_version": 9,
            "tile_layout": "wide-v2-joint-priority",
            "resolution_tile_ranges": {
                str(resolution): TILE_ZOOM_RANGES[resolution]
                for resolution in (3, 7)
            },
            "available_resolutions": [3, 7],
            "complete_resolutions": [3, 7] if is_complete else [3],
            "detail_resolutions": [3, 7],
            "res7_base_cells": completed,
        })
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(metadata, separators=(",", ":")) + "\n")
        print(f"Exported {target}")
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command", required=True)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--parts-dir", type=Path, required=True)
    aggregate.add_argument("--species", type=Path, required=True)
    aggregate.add_argument("--species-systems", type=Path, required=True)
    aggregate.add_argument("--output-dir", type=Path, required=True)
    aggregate.add_argument("--scratch-dir", type=Path, required=True)
    aggregate.add_argument("--base-cell", type=int, action="append")
    aggregate.add_argument("--limit", type=int)
    aggregate.add_argument("--memory-limit", default="4GB")
    aggregate.add_argument("--workers", type=positive_int, default=configured_count("RES7_WORKERS"))
    aggregate.add_argument(
        "--threads", type=positive_int, default=configured_count("RES7_THREADS", default=1)
    )
    aggregate.add_argument("--overwrite", action="store_true")
    aggregate.add_argument(
        "--no-progress",
        action="store_true",
        help="Use line-oriented logs instead of the interactive Rich dashboard.",
    )
    aggregate.set_defaults(run=build_parts)

    tiles = subparsers.add_parser("tiles")
    tiles.add_argument("--parts-dir", type=Path, required=True)
    tiles.add_argument("--build-duckdb", type=Path, required=True)
    tiles.add_argument("--output", type=Path, required=True)
    tiles.add_argument("--scratch-dir", type=Path, required=True)
    tiles.add_argument("--base-cell", type=int, action="append")
    tiles.add_argument("--source-parts-dir", type=Path)
    tiles.add_argument("--metadata-template", type=Path)
    tiles.add_argument("--metadata-output", type=Path)
    tiles.add_argument("--tippecanoe", default="tippecanoe")
    tiles.add_argument(
        "--jurisdictions", type=Path,
        default=Path("data/boundaries/country-scope.geojson"),
    )
    tiles.add_argument(
        "--admin1-boundaries", type=Path,
        default=Path("app/static/data/boundaries/admin1.geojson"),
    )
    tiles.add_argument(
        "--municipality-boundaries", type=Path,
        default=Path("app/static/data/boundaries/municipality.geojson"),
    )
    tiles.add_argument(
        "--eez-boundaries", type=Path,
        default=Path("data/boundaries/eez.geojson"),
    )
    tiles.add_argument(
        "--conservation-boundaries", type=Path,
        default=Path("app/static/data/boundaries/conservation-framework.geojson"),
    )
    tiles.set_defaults(run=build_tiles)
    return root


def main() -> None:
    args = parser().parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
