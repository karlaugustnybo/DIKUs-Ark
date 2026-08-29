#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Build the global H3 cell -> species aggregation from raw h3_pairs data.
#
# Input:  ~22B (h3_cell, res, id_no) triples in h3_pairs/class=*.parquet
# Output: (h3_cell, species_ids) parquet files for res=3 and res=7
#
# Why this pipeline exists:
#   A naive `GROUP BY h3_cell, list(DISTINCT id_no)` on 22B rows blows up
#   RAM and causes massive disk spill. We avoid this by:
#
#     1. Integer encoding: id_no is ALREADY an integer, h3_cell is ALREADY
#        a UBIGINT. No string dictionary needed.
#     2. Base-cell partitioning: split res=7 data into 122 buckets by H3
#        base cell (extracted via bit ops, no h3 extension needed). Each
#        bucket fits in RAM.
#     3. Two-stage GROUP BY: first deduplicate (h3_cell, id_no), then
#        aggregate into list(id_no). Avoids list(DISTINCT) blowup.
#
# Usage:
#   uv run app/build_h3_aggregate.py                  # run all steps
#   uv run app/build_h3_aggregate.py --step partition # run one step
#   uv run app/build_h3_aggregate.py --step res7 --base-cell 5  # test one cell
#   uv run app/build_h3_aggregate.py --step res7 --resume       # skip done cells
#
# Temperature monitoring:
#   CPU temp requires sudo. Run `sudo -v` in your terminal first to cache
#   credentials, then run this script — it will use `sudo -n powermetrics`
#   to read CPU/GPU die temperature. Without sudo, only the internal SSD
#   temperature is shown (via ioreg, no sudo needed).
# ---------------------------------------------------------------------------

import argparse
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import psutil
from dotenv import load_dotenv
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def configured_path(name: str, default: str) -> Path:
    value = Path(os.environ.get(name, default)).expanduser()
    return value if value.is_absolute() else (ROOT / value).resolve()

# Input: raw (h3_cell, res, id_no) pairs — one parquet file per taxonomic class
INPUT_DIR = configured_path("H3_INPUT_DIR", "data/h3_pairs")
INPUT_GLOB = str(INPUT_DIR / "*.parquet")

# Intermediate: partitioned by (res, base_cell) — roughly same size as input
ENCODED_DIR = configured_path("H3_ENCODED_DIR", "data/h3_encoded")

# Output: final aggregated parquet files
OUTPUT_DIR = configured_path("H3_AGGREGATED_DIR", "data/h3_aggregated")

# DuckDB spill directory — needs 50GB+ free, must NOT be the system SSD
SCRATCH_DIR = configured_path("DUCKDB_SCRATCH_DIR", "data/duckdb_scratch")

# DuckDB tuning
MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "4GB")
THREADS = int(os.environ.get("DUCKDB_THREADS", "1"))

# H3 base cell extraction: bits 51-45 of the H3 index encode the base cell
# (0-121). This avoids needing the DuckDB h3 extension.
BASE_CELL_MASK = 127  # 7 bits (H3 base cells 0-121)
BASE_CELL_SHIFT = 45


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------
console = Console()


# ---------------------------------------------------------------------------
# System Monitor (background thread)
# ---------------------------------------------------------------------------
@dataclass
class SystemSnapshot:
    cpu_percent: list = field(default_factory=list)   # per-core %
    cpu_avg: float = 0.0
    mem_used_gb: float = 0.0
    mem_total_gb: float = 0.0
    mem_percent: float = 0.0
    ssd_temp: float = 0.0       # internal SSD °C (ioreg)
    cpu_temp: float = 0.0       # CPU die °C (powermetrics, needs sudo)
    gpu_temp: float = 0.0       # GPU die °C (powermetrics, needs sudo)
    disk_read_mb: float = 0.0   # MB/s
    disk_write_mb: float = 0.0  # MB/s
    scratch_gb: float = 0.0      # DuckDB scratch dir size
    has_cpu_temp: bool = False


class SystemMonitor:
    """Background thread that periodically samples system metrics."""

    def __init__(self, interval=2.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.snapshot = SystemSnapshot()
        self._last_disk_io = None
        self._last_disk_time = None
        self._can_powermetrics = self._check_sudo()

    def _check_sudo(self):
        """Check if we can run powermetrics with sudo (cached sudo)."""
        try:
            r = subprocess.run(
                ["sudo", "-n", "powermetrics", "-i", "1", "-n", "1"],
                capture_output=True, text=True, timeout=15,
            )
            return r.returncode == 0
        except Exception:
            return False

    def start(self):
        psutil.cpu_percent(percpu=True)  # prime the counters
        self._last_disk_io = psutil.disk_io_counters()
        self._last_disk_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval)

    def _sample(self):
        s = self.snapshot

        # CPU
        s.cpu_percent = psutil.cpu_percent(percpu=True)
        s.cpu_avg = sum(s.cpu_percent) / len(s.cpu_percent) if s.cpu_percent else 0

        # Memory
        mem = psutil.virtual_memory()
        s.mem_used_gb = (mem.total - mem.available) / 1e9
        s.mem_total_gb = mem.total / 1e9
        s.mem_percent = mem.percent

        # Disk I/O rate
        io = psutil.disk_io_counters()
        now = time.time()
        if self._last_disk_io and io and self._last_disk_time:
            dt = now - self._last_disk_time
            s.disk_read_mb = (io.read_bytes - self._last_disk_io.read_bytes) / 1e6 / dt
            s.disk_write_mb = (io.write_bytes - self._last_disk_io.write_bytes) / 1e6 / dt
        self._last_disk_io = io
        self._last_disk_time = now

        # Internal SSD temp (ioreg, no sudo)
        s.ssd_temp = self._read_ssd_temp()

        # CPU/GPU temp (powermetrics, needs sudo)
        # Note: M1 Macs don't expose die temperature via powermetrics.
        # We try anyway in case future macOS versions add it back.
        if self._can_powermetrics:
            cpu, gpu = self._read_cpu_temp()
            if cpu is not None and cpu > 0:
                s.cpu_temp = cpu
                s.gpu_temp = gpu or 0
                s.has_cpu_temp = True

        # Scratch dir size
        s.scratch_gb = self._dir_size_gb(SCRATCH_DIR)

    @staticmethod
    def _read_ssd_temp():
        try:
            r = subprocess.run(
                ["ioreg", "-c", "AppleEmbeddedNVMeTemperatureSensor", "-l", "-w", "0"],
                capture_output=True, text=True, timeout=5,
            )
            m = re.search(r'"Temperature" = (\d+)', r.stdout)
            if m:
                return int(m.group(1)) / 100.0
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _read_cpu_temp():
        try:
            r = subprocess.run(
                ["sudo", "-n", "powermetrics", "-i", "1", "-n", "1"],
                capture_output=True, text=True, timeout=15,
            )
            # Debug: dump raw output to file on first call
            debug_path = "/tmp/powermetrics_output.txt"
            if not os.path.exists(debug_path):
                with open(debug_path, "w") as f:
                    f.write(r.stdout[:2000])
            
            # Broad regex — match any "temperature" line with a numeric value
            cpu_m = re.search(r'CPU\s+die\s+(?:temp|temperature):\s*([\d.]+)', r.stdout, re.IGNORECASE)
            if not cpu_m:
                # Try broader pattern: any line with "temperature" and a number
                temps = re.findall(r'(\w+)\s+(?:die\s+)?temperature:\s*([\d.]+)', r.stdout, re.IGNORECASE)
                cpu = None
                gpu = None
                for name, val in temps:
                    if 'cpu' in name.lower():
                        cpu = float(val)
                    elif 'gpu' in name.lower():
                        gpu = float(val)
                if cpu is None and temps:
                    cpu = float(temps[0][1])  # first temperature found
                return cpu, gpu
            gpu_m = re.search(r'GPU\s+die\s+(?:temp|temperature):\s*([\d.]+)', r.stdout, re.IGNORECASE)
            cpu = float(cpu_m.group(1)) if cpu_m else None
            gpu = float(gpu_m.group(1)) if gpu_m else None
            return cpu, gpu
        except Exception:
            return None, None

    @staticmethod
    def _dir_size_gb(path):
        total = 0
        try:
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
        except Exception:
            pass
        return total / 1e9


# ---------------------------------------------------------------------------
# Display rendering
# ---------------------------------------------------------------------------
class Display:
    """Manages the rich live display with progress, metrics, and logs."""

    def __init__(self, monitor: SystemMonitor):
        self.monitor = monitor
        self.logs = deque(maxlen=8)
        self.step_name = ""
        self.step_num = 0
        self.step_total = 5
        self.phase = ""          # sub-description within a step
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("({task.percentage:>3.0f}%)"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            expand=True,
        )
        self.task_id = None
        self._extra_info = {}    # extra key->value pairs shown under progress

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.logs.appendleft(f"[dim]{ts}[/dim]  {msg}")

    def set_step(self, num, total, name):
        self.step_num = num
        self.step_total = total
        self.step_name = name
        self.phase = ""
        self._extra_info = {}
        self.log(f"[bold]Starting step {num}/{total}:[/bold] {name}")

    def set_phase(self, phase):
        self.phase = phase

    def set_info(self, **kwargs):
        self._extra_info.update(kwargs)

    def init_progress(self, total, description="Progress"):
        if self.task_id is not None:
            self.progress.remove_task(self.task_id)
        self.task_id = self.progress.add_task(description, total=total)
        return self.task_id

    def update_progress(self, advance=1, **kwargs):
        if self.task_id is not None:
            self.progress.update(self.task_id, advance=advance, **kwargs)

    def __rich_console__(self, console, options):
        yield self.render()

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._render_header(), size=3),
            Layout(self._render_progress(), size=4),
            Layout(self._render_system(), size=8),
            Layout(self._render_logs()),
        )
        return layout

    def _render_header(self) -> Panel:
        title = "Ark-IV H3 Aggregation Pipeline"
        subtitle = f"Step {self.step_num}/{self.step_total}: [bold cyan]{self.step_name}[/bold cyan]"
        if self.phase:
            subtitle += f"  —  [dim]{self.phase}[/dim]"
        return Panel(
            Text.from_markup(subtitle, justify="center"),
            title=title,
            border_style="blue",
        )

    def _render_progress(self) -> Panel:
        items = []
        if self.task_id is not None:
            items.append(self.progress)
        elif self.phase:
            items.append(Text.from_markup(f"[yellow]⏳ {self.phase}[/yellow]"))
        if self._extra_info:
            info_parts = []
            for k, v in self._extra_info.items():
                info_parts.append(f"[cyan]{k}[/cyan]: {v}")
            from rich.text import Text as RichText
            items.append(RichText.from_markup("  ".join(info_parts)))
        return Panel(Group(*items) if items else Text(""), title="Progress", border_style="green", padding=(0, 1))

    def _render_system(self) -> Panel:
        s = self.monitor.snapshot
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)

        # CPU bars (compact: 4 chars per core)
        cpu_str = ""
        for i, pct in enumerate(s.cpu_percent):
            if i == 4:
                cpu_str += "  "  # gap between efficiency and performance cores
            filled = int(pct / 100 * 4)
            bar = "█" * filled + "░" * (4 - filled)
            color = 'red' if pct > 75 else 'yellow' if pct > 50 else 'green'
            cpu_str += f"[{color}]{bar}[/] "

        # Temperature
        if s.has_cpu_temp and s.cpu_temp > 0:
            temp_str = f"CPU {self._temp_color(s.cpu_temp)}  GPU {self._temp_color(s.gpu_temp)}  SSD {self._temp_color(s.ssd_temp)}"
        else:
            temp_str = f"SSD {self._temp_color(s.ssd_temp)}  [dim](CPU/GPU temp not available on M1)[/]"

        # Memory + Disk (compact, one line)
        mem_color = "red" if s.mem_percent > 85 else "yellow" if s.mem_percent > 70 else "green"
        mem_str = f"[{mem_color}]{s.mem_used_gb:.1f}/{s.mem_total_gb:.0f}G ({s.mem_percent:.0f}%)[/]"
        disk_str = f"R {s.disk_read_mb:>4.0f} W {s.disk_write_mb:>4.0f} MB/s  Scratch {s.scratch_gb:.1f}G"

        table.add_row(f"{cpu_str}  avg {s.cpu_avg:.0f}%")
        table.add_row(temp_str)
        table.add_row(f"RAM {mem_str}  {disk_str}")

        return Panel(table, title="System Monitor", border_style="cyan", padding=(0, 1))

    @staticmethod
    def _temp_color(temp):
        if temp == 0:
            return "[dim]—°C[/]"
        if temp > 85:
            return f"[bold red]{temp:.1f}°C[/]"
        if temp > 70:
            return f"[yellow]{temp:.1f}°C[/]"
        return f"[green]{temp:.1f}°C[/]"

    def _render_logs(self) -> Panel:
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column(ratio=1)
        for msg in list(self.logs)[:6]:
            table.add_row(msg)
        return Panel(table, title="Log", border_style="dim", padding=(0, 1))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_connection():
    """Create a DuckDB connection with memory/spill settings tuned for
    the 8 GB Mac, writing spill to the external drive."""
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory = '{SCRATCH_DIR}'")
    con.execute(f"SET threads = {THREADS}")
    return con


