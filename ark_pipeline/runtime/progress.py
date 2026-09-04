"""Low-overhead, process-safe progress events. Disabled outside a managed run."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

_context = contextvars.ContextVar("pipeline_progress", default={})
_last: dict[tuple, float] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return bool(os.environ.get("PIPELINE_PROGRESS_PATH"))


@contextlib.contextmanager
def scope(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def emit(kind="detail", *, force=False, **fields):
    path = os.environ.get("PIPELINE_PROGRESS_PATH")
    if not path:
        return
    record = {"stage": os.environ.get("PIPELINE_STAGE", "pipeline"), "pid": os.getpid(),
              **_context.get(), **fields, "kind": kind, "time": time.time()}
    # Geometry completion and lifecycle events are never sampled away.
    key = (os.getpid(), record["stage"], record.get("task", "main"), kind)
    with _lock:
        now = time.monotonic()
        if kind in {"detail", "work"} and not force and now - _last.get(key, 0) < 0.2:
            return
        _last[key] = now
        payload = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(payload) > 4096:
            raise ValueError("Progress records must stay below 4 KiB")
        # One append write per record, including across spawned worker processes.
        # Telemetry loss must never abort or change the data calculation.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except OSError:
            pass


def tracked_stage(name):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            if not enabled():
                return function(*args, **kwargs)
            started = time.monotonic()
            previous = os.environ.get("PIPELINE_STAGE")
            os.environ["PIPELINE_STAGE"] = name
            emit("stage_start", stage=name)
            try:
                result = function(*args, **kwargs)
                code = result if isinstance(result, int) else 0
                emit("stage_end", stage=name, status="passed" if code == 0 else "action-required" if code == 2 else "failed",
                     elapsed=time.monotonic() - started)
                return result
            except BaseException as error:
                emit("stage_end", stage=name, status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                     message=str(error)[:400], elapsed=time.monotonic() - started)
                raise
            finally:
                if previous is None:
                    os.environ.pop("PIPELINE_STAGE", None)
                else:
                    os.environ["PIPELINE_STAGE"] = previous
        return wrapped
    return decorate


def monitor_query(connection, phase="SQL query"):
    """Poll only native query progress; this is never a whole-stage percentage.

    Returns a stop function to call before closing the connection. SQL continues
    on its original thread, including Arrow readers tied to that thread.
    """
    if not enabled():
        return lambda: None
    connection.execute("SET enable_progress_bar=true")
    connection.execute("SET enable_progress_bar_print=false")
    connection.execute("SET progress_bar_time=0")
    stop = threading.Event()
    fields = dict(_context.get())
    stage = os.environ.get("PIPELINE_STAGE", "pipeline")

    def watch():
        while not stop.wait(0.25):
            try:
                value = connection.query_progress()
                emit(**{**fields, "stage": stage, "task": f"sql:{os.getpid()}", "phase": phase,
                        "fraction": None if value < 0 else min(1, value / 100), "unit": "current query"})
            except Exception:
                return

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()

    def finish():
        stop.set()
        thread.join(timeout=1)
        emit("task_end", stage=stage, task=f"sql:{os.getpid()}")
    return finish


class EventReader:
    def __init__(self, path: Path):
        self.path, self.offset, self.pending = path, 0, b""

    def read(self):
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read(4 * 1024 * 1024)
                self.offset = stream.tell()
        except FileNotFoundError:
            return []
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        records = []
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except (ValueError, UnicodeError):
                continue
        return records


def relay_compiler(stream):
    """Drain stderr concurrently so compiler progress cannot block feature input."""
    stage = os.environ.get("PIPELINE_STAGE", "tiles")

    def read():
        pending = ""
        while chunk := os.read(stream.fileno(), 4096):
            value = chunk.decode("utf-8", errors="replace")
            sys.stderr.write(value)
            pending += value
            parts = re.split(r"[\r\n]", pending)
            pending = parts.pop()[-1000:]
            for line in parts:
                line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
                if line:
                    match = re.search(r"(\d+(?:\.\d+)?)%", line)
                    emit(stage=stage, task="compiler", phase="Tippecanoe · " + line[-160:],
                         fraction=min(1, float(match[1]) / 100) if match else None, unit="compiler pass")
        emit("task_end", stage=stage, task="compiler")

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread
