"""Rich dashboard shared by benchmark and production pipeline runners."""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ark_pipeline.runtime.benchmark_estimates import duration
from ark_pipeline.runtime.checkpoints import read_checkpoint
from ark_pipeline.runtime.forecasts import Forecast
from ark_pipeline.runtime.progress import EventReader
from ark_pipeline.runtime.provenance import atomic_json

LABELS = {"acquisition": "Acquire sources", "source_scan": "Source I/O & census", "sample_setup": "Stratified sample",
          "crosswalk": "Species matching", "pairs": "Spatial sources → H3", "lists": "Deduplicate & group",
          "boundaries": "Boundary preparation", "metadata": "Species metadata", "coarse_db": "Coarse database",
          "coarse_cache": "Coarse metrics & map", "fine_metrics": "Fine metrics", "prepared_inputs": "Reconcile inputs", "tiles": "Compile map tiles"}
CYAN, MUTED, GREEN = "#69d2e7", "#8796aa", "#91d7a3"


def spark(values):
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(7, max(0, int(value / 100 * 7)))] for value in values)


class Dashboard:
    def __init__(self, names, output: Path, resources: dict, *, prior=None, note="", mode="benchmark", ui="auto", console=None, identity=None):
        self.names = list(names)
        self.output, self.resources = output, resources
        self.console = console or Console()
        self.enabled = ui == "rich" or (ui == "auto" and self.console.is_terminal)
        self.forecast = Forecast(prior, resources, mode)
        self.mode, self.note = mode, note
        if prior and prior.get("warnings"):
            self.note += " · provisional sample; see benchmark report"
        if prior and prior["resources"] != resources:
            self.note += " · worker scaling assumed until measured"
        self.reader = EventReader(output / "progress.jsonl")
        self.states = {name: {"status": "pending", "phase": "Waiting"} for name in names}
        self.tasks = {}
        self.logs = deque(maxlen=100)
        self.history = deque(maxlen=30)
        self.started = time.time()
        self.status = "running"
        self.stats = {"cpu": None, "ram_percent": None, "ram_used": None, "ram_total": None, "rss": None, "cores": None}
        self.processes = {}
        self.last_monitor = 0
        self.live = None
        self.global_estimate = prior.get("estimate", {}).get("total_seconds") if prior else None
        self.identity = identity
        self.last_save = 0
        self.lock = None
        self.restored = False
        self.restarted = set()

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = (self.output / ".run.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise ValueError(f"This pipeline run is already active: {self.output}") from None
        if self.identity is not None:
            saved = read_checkpoint(self.output, self.identity)
            if saved:
                now = time.time()
                self.states = saved["states"]
                self.forecast = Forecast.restore(saved["forecast"])
                shift = now - saved["saved_at"]
                self.forecast.started = {k: v + shift for k, v in self.forecast.started.items()}
                for task in [*self.forecast.active.values(), *self.forecast.active_partitions.values()]:
                    task["time"] += shift
                self.forecast.work_history = {k: [(t + shift, value) for t, value in rows]
                                              for k, rows in self.forecast.work_history.items()}
                for state in self.states.values():
                    if "started" in state:
                        state["started"] += shift
                self.started = now - saved["elapsed"]
                self.global_estimate = saved["global_estimate"]
                self.logs.extend((timestamp, message) for timestamp, message in saved["logs"])
                self.reader.offset = saved["event_offset"]
                self.reader.pending = bytes.fromhex(saved["event_pending"])
                for event in self.reader.read():
                    self.accept(event)
                self.restored = True
                self.logs.append((now, "Resumed saved progress · completed outputs will be validated"))
        self.save(force=True)
        if self.enabled:
            self.live = Live(self.render(), console=self.console, auto_refresh=False, screen=self.console.is_terminal, vertical_overflow="crop")
            self.live.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.lock is None:
            return
        if exc_type:
            self.status = "interrupted" if exc_type is KeyboardInterrupt else "failed"
        if self.status in {"interrupted", "failed", "action-required"}:
            for name, state in self.states.items():
                if state["status"] == "running":
                    self.accept({"kind": "stage_end", "stage": name, "time": time.time(), "status": self.status,
                                 "elapsed": time.time() - state.get("started", time.time())})
        try:
            self.tick(force=True)
        finally:
            try:
                if self.live:
                    self.live.stop()
            finally:
                if self.lock:
                    self.lock.close()
                    self.lock = None

    def save(self, force=False):
        if self.identity is None or (not force and time.monotonic() - self.last_save < 1):
            return
        now = time.time()
        atomic_json(self.output / "dashboard-state.json", {
            "schema_version": 1, "identity": self.identity, "saved_at": now, "status": self.status,
            "elapsed": now - self.started, "states": self.states, "forecast": self.forecast.snapshot(),
            "global_estimate": self.global_estimate, "logs": list(self.logs),
            "event_offset": self.reader.offset, "event_pending": self.reader.pending.hex(),
        })
        self.last_save = time.monotonic()

    def begin_stage(self, name):
        if name not in self.states:
            return
        state = self.states[name]
        if self.restored and name not in self.restarted and state["status"] != "pending":
            now = time.time()
            state["previous_elapsed"] = state.get("elapsed", now - state.get("started", now))
            state.pop("elapsed", None)
            state.pop("work", None)
            state.update(status="running", phase="Revalidating saved outputs", started=now)
            self.forecast.restart_stage(name, now)
            self.tasks = {k: v for k, v in self.tasks.items() if v["stage"] != name}
            self.restarted.add(name)
        self.accept({"kind": "stage_start", "stage": name, "time": time.time()})

    def accept(self, event):
        name, kind = event.get("stage"), event.get("kind")
        if name not in self.states:
            return
        self.forecast.accept(event)
        state = self.states[name]
        if kind == "stage_start":
            if self.restored and name not in self.restarted and state["status"] != "pending":
                self.begin_stage(name)
                return
            self.restarted.add(name)
            if state["status"] == "pending":
                state.update(status="running", started=event["time"], phase="Working · see activity")
                self.logs.append((event["time"], f"{LABELS.get(name, name)} started"))
        elif kind == "stage_end":
            state.update(status=event["status"], elapsed=event.get("elapsed", 0) + state.get("previous_elapsed", 0), phase=event.get("message", "Complete"))
            self.tasks = {k: v for k, v in self.tasks.items() if v["stage"] != name}
            self.logs.append((event["time"], f"{LABELS.get(name, name)} · {event['status']}"))
        elif kind in {"detail", "work", "geometry_start"}:
            if state["status"] == "pending":
                self.accept({"stage": name, "kind": "stage_start", "time": event["time"]})
            task = event.get("task", f"main:{event.get('pid', 0)}")
            if kind == "work" and event.get("overall"):
                state["work"] = event
                state["phase"] = event.get("phase", state["phase"])
            else:
                self.tasks[(name, task)] = event
        elif kind == "phase":
            state.pop("work", None)
            state["phase"] = event["phase"]
        elif kind in {"geometry_done", "task_end"}:
            self.tasks.pop((name, event.get("task")), None)
        elif kind == "message":
            self.logs.append((event["time"], event["message"]))

    def monitor(self, pid=None):
        if time.monotonic() - self.last_monitor < 0.5:
            return
        self.last_monitor = time.monotonic()
        try:
            self.stats["cpu"] = psutil.cpu_percent(interval=None)
            self.history.append(self.stats["cpu"])
        except (OSError, psutil.Error):
            pass
        try:
            memory = psutil.virtual_memory()
            # On macOS psutil.used and psutil.percent account for inactive memory
            # differently. Show one consistent available-memory calculation.
            used = memory.total - memory.available
            self.stats.update(ram_percent=100 * used / memory.total, ram_used=used, ram_total=memory.total)
        except (OSError, psutil.Error):
            pass
        if not pid:
            return
        try:
            parent = psutil.Process(pid)
            rss = cores = 0.0
            current = [parent, *parent.children(recursive=True)]
            keys = set()
            for child in current:
                with contextlib.suppress(psutil.Error, OSError):
                    key = (child.pid, child.create_time())
                    keys.add(key)
                    process = self.processes.setdefault(key, child)
                    rss += process.memory_info().rss
                    cores += process.cpu_percent(interval=None) / 100
            self.processes = {k: v for k, v in self.processes.items() if k in keys}
            self.stats.update(rss=rss, cores=cores)
        except (OSError, psutil.Error):
            self.stats.update(rss=None, cores=None)

    def tick(self, pid=None, force=False):
        for event in self.reader.read():
            self.accept(event)
        self.monitor(pid)
        self.save(force=force)
        if self.live:
            self.live.update(self.render(), refresh=True)

    def render(self, width=None, now=None):
        now = now or time.time()
        width = width or self.console.width
        height = self.console.height
        narrow = width < 100
        compact = height < (44 if narrow else 36)
        options = self.console.options.update(width=width, height=None)

        def lines(renderable):
            return len(self.console.render_lines(renderable, options, pad=False))
        elapsed = now - self.started
        remaining, _ = self.forecast.total(self.names, now)
        unknown = sum(self.forecast.remaining(name, now) is None for name in self.names)
        remaining_label = (duration(remaining) if remaining is not None else
                           f"Unknown · {unknown} stage{'s' if unknown != 1 else ''} unestimated")
        heading = Text("  A R K  /  DATA LAB", style=f"bold {CYAN}")
        heading.append(f"    {self.mode.upper()}  ·  {self.status.upper()}", style=MUTED)
        header = Panel(heading, border_style=CYAN, padding=(0, 1))
        forecast = Table.grid(expand=True, padding=(0, 1))
        forecast.add_column(style=MUTED)
        forecast.add_column(justify="right", style="bold white")
        forecast.add_row("Elapsed", duration(elapsed))
        forecast.add_row("Run remaining", remaining_label)
        finish = (datetime.fromtimestamp(now) + timedelta(seconds=remaining)).strftime("%H:%M:%S") if remaining is not None else "—"
        forecast.add_row("Expected finish", finish)
        if self.mode == "benchmark":
            forecast.add_row("Full-build projection", duration(self.global_estimate))
        machine = Table.grid(expand=True, padding=(0, 1))
        machine.add_column(style=MUTED)
        machine.add_column(justify="right")
        cpu = self.stats["cpu"]
        machine.add_row("System CPU", Text("Unavailable" if cpu is None else f"{cpu:5.1f}%  {spark(self.history)}", style=CYAN))
        ram = "Unavailable" if self.stats["ram_used"] is None else f"{self.stats['ram_used'] / 2**30:.1f} / {self.stats['ram_total'] / 2**30:.1f} GiB  ·  {self.stats['ram_percent']:.0f}%"
        machine.add_row("System RAM", ram)
        rss = "Unavailable" if self.stats["rss"] is None else f"{self.stats['rss'] / 2**30:.2f} GiB"
        cores = "—" if self.stats["cores"] is None else f"{self.stats['cores']:.1f} cores"
        machine.add_row("Pipeline RSS / CPU", f"{rss}  /  {cores}")
        machine.add_row("Worker limits · spatial / metrics / tiles", f"{self.resources['spatial_workers']} / {self.resources['metric_workers']} / {self.resources['tile_threads']}")
        if self.forecast.active and self.states.get("pairs", {}).get("status") == "running":
            tile_slots = max(row.get("tile_workers", 1) for row in self.forecast.active.values())
            machine.add_row("Active range jobs", f"{len(self.forecast.active)} · up to {tile_slots} tile slots per geometry")
        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        if not narrow:
            top.add_column(ratio=1)
            top.add_row(Panel(forecast, title="RUN FORECAST", border_style=MUTED), Panel(machine, title="MACHINE", border_style=MUTED))
        else:
            top.add_row(Panel(Group(forecast, Text(""), machine), title="RUN / MACHINE", border_style=MUTED))
        if compact:
            summary = Text(f"Elapsed {duration(elapsed)}   Remaining {remaining_label}   Finish {finish}\n", style="bold white")
            summary.append(f"CPU {'—' if cpu is None else f'{cpu:.0f}%'}   RAM {ram}   Pipeline {rss} / {cores}", style=CYAN)
            summary.append(f"\nWorker limits {self.resources['spatial_workers']} / {self.resources['metric_workers']} / {self.resources['tile_threads']}", style=MUTED)
            if self.forecast.active and self.states.get("pairs", {}).get("status") == "running":
                summary.append(f" · {len(self.forecast.active)} active range jobs", style=CYAN)
            if self.mode == "benchmark":
                summary.append(f"   Full build {duration(self.global_estimate)}", style="bold white")
            top = Panel(summary, title="RUN / MACHINE", border_style=MUTED)
        table = Table(expand=True, box=box.SIMPLE, show_edge=False, padding=(0, 1), header_style=f"bold {MUTED}")
        table.add_column("Done", width=5, justify="right", no_wrap=True)
        table.add_column("Stage", ratio=2, no_wrap=True, overflow="ellipsis")
        table.add_column("Measured work", ratio=3, no_wrap=True, overflow="ellipsis")
        table.add_column("Elapsed", justify="right", no_wrap=True)
        if not narrow:
            table.add_column("Benchmark", justify="right")
        table.add_column("Live left", justify="right", no_wrap=True)
        visible_names = self.names
        visible_count = max(1, min(6, height - 19)) if compact else len(self.names)
        if compact and len(self.names) > visible_count:
            current = next((i for i, n in enumerate(self.names) if self.states[n]["status"] in {"running", "failed", "interrupted"}), 0)
            start = min(max(0, current - 2), len(self.names) - visible_count)
            visible_names = self.names[start:start + visible_count]
        for name in visible_names:
            state = self.states[name]
            status = state["status"]
            color = GREEN if status == "passed" else CYAN if status == "running" else "red" if status in {"failed", "interrupted"} else MUTED
            marker = "✓" if status == "passed" else "—" if status == "running" else "×" if status in {"failed", "interrupted"} else "·"
            work = state.get("work")
            if name == "pairs" and self.forecast.counts and work is None:
                work = {"completed": sum(self.forecast.done.values()), "total": sum(self.forecast.counts.values()), "unit": "polygons"}
            if status == "running" and work and work.get("total", 0):
                marker = f"{min(99.9, max(0, 100 * work['completed'] / work['total'])):.1f}%"
            if status == "passed":
                progress = Text("Complete", style=GREEN)
            elif work and work.get("total", 0):
                def count(value):
                    return f"{value / 1e6:.2f}m" if value >= 1e6 else f"{value:,.0f}"
                progress = Text(f"{count(work['completed'])} / {count(work['total'])} {work.get('unit', '')}",
                                style=MUTED, no_wrap=True, overflow="ellipsis")
            else:
                progress = Text(state["phase"] if status != "pending" else "Waiting", style=color, overflow="ellipsis", no_wrap=True)
            spent = state.get("elapsed", now - state.get("started", now) + state.get("previous_elapsed", 0))
            columns = [Text(marker, style=color), Text(LABELS.get(name, name), style=color), progress, duration(spent) if status != "pending" else "—"]
            if not narrow:
                columns.append(duration(self.forecast.base.get(name)))
            left = self.forecast.remaining(name, now)
            phase_left = self.forecast.work_remaining(name, now) if work and work.get("scope") == "phase" else None
            left_label = duration(left) if left is not None else "Estimating" if status == "running" else "—"
            if left is None and phase_left is not None and status == "running":
                left_label = f"{duration(phase_left)} + ?"
            columns.append("—" if status in {"passed", "failed", "interrupted"} else left_label)
            table.add_row(*columns)
        title = f"PIPELINE · {sum(s['status'] == 'passed' for s in self.states.values())}/{len(self.names)} stages complete"
        pipeline = Panel(table, title=title, border_style=MUTED)
        header_height, summary_height = lines(header), lines(top)
        footer_height = 1 if compact else 3
        pipeline_height = min(lines(pipeline), max(3, height - header_height - summary_height - footer_height - 4))
        activity_rows = max(2, height - header_height - summary_height - footer_height - pipeline_height - 2)
        activity = Table.grid(expand=True)
        activity.add_column(style=MUTED, width=16)
        activity.add_column()
        active = sorted(self.tasks.values(), key=lambda e: e["time"], reverse=True)[:max(1, activity_rows // 2)]
        for event in active:
            fraction = event.get("fraction")
            detail = event.get("phase", "Processing")
            if fraction is not None:
                detail += f" · {min(1, max(0, fraction)):.1%} of {event.get('unit', 'current operation')}"
            elif event.get("completed") is not None:
                detail += f" · {event['completed']:,.0f} {event.get('unit', '')}"
            activity.add_row(f"PID {event.get('pid', '—')}", Text(detail, overflow="ellipsis", no_wrap=True))
        if not active:
            activity.add_row("Activity", "Waiting for the next operation" if self.status == "running" else "Run finished")
        for timestamp, message in list(self.logs)[-max(1, activity_rows - max(1, len(active))):]:
            activity.add_row(datetime.fromtimestamp(timestamp).strftime("%H:%M:%S"), Text(message, style=MUTED, overflow="ellipsis", no_wrap=True))
        footer = Text(self.note + "\n", style=MUTED, overflow="ellipsis", no_wrap=True)
        footer.append("Done = measured work, not time · ETAs approximate · + ? = later phase unestimated\n", style=MUTED)
        footer.append(f"Logs: {self.output / 'logs'}", style=MUTED)
        if compact:
            footer = Text(self.note, style=MUTED, overflow="ellipsis", no_wrap=True)
        layout = Layout(name="dashboard")
        layout.split_column(
            Layout(header, name="header", size=header_height),
            Layout(top, name="summary", size=summary_height),
            Layout(pipeline, name="pipeline", size=pipeline_height),
            Layout(Panel(activity, title="LIVE ACTIVITY", border_style=MUTED), name="activity", ratio=1),
            Layout(footer, name="footer", size=footer_height),
        )
        return layout


class LogTail:
    """Bounded fallback activity for tools without structured progress events."""

    def __init__(self, path):
        self.path, self.offset, self.pending = path, 0, ""

    def update(self, dashboard):
        with self.path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(self.offset, size - 65536)
            clipped = start > self.offset
            stream.seek(start)
            data = stream.read(65536)
            self.offset = stream.tell()
        if not data:
            return
        text = Text.from_ansi(data.decode("utf-8", errors="replace")).plain.replace("\r", "\n")
        lines = (("" if clipped else self.pending) + text).split("\n")
        self.pending = lines.pop()[-1000:]
        if clipped:
            lines = lines[1:]
        for line in [line.strip() for line in lines if line.strip()][-2:]:
            dashboard.logs.append((time.time(), line[:500]))


def run_command(stage, output: Path, environment: dict, dashboard: Dashboard | None = None, cwd=None):
    log_path = output / "logs" / (stage["name"] + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    events = output / "progress.jsonl"
    env = {**environment, "PIPELINE_PROGRESS_PATH": str(events), "PIPELINE_STAGE": stage["name"], "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    result = {**stage, "status": "running", "started_at": datetime.now().isoformat(), "log": str(log_path), "peak_process_tree_rss_bytes": None}
    if dashboard and stage["name"] in dashboard.states:
        dashboard.begin_stage(stage["name"])
    elif not dashboard or not dashboard.enabled:
        print(f"Pipeline: {stage['name']} · log: {log_path}", flush=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(stage["command"], cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        tail = LogTail(log_path)
        try:
            while process.poll() is None:
                if dashboard:
                    tail.update(dashboard)
                    dashboard.tick(process.pid)
                    rss = dashboard.stats["rss"]
                    if rss is not None:
                        result["peak_process_tree_rss_bytes"] = max(result["peak_process_tree_rss_bytes"] or 0, rss)
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
        except BaseException as error:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            if dashboard:
                for name, state in dashboard.states.items():
                    if state["status"] == "running":
                        dashboard.accept({"kind": "stage_end", "stage": name, "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                                          "time": time.time(), "elapsed": time.monotonic() - started})
            raise
    result.update(wall_seconds=time.monotonic() - started, exit_code=process.returncode,
                  status="passed" if process.returncode == 0 else "action-required" if process.returncode == 2 else "failed")
    if dashboard:
        tail.update(dashboard)
        dashboard.tick(force=True)
        names = [stage["name"]] if stage["name"] in dashboard.states else [n for n, state in dashboard.states.items() if state["status"] == "running"]
        for name in names:
            dashboard.accept({"kind": "stage_end", "stage": name, "status": result["status"], "time": time.time(), "elapsed": result["wall_seconds"]})
        dashboard.tick(force=True)
    if not dashboard or not dashboard.enabled:
        print(f"Pipeline: {stage['name']} {result['status']} in {result['wall_seconds']:.2f}s", flush=True)
    return result