def fmt_time(seconds):
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m{s}s"


def dir_size_gb(path):
    """Size of a directory tree in GB, excluding macOS AppleDouble (._*) files."""
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                if f.startswith("._"):
                    continue
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
    except Exception:
        pass
    return total / 1e9


def clean_apple_double(path):
    """Delete macOS AppleDouble (._*) files from a directory tree.
    These are created automatically on exFAT drives and crash DuckDB's
    read_parquet with "No magic bytes found". Returns count of remaining files."""
    # Use find with -delete. On exFAT with 690K+ files this can take minutes,
    # so allow up to 10 minutes.
    subprocess.run(
        ["find", path, "-name", "._*", "-type", "f", "-delete"],
        capture_output=True, text=True, timeout=600,
    )
    # Check if any remain
    remaining = subprocess.run(
        ["find", path, "-name", "._*", "-type", "f"],
        capture_output=True, text=True, timeout=120,
    )
    remaining_count = len(remaining.stdout.strip().splitlines()) if remaining.stdout.strip() else 0
    return remaining_count


def count_parquet_files(path):
    return len(list(Path(path).rglob("*.parquet")))


# ---------------------------------------------------------------------------
# Step 1: Partition raw data by (res, base_cell)
# ---------------------------------------------------------------------------
def step_partition(con, display, monitor):
    encoded = Path(ENCODED_DIR)
    if (encoded / "res=7").exists() and (encoded / "res=3").exists():
        display.log("[green]Encoded data already exists — skipping partitioning.[/]")
        display.set_info(note="skipped (exists)")
        return

    # Calculate expected output size from input (rough estimate — partitioned
    # output is typically ~1.4x the input due to per-file overhead + base_cell column)
    input_size_gb = sum(f.stat().st_size for f in Path(INPUT_DIR).glob("*.parquet")) / 1e9
    display.log(f"Input size: {input_size_gb:.1f} GB across {len(list(Path(INPUT_DIR).glob('*.parquet')))} files")
    display.set_phase("Partitioning by (res, base_cell)…")

    # Run COPY in a background thread; monitor output dir growth in foreground
    error_box = [None]
    done_event = threading.Event()

    def _partition():
        try:
            con.execute(f"""
                COPY (
                    SELECT
                        h3_cell,
                        id_no,
                        res,
                        (h3_cell >> {BASE_CELL_SHIFT}) & {BASE_CELL_MASK} AS base_cell
                    FROM read_parquet('{INPUT_GLOB}')
                    WHERE id_no IS NOT NULL
                ) TO '{ENCODED_DIR}' (
                    FORMAT parquet,
                    PARTITION_BY (res, base_cell),
                    OVERWRITE_OR_IGNORE true
                );
            """)
        except Exception as e:
            error_box[0] = e
        finally:
            done_event.set()

    display.log("Starting COPY (background)…")
    t0 = time.time()
    thread = threading.Thread(target=_partition, daemon=True)
    thread.start()

    # Monitor output directory growth
    while not done_event.is_set():
        current_gb = dir_size_gb(ENCODED_DIR)
        rate = monitor.snapshot.disk_write_mb
        display.set_info(
            written=f"{current_gb:.1f} GB",
            rate=f"{rate:.0f} MB/s",
        )
        time.sleep(2)

    thread.join(timeout=5)
    if error_box[0]:
        raise error_box[0]

    elapsed = time.time() - t0
    final_gb = dir_size_gb(ENCODED_DIR)
    display.log(f"[green]Partition COPY done in {fmt_time(elapsed)}. Output: {final_gb:.1f} GB[/]")

    # Sanity check
    display.set_phase("Verifying row counts…")
    n_res3 = con.execute(f"SELECT COUNT(*) FROM read_parquet('{ENCODED_DIR}/res=3/**/data_*.parquet')").fetchone()[0]
    n_res7 = con.execute(f"SELECT COUNT(*) FROM read_parquet('{ENCODED_DIR}/res=7/**/data_*.parquet')").fetchone()[0]
    display.log(f"res=3: {n_res3:,} rows    res=7: {n_res7:,} rows")
    display.set_info(rows_res3=f"{n_res3:,}", rows_res7=f"{n_res7:,}")


# ---------------------------------------------------------------------------
# Step 2: Aggregate res=3 (small dataset — one shot)
# ---------------------------------------------------------------------------
def validate_parquet(con, path, display):
    """Check if a parquet file is complete and readable.
    A truncated parquet (interrupted mid-write) will fail because the
    footer is at the end. If invalid, delete it so it gets reprocessed."""
    if not Path(path).exists():
        return False
    try:
        con.execute(f"SELECT COUNT(*) FROM read_parquet('{path}')").fetchone()
        return True
    except Exception:
        display.log(f"[yellow]{Path(path).name} is incomplete (interrupted?). Deleting for reprocessing.[/]")
        try:
            Path(path).unlink()
        except Exception:
            pass
        return False


def step_res3(con, display):
    out_path = Path(OUTPUT_DIR) / "h3_res3_species_global.parquet"
    if validate_parquet(con, out_path, display):
        display.log(f"[green]{out_path.name} validated — skipping.[/]")
        return

    display.set_phase("Aggregating res=3 (one shot)…")
    display.log("Aggregating res=3…")
    t0 = time.time()

    con.execute(f"""
        COPY (
            WITH pairs AS (
                SELECT h3_cell, id_no
                FROM read_parquet('{ENCODED_DIR}/res=3/**/data_*.parquet')
                GROUP BY h3_cell, id_no
            )
            SELECT h3_cell, list(id_no) AS species_ids
            FROM pairs
            GROUP BY h3_cell
        ) TO '{out_path}' (FORMAT parquet);
    """)

    elapsed = time.time() - t0
    n_cells = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path}')").fetchone()[0]
    display.log(f"[green]res=3 done in {fmt_time(elapsed)}. {n_cells:,} unique cells.[/]")
    display.set_info(cells=f"{n_cells:,}", elapsed=fmt_time(elapsed))


# ---------------------------------------------------------------------------
# Step 3: Aggregate res=7 (loop over base cells)
# ---------------------------------------------------------------------------
def validate_latest_output(con, out_dir, display):
    """Check if the most recently written parquet file is complete.
    A truncated parquet (interrupted mid-write) will fail to read because
    the footer is at the end. If invalid, delete it so it gets reprocessed."""
    parts = sorted(out_dir.glob("base_*.parquet"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not parts:
        return
    latest = parts[0]
    if validate_parquet(con, latest, display):
        display.log(f"[green]Latest file {latest.name} validated OK.[/]")


def step_res7(con, display, monitor, base_cell=None, resume=False):
    out_dir = Path(OUTPUT_DIR) / "res7_parts"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Always validate the most recently written file — if it was
    # interrupted mid-write (truncated parquet), delete it so it gets reprocessed.
    validate_latest_output(con, out_dir, display)

    # Determine which base cells to process
    if base_cell is not None:
        cells_to_process = [base_cell]
    else:
        encoded_res7 = Path(ENCODED_DIR) / "res=7"
        if not encoded_res7.exists():
            display.log("[red]ERROR: Encoded res=7 data not found. Run --step partition first.[/]")
            return
        cells_to_process = sorted(
            int(d.name.split("=")[1]) for d in encoded_res7.iterdir()
            if d.name.startswith("base_cell=")
        )

    # Always skip base cells with existing valid output (regardless of --resume)
    total = len(cells_to_process)
    skipped = sum(
        1 for bc in cells_to_process
        if (out_dir / f"base_{bc}.parquet").exists()
    )
    to_do = total - skipped
    display.log(f"Base cells: {total} total, {skipped} already done, {to_do} to process")
    display.init_progress(total, "Base cells")
    if skipped:
        display.update_progress(advance=skipped)

    t_start = time.time()
    done = 0

    for bc in cells_to_process:
        out_file = out_dir / f"base_{bc}.parquet"

        if out_file.exists():
            continue

        base_dir = Path(f"{ENCODED_DIR}/res=7/base_cell={bc}")
        if not base_dir.exists():
            display.log(f"[dim]base {bc}: no data — skipped[/]")
            display.update_progress(advance=1)
            continue

        display.set_phase(f"Base cell {bc} ({done + 1}/{to_do})")
        t0 = time.time()

        # List all parquet files in this base cell
        all_files = sorted(base_dir.glob("data_*.parquet"))
        n_files = len(all_files)
        total_size = sum(f.stat().st_size for f in all_files) / 1e9

        # Hash-partition ALL base cells by h3_cell into N shards.
        # Each shard fully aggregates its 1/N of cells, then we concatenate.
        N_SHARDS = 16

        if total_size < 0.5 and n_files <= 100:
            # Very small base cell — one shot
            con.execute(f"""
                COPY (
                    WITH pairs AS (
                        SELECT h3_cell, id_no
                        FROM read_parquet('{base_dir}/**/data_*.parquet')
                        GROUP BY h3_cell, id_no
                    )
                    SELECT h3_cell, list(id_no) AS species_ids
                    FROM pairs
                    GROUP BY h3_cell
                ) TO '{out_file}' (FORMAT parquet);
            """)
        else:
            # Larger base cell — partition into shards in ONE pass, then aggregate each.
            # Phase 1: single-pass split by h3_cell % N (no GROUP BY, minimal RAM)
            # Phase 2: aggregate each small shard independently
            # Phase 3: concatenate aggregated shards (no GROUP BY)
            display.log(f"  base {bc}: {n_files} files, {total_size:.2f} GB — {N_SHARDS} shards")
            temp_dir = out_dir / f"_temp_base_{bc}"
            temp_dir.mkdir(exist_ok=True)

            # Phase 1: single-pass partition (just split rows, no aggregation)
            display.set_phase(f"Base cell {bc} partitioning ({done + 1}/{to_do})")
            con.execute(f"""
                COPY (
                    SELECT h3_cell, id_no, CAST(hash(h3_cell) % {N_SHARDS} AS INTEGER) AS shard
                    FROM read_parquet('{base_dir}/**/data_*.parquet')
                ) TO '{temp_dir}/part' (
                    FORMAT parquet,
                    PARTITION_BY (shard),
                    OVERWRITE_OR_IGNORE true
                );
            """)

            # Phase 2: aggregate each shard (now small files, ~28M rows each)
            for shard in range(N_SHARDS):
                con.execute(f"""
                    COPY (
                        WITH pairs AS (
                            SELECT h3_cell, id_no
                            FROM read_parquet('{temp_dir}/part/shard={shard}/**/data_*.parquet')
                            GROUP BY h3_cell, id_no
                        )
                        SELECT h3_cell, list(id_no) AS species_ids
                        FROM pairs
                        GROUP BY h3_cell
                    ) TO '{temp_dir}/agg_{shard:02d}.parquet' (FORMAT parquet);
                """)
                display.set_phase(f"Base cell {bc} agg shard {shard+1}/{N_SHARDS} ({done + 1}/{to_do})")

            # Phase 3: concatenate — no GROUP BY needed (disjoint h3_cells)
            display.set_phase(f"Base cell {bc} concatenating ({done + 1}/{to_do})")
            con.execute(f"""
                COPY (
                    SELECT h3_cell, species_ids
                    FROM read_parquet('{temp_dir}/agg_*.parquet')
                ) TO '{out_file}' (FORMAT parquet);
            """)

            # Clean up temp files
            shutil.rmtree(temp_dir, ignore_errors=True)

        elapsed = time.time() - t0
        done += 1
        display.update_progress(advance=1)

        n_cells = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_file}')").fetchone()[0]
        n_species = con.execute(f"SELECT SUM(len(species_ids)) FROM read_parquet('{out_file}')").fetchone()[0]
        display.log(
            f"base {bc:>3}: {n_cells:>10,} cells  {n_species:>12,} spp  {fmt_time(elapsed)}"
        )

        # ETA
        elapsed_total = time.time() - t_start
        if done > 0:
            avg_per_cell = elapsed_total / done
            remaining = to_do - done
            eta = avg_per_cell * remaining
            display.set_info(
                done=f"{done}/{to_do}",
                avg=f"{avg_per_cell:.1f}s/cell",
                eta=fmt_time(eta),
                elapsed=fmt_time(elapsed_total),
            )

    display.log(f"[green]res=7 done. Processed {done}, skipped {skipped}.[/]")


# ---------------------------------------------------------------------------
# Step 4: Combine res=7 per-base-cell files into a single parquet
# ---------------------------------------------------------------------------
def step_combine_res7(con, display, parts_dir=None, out_file=None,
                       out_name="h3_res7_species_global.parquet"):
    if parts_dir is None:
        parts_dir = Path(OUTPUT_DIR) / "res7_parts"
    if out_file is None:
        out_file = Path(OUTPUT_DIR) / out_name

    if validate_parquet(con, out_file, display):
        display.log(f"[green]{out_file.name} validated — skipping.[/]")
        return

    parts = sorted(Path(parts_dir).glob("base_*.parquet"))
    if not parts:
        display.log(f"[red]ERROR: No parts found in {parts_dir}.[/]")
        return

    display.set_phase(f"Combining {len(parts)} base-cell files…")
    display.log(f"Combining {len(parts)} base-cell files…")
    t0 = time.time()

    parts_glob = f"{parts_dir}/base_*.parquet"
    con.execute(f"""
        COPY (
            SELECT h3_cell, species_ids
            FROM read_parquet('{parts_glob}')
            ORDER BY h3_cell
        ) TO '{out_file}' (FORMAT parquet);
    """)

    elapsed = time.time() - t0
    n_cells = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_file}')").fetchone()[0]
    display.log(f"[green]Combine done in {fmt_time(elapsed)}. {n_cells:,} unique cells.[/]")
    display.set_info(cells=f"{n_cells:,}", elapsed=fmt_time(elapsed))


# ---------------------------------------------------------------------------
# Step 5: Verify results
# ---------------------------------------------------------------------------
def step_verify(con, display):
    display.set_phase("Verifying…")

    for res, fname in [("3", "h3_res3_species_global.parquet"),
                        ("7", "h3_res7_species_global.parquet")]:
        path = Path(OUTPUT_DIR) / fname
        if not path.exists():
            display.log(f"[red]res={res}: file not found ({fname})[/]")
            continue

        stats = con.execute(f"""
            SELECT
                COUNT(*) AS n_cells,
                SUM(len(species_ids)) AS total_species_entries,
                MAX(len(species_ids)) AS max_species_per_cell,
                AVG(len(species_ids)) AS avg_species_per_cell
            FROM read_parquet('{path}')
        """).fetchone()

        n_cells, total_entries, max_spp, avg_spp = stats
        display.log(
            f"res={res}: {n_cells:>12,} cells, "
            f"{total_entries:>14,} entries, "
            f"max {max_spp:>6,}/cell, avg {avg_spp:>6.1f}/cell"
        )

        # Duplicate check — only for res3 (small). res7 is too large to
        # unnest + GROUP BY in memory; skip (pipeline guarantees no dups
        # by construction via GROUP BY h3_cell, id_no).
        if res == "3":
            dups = con.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT h3_cell, sid, COUNT(*) AS cnt
                    FROM read_parquet('{path}'), unnest(species_ids) AS t(sid)
                    GROUP BY h3_cell, sid
                    HAVING cnt > 1
                )
            """).fetchone()[0]
            color = "green" if dups == 0 else "red"
            display.log(f"  duplicates: [{color}]{dups}[/] (should be 0)")
        else:
            display.log("  duplicates: skipped (res7 too large — guaranteed by construction)")

    # Disk usage
    total_size = sum(f.stat().st_size for f in Path(OUTPUT_DIR).rglob("*.parquet"))
    display.log(f"Total output size: {total_size / 1e9:.2f} GB")
    display.set_info(output_size=f"{total_size / 1e9:.2f} GB")


# ---------------------------------------------------------------------------
# Step 6: Expand res3 cells to res7 children — creates a mapping table
#         (res3_cell, res7_child) partitioned by base_cell.
#         res3 species are present throughout the cell, so every res7 child
#         inherits the full res3 species list.
# ---------------------------------------------------------------------------
def step_expand_res3(con, display):
    import h3
    import pyarrow as pa
    import pyarrow.parquet as pq

    mapping_dir = Path(OUTPUT_DIR) / "res3_to_res7_mapping"

    # Check if already done (look for partitioned data, exclude AppleDouble)
    if mapping_dir.exists() and any(mapping_dir.rglob("data_*.parquet")):
        display.log("[green]res3→res7 mapping already exists — skipping.[/]")
        return

    mapping_dir.mkdir(parents=True, exist_ok=True)

    res3_path = Path(OUTPUT_DIR) / "h3_res3_species_global.parquet"
    if not res3_path.exists():
        display.log("[red]ERROR: h3_res3_species_global.parquet not found. Run --step res3 first.[/]")
        return

    # Read res3 cells (stored as UBIGINT in parquet, h3 library needs hex strings)
    res3_cells = [r[0] for r in con.execute(
        f"SELECT h3_cell FROM read_parquet('{res3_path}')"
    ).fetchall()]
    n_res3 = len(res3_cells)

    expected_children = n_res3 * 2401  # 7^4 — approximate (pentagons have 6^k)
    display.log(f"Expanding {n_res3:,} res3 cells → ~{expected_children:,} res7 children…")
    display.init_progress(n_res3, "res3 cells")

    # Write mapping to a temp parquet in batches, then re-partition by base_cell
    temp_path = mapping_dir / "_temp_mapping.parquet"
    schema = pa.schema([
        ('res3_cell', pa.uint64()),
        ('res7_child', pa.uint64()),
    ])

    batch_size = 200  # 200 cells → ~480K rows per batch
    t0 = time.time()

    writer = pq.ParquetWriter(str(temp_path), schema, compression='snappy')

    for i in range(0, n_res3, batch_size):
        batch = res3_cells[i:i + batch_size]
        parents = []
        children = []
        for cell_int in batch:
            cell_hex = f'{cell_int:015x}'
            kids = h3.cell_to_children(cell_hex, 7)
            parents.extend([cell_int] * len(kids))
            children.extend(int(k, 16) for k in kids)

        table = pa.table({
            'res3_cell': pa.array(parents, type=pa.uint64()),
            'res7_child': pa.array(children, type=pa.uint64()),
        }, schema=schema)
        writer.write_table(table)
        display.update_progress(advance=len(batch))

    writer.close()

    display.set_phase("Partitioning mapping by base_cell…")
    display.log("Partitioning mapping by base_cell…")

    # Re-partition by base_cell using DuckDB
    con.execute(f"""
        COPY (
            SELECT
                res3_cell,
                res7_child,
                (res7_child >> {BASE_CELL_SHIFT}) & {BASE_CELL_MASK} AS base_cell
            FROM read_parquet('{temp_path}')
        ) TO '{mapping_dir}' (
            FORMAT parquet,
            PARTITION_BY (base_cell),
            OVERWRITE_OR_IGNORE true
        );
    """)

    # Clean up temp file
    temp_path.unlink()

    # Clean up any AppleDouble files
    clean_apple_double(mapping_dir)

    elapsed = time.time() - t0
    # Count actual rows
    n_rows = con.execute(f"""
        SELECT COUNT(*) FROM read_parquet('{mapping_dir}/**/data_*.parquet')
    """).fetchone()[0]
    display.log(f"[green]Expand done in {fmt_time(elapsed)}. {n_rows:,} mapping rows.[/]")
    display.set_info(rows=f"{n_rows:,}", elapsed=fmt_time(elapsed))


# ---------------------------------------------------------------------------
# Step 7: Merge expanded res3→res7 cells with existing res7 cells.
#         For each base cell:
#           - Expand: JOIN mapping with res3 to get (res7_child, species_ids)
#           - UNION ALL with existing res7 (h3_cell, species_ids)
#           - GROUP BY h3_cell, flatten + list_distinct to merge species lists
#         Output: res7_merged_parts/base_{bc}.parquet
# ---------------------------------------------------------------------------
def step_merge_res7(con, display, monitor, base_cell=None):
    merged_dir = Path(OUTPUT_DIR) / "res7_merged_parts"
    merged_dir.mkdir(parents=True, exist_ok=True)

    mapping_dir = Path(OUTPUT_DIR) / "res3_to_res7_mapping"
    res3_path = Path(OUTPUT_DIR) / "h3_res3_species_global.parquet"
    existing_dir = Path(OUTPUT_DIR) / "res7_parts"

    if not mapping_dir.exists():
        display.log("[red]ERROR: res3→res7 mapping not found. Run --step expand first.[/]")
        return

    # Validate latest output if resuming
    validate_latest_output(con, merged_dir, display)

    # Determine which base cells to process
    if base_cell is not None:
        cells_to_process = [base_cell]
    else:
        cells_to_process = sorted(
            int(d.name.split("=")[1]) for d in mapping_dir.iterdir()
            if d.name.startswith("base_cell=")
        )

    total = len(cells_to_process)
    skipped = sum(
        1 for bc in cells_to_process
        if (merged_dir / f"base_{bc}.parquet").exists()
    )
    to_do = total - skipped
    display.log(f"Base cells: {total} total, {skipped} already done, {to_do} to merge")
    display.init_progress(total, "Base cells")
    if skipped:
        display.update_progress(advance=skipped)

    t_start = time.time()
    done = 0

    for bc in cells_to_process:
        out_file = merged_dir / f"base_{bc}.parquet"
        if out_file.exists():
            continue

        mapping_glob = f"{mapping_dir}/base_cell={bc}/**/data_*.parquet"
        existing_file = existing_dir / f"base_{bc}.parquet"

        display.set_phase(f"Base cell {bc} ({done + 1}/{to_do})")
        t0 = time.time()

        existing_clause = ""
        if existing_file.exists():
            existing_clause = f"""
                UNION ALL
                SELECT h3_cell, species_ids
                FROM read_parquet('{existing_file}')
            """

        # Always hash-partition into shards to avoid OOM on flatten(list())
        N_SHARDS = 16
        temp_dir = merged_dir / f"_temp_base_{bc}"
        temp_dir.mkdir(exist_ok=True)

        # Phase 1: split all rows by hash(h3_cell) % N
        display.set_phase(f"Base cell {bc} partitioning ({done + 1}/{to_do})")
        con.execute(f"""
            COPY (
                WITH expanded AS (
                    SELECT m.res7_child AS h3_cell, r.species_ids
                    FROM read_parquet('{mapping_glob}') m
                    JOIN read_parquet('{res3_path}') r ON m.res3_cell = r.h3_cell
                ),
                all_rows AS (
                    SELECT h3_cell, species_ids FROM expanded
                    {existing_clause}
                )
                SELECT
                    h3_cell,
                    species_ids,
                    CAST(hash(h3_cell) % {N_SHARDS} AS INTEGER) AS shard
                FROM all_rows
                WHERE species_ids IS NOT NULL
            ) TO '{temp_dir}/part' (
                FORMAT parquet,
                PARTITION_BY (shard),
                OVERWRITE_OR_IGNORE true
            );
        """)

        # Phase 2: aggregate each shard
        for shard in range(N_SHARDS):
            con.execute(f"""
                COPY (
                    SELECT
                        h3_cell,
                        list_sort(list_distinct(flatten(list(species_ids)))) AS species_ids
                    FROM read_parquet('{temp_dir}/part/shard={shard}/**/data_*.parquet')
                    GROUP BY h3_cell
                ) TO '{temp_dir}/agg_{shard:02d}.parquet' (FORMAT parquet);
            """)
            display.set_phase(f"Base cell {bc} agg shard {shard+1}/{N_SHARDS} ({done + 1}/{to_do})")

        # Phase 3: concatenate (disjoint h3_cells)
        display.set_phase(f"Base cell {bc} concatenating ({done + 1}/{to_do})")
        con.execute(f"""
            COPY (
                SELECT h3_cell, species_ids
                FROM read_parquet('{temp_dir}/agg_*.parquet')
            ) TO '{out_file}' (FORMAT parquet);
        """)

        shutil.rmtree(temp_dir, ignore_errors=True)

        elapsed = time.time() - t0
        done += 1
        display.update_progress(advance=1)

        n_cells = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_file}')").fetchone()[0]
        n_species = con.execute(f"SELECT SUM(len(species_ids)) FROM read_parquet('{out_file}')").fetchone()[0]
        display.log(
            f"base {bc:>3}: {n_cells:>10,} cells  {n_species:>12,} spp  {fmt_time(elapsed)}"
        )

        elapsed_total = time.time() - t_start
        if done > 0:
            avg = elapsed_total / done
            remaining = to_do - done
            eta = avg * remaining
            display.set_info(
                done=f"{done}/{to_do}",
                avg=f"{avg:.1f}s/cell",
                eta=fmt_time(eta),
                elapsed=fmt_time(elapsed_total),
            )

    display.log(f"[green]Merge done. Processed {done}, skipped {skipped}.[/]")


# ---------------------------------------------------------------------------
# Step 8: Rollup merged res7 → res3 parent cells.
#         Uses the res3→res7 mapping in reverse: JOIN mapping with merged
#         res7 parts on res7_child = h3_cell, GROUP BY res3_cell, flatten +
#         list_distinct species_ids.
#         Since res3 cells and their res7 children share the same base cell,
#         each res3 cell is complete within one base cell partition.
#         Output: res3_merged_parts/base_{bc}.parquet
# ---------------------------------------------------------------------------
def step_rollup_res3(con, display, monitor, base_cell=None):
    out_dir = Path(OUTPUT_DIR) / "res3_merged_parts"
    out_dir.mkdir(parents=True, exist_ok=True)

    mapping_dir = Path(OUTPUT_DIR) / "res3_to_res7_mapping"
    merged_dir = Path(OUTPUT_DIR) / "res7_merged_parts"

    if not mapping_dir.exists():
        display.log("[red]ERROR: res3→res7 mapping not found. Run --step expand first.[/]")
        return
    if not merged_dir.exists() or not any(merged_dir.glob("base_*.parquet")):
        display.log("[red]ERROR: res7_merged_parts not found. Run --step merge first.[/]")
        return

    validate_latest_output(con, out_dir, display)

    if base_cell is not None:
        cells_to_process = [base_cell]
    else:
        cells_to_process = sorted(
            int(d.name.split("=")[1]) for d in mapping_dir.iterdir()
            if d.name.startswith("base_cell=")
        )

    total = len(cells_to_process)
    skipped = sum(
        1 for bc in cells_to_process
        if (out_dir / f"base_{bc}.parquet").exists()
    )
    to_do = total - skipped
    display.log(f"Base cells: {total} total, {skipped} already done, {to_do} to rollup")
    display.init_progress(total, "Base cells")
    if skipped:
        display.update_progress(advance=skipped)

    t_start = time.time()
    done = 0

    for bc in cells_to_process:
        out_file = out_dir / f"base_{bc}.parquet"
        if out_file.exists():
            continue

        mapping_glob = f"{mapping_dir}/base_cell={bc}/**/data_*.parquet"
        merged_file = merged_dir / f"base_{bc}.parquet"
        if not merged_file.exists():
            display.log(f"[dim]base {bc}: no merged res7 — skipped[/]")
            display.update_progress(advance=1)
            continue

        display.set_phase(f"Base cell {bc} ({done + 1}/{to_do})")
        t0 = time.time()

        # Always hash-partition into shards to avoid OOM on flatten(list())
        N_SHARDS = 16
        temp_dir = out_dir / f"_temp_base_{bc}"
        temp_dir.mkdir(exist_ok=True)

        # Phase 1: split by hash(res3_cell) % N
        display.set_phase(f"Base cell {bc} partitioning ({done + 1}/{to_do})")
        con.execute(f"""
            COPY (
                SELECT
                    m.res3_cell AS h3_cell,
                    r.species_ids,
                    CAST(hash(m.res3_cell) % {N_SHARDS} AS INTEGER) AS shard
                FROM read_parquet('{mapping_glob}') m
                JOIN read_parquet('{merged_file}') r ON m.res7_child = r.h3_cell
            ) TO '{temp_dir}/part' (
                FORMAT parquet,
                PARTITION_BY (shard),
                OVERWRITE_OR_IGNORE true
            );
        """)

        # Phase 2: aggregate each shard
        for shard in range(N_SHARDS):
            con.execute(f"""
                COPY (
                    SELECT
                        h3_cell,
                        list_sort(list_distinct(flatten(list(species_ids)))) AS species_ids
                    FROM read_parquet('{temp_dir}/part/shard={shard}/**/data_*.parquet')
                    GROUP BY h3_cell
                ) TO '{temp_dir}/agg_{shard:02d}.parquet' (FORMAT parquet);
            """)
            display.set_phase(f"Base cell {bc} agg shard {shard+1}/{N_SHARDS} ({done + 1}/{to_do})")

        # Phase 3: concatenate (disjoint res3_cells)
        display.set_phase(f"Base cell {bc} concatenating ({done + 1}/{to_do})")
        con.execute(f"""
            COPY (
                SELECT h3_cell, species_ids
                FROM read_parquet('{temp_dir}/agg_*.parquet')
            ) TO '{out_file}' (FORMAT parquet);
        """)

        shutil.rmtree(temp_dir, ignore_errors=True)

        elapsed = time.time() - t0
        done += 1
        display.update_progress(advance=1)

        n_cells = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_file}')").fetchone()[0]
        n_species = con.execute(f"SELECT SUM(len(species_ids)) FROM read_parquet('{out_file}')").fetchone()[0]
        display.log(
            f"base {bc:>3}: {n_cells:>10,} res3 cells  {n_species:>12,} spp  {fmt_time(elapsed)}"
        )

        elapsed_total = time.time() - t_start
        if done > 0:
            avg = elapsed_total / done
            remaining = to_do - done
            eta = avg * remaining
            display.set_info(
                done=f"{done}/{to_do}",
                avg=f"{avg:.1f}s/cell",
                eta=fmt_time(eta),
                elapsed=fmt_time(elapsed_total),
            )

    display.log(f"[green]Rollup done. Processed {done}, skipped {skipped}.[/]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Build global H3 cell -> species aggregation from h3_pairs."
    )
    parser.add_argument(
        "--step",
        choices=["all", "partition", "res3", "res7", "combine", "verify",
                  "expand", "merge", "combine_merged",
                  "rollup", "combine_rollup"],
        default="all",
        help="Which step to run (default: all)."
    )
    parser.add_argument(
        "--base-cell",
        type=int,
        default=None,
        help="Process only this base cell (0-121). Use with --step res7, --step merge, or --step rollup."
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip base cells whose output already exists. Use with --step res7."
    )
    args = parser.parse_args()

    # Create directories
    Path(SCRATCH_DIR).mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    # Clean any stale scratch files
    for f in Path(SCRATCH_DIR).glob("*.tmp"):
        try:
            f.unlink()
        except Exception:
            pass

    # Try to get sudo access for CPU/GPU temperature monitoring.
    # sudo -v prompts for password interactively (before rich display takes over).
    sudo_ok = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    if not sudo_ok:
        print("CPU/GPU temperature needs sudo. Enter password (or press Ctrl+C to skip):", flush=True)
        try:
            subprocess.run(["sudo", "-v"], check=True)
        except (subprocess.CalledProcessError, KeyboardInterrupt):
            print("Skipping CPU/GPU temp (SSD temp still works).")

    monitor = SystemMonitor(interval=2.0)
    monitor.start()
    display = Display(monitor)

    con = make_connection()

    steps = {
        "partition": lambda: step_partition(con, display, monitor),
        "res3":      lambda: step_res3(con, display),
        "res7":      lambda: step_res7(con, display, monitor, args.base_cell, args.resume),
        "combine":   lambda: step_combine_res7(con, display),
        "verify":    lambda: step_verify(con, display),
        "expand":    lambda: step_expand_res3(con, display),
        "merge":     lambda: step_merge_res7(con, display, monitor, args.base_cell),
        "combine_merged": lambda: step_combine_res7(
            con, display,
            parts_dir=Path(OUTPUT_DIR) / "res7_merged_parts",
            out_file=Path(OUTPUT_DIR) / "h3_res7_species_global_merged.parquet",
            out_name="h3_res7_species_global_merged.parquet",
        ),
        "rollup": lambda: step_rollup_res3(con, display, monitor, args.base_cell),
        "combine_rollup": lambda: step_combine_res7(
            con, display,
            parts_dir=Path(OUTPUT_DIR) / "res3_merged_parts",
            out_file=Path(OUTPUT_DIR) / "h3_res3_species_global_merged.parquet",
            out_name="h3_res3_species_global_merged.parquet",
        ),
    }

    if args.step == "all":
        order = ["partition", "res3", "res7", "combine", "verify",
                  "expand", "merge", "combine_merged",
                  "rollup", "combine_rollup"]
    else:
        order = [args.step]

    def run_steps():
        for i, step_name in enumerate(order, 1):
            display.set_step(i, len(order), step_name.replace("_", " ").title())
            t0 = time.time()
            try:
                steps[step_name]()
            except Exception as e:
                display.log(f"[red]ERROR in {step_name}: {e}[/]")
                raise
            display.set_info(step_elapsed=fmt_time(time.time() - t0))

    with Live(display, console=console, refresh_per_second=2, screen=True):
        try:
            run_steps()
        finally:
            monitor.stop()
            con.close()

    # Print final summary after Live exits
    console.print("\n[bold green]Pipeline complete.[/bold green]")
    for msg in reversed(list(display.logs)):
        console.print(msg)


if __name__ == "__main__":
    main()
