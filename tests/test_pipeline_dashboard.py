from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from ark_pipeline.runtime.checkpoints import find_checkpoint
from ark_pipeline.runtime.dashboard import Dashboard, LogTail, run_command
from ark_pipeline.runtime.forecasts import Forecast, load_prior
from ark_pipeline.runtime.progress import EventReader, emit
from ark_pipeline.runtime.provenance import atomic_json, sha256
from ark_pipeline.runtime.resources import resolve_resources

RESOURCES = resolve_resources(workers=2, environ={}).report()


def prior():
    return {"resources": RESOURCES, "stages": [{"name": name, "wall_seconds": 100} for name in ("pairs", "lists", "fine_metrics", "tiles")],
            "estimate": {"bands": [
                {"size_bin": 0, "population": 100, "mean_kernel_seconds": 1, "mean_output_pairs": 10},
                {"size_bin": 1, "population": 10, "mean_kernel_seconds": 100, "mean_output_pairs": 1000}],
                "stages": [{"name": name, "estimated_seconds": 100} for name in ("pairs", "lists", "fine_metrics", "tiles")],
                "projected_workload": {"res7_cells": 1000, "res7_relationships": 10000, "res3_cells": 100}},
            "population": {"archives": [{"logical_name": "old.zip", "size_bin_counts": [40, 3]}]},
            "workload": {"res7_cells": 100, "res7_relationships": 1000, "res3_cells": 10},
            "observations": [], "selection": {"sha256": "fixture"}}


class LiveEstimateTests(unittest.TestCase):
    def test_last_extreme_uses_its_live_progress_without_eight_core_discount(self):
        model = Forecast(None, {**RESOURCES, "spatial_workers": 8})
        model.counts.update({23: 997})
        model.done.update({23: 996})
        model.band_prior[23] = (50, 100)
        model.kernel_done = 4000
        model.accept({"stage": "pairs", "kind": "stage_start", "time": 0})
        model.accept({"stage": "pairs", "kind": "geometry_start", "time": 250,
                      "task": "last", "id": "997", "size_bin": 23, "forced_extreme": True})
        model.accept({"stage": "pairs", "kind": "detail", "time": 700,
                      "task": "last", "fraction": 0.5})
        self.assertEqual(model.remaining("pairs", 700), 450)
        model.accept({"stage": "pairs", "kind": "detail", "time": 701,
                      "task": "last", "fraction": None})
        self.assertIsNone(model.remaining("pairs", 701))

    def test_recent_slowdown_and_longest_partition_bound_the_estimate(self):
        model = Forecast(None, RESOURCES)
        model.accept({"stage": "fine_metrics", "kind": "stage_start", "time": 0})
        for now, completed in ((60, 850), (90, 900)):
            model.accept({"stage": "fine_metrics", "kind": "work", "time": now,
                          "overall": True, "completed": completed, "total": 1000})
        self.assertEqual(model.remaining("fine_metrics", 90), 60)
        model.accept({"stage": "fine_metrics", "kind": "detail", "time": 10,
                      "unit": "partition", "task": "base:1", "fraction": 0})
        model.accept({"stage": "fine_metrics", "kind": "detail", "time": 90,
                      "unit": "partition", "task": "base:1", "fraction": 0.5})
        self.assertEqual(model.remaining("fine_metrics", 90), 80)
        model.accept({"stage": "fine_metrics", "kind": "task_end", "task": "base:1"})
        self.assertEqual(model.remaining("fine_metrics", 90), 60)

    def test_tile_stream_eta_does_not_claim_to_include_compilation(self):
        model = Forecast(None, RESOURCES)
        model.accept({"stage": "tiles", "kind": "stage_start", "time": 0})
        model.accept({"stage": "tiles", "kind": "work", "time": 10, "overall": True,
                      "completed": 10, "total": 100, "scope": "phase"})
        self.assertEqual(model.work_remaining("tiles", 10), 90)
        self.assertIsNone(model.remaining("tiles", 10))
        self.assertIsNone(model.total(["tiles"], 10)[0])
        model.accept({"stage": "tiles", "kind": "phase", "time": 20})
        self.assertIsNone(model.work_remaining("tiles", 20))

    def test_fast_small_polygons_cannot_erase_slow_band_cost(self):
        model = Forecast(prior(), RESOURCES, "full")
        model.accept({"stage": "pairs", "kind": "stage_start", "time": 0})
        for i in range(20):
            model.accept({"stage": "pairs", "kind": "geometry_done", "id": str(i), "task": "worker", "size_bin": 0,
                          "kernel_seconds": 0.1, "output_pairs": 10})
        self.assertEqual(model.done[0], 20)
        self.assertEqual(model.band_cost(1), (100, 1000))
        self.assertGreater(model.remaining("pairs", 2), 900)
        # A duplicate event must not manufacture completed work.
        model.accept({"stage": "pairs", "kind": "geometry_done", "id": "0", "task": "worker", "size_bin": 0,
                      "kernel_seconds": 999, "output_pairs": 100000})
        self.assertEqual(model.done[0], 20)

    def test_forced_extreme_is_not_a_random_band_observation(self):
        model = Forecast(prior(), RESOURCES, "full")
        model.accept({"stage": "pairs", "kind": "geometry_done", "id": "extreme", "task": "worker", "size_bin": 1,
                      "forced_extreme": True, "kernel_seconds": 10000, "output_pairs": 100000})
        self.assertEqual(model.band_cost(1), (100, 1000))
        self.assertEqual(model.done[1], 1)
        model.accept({"stage": "pairs", "kind": "archive_reused", "logical_name": "old.zip"})
        self.assertEqual(model.counts[0], 60)
        self.assertEqual(model.counts[1], 7)

    def test_live_workload_replaces_sample_density_assumptions(self):
        model = Forecast(prior(), RESOURCES)
        model.accept({"stage": "lists", "kind": "workload", "counts": {"res7_cells": 200, "res7_relationships": 4000, "res3_cells": 20}})
        self.assertEqual(model.base["fine_metrics"], 300)  # average of 2x cells and 4x relationships
        self.assertEqual(model.base["tiles"], 200)
        model.accept({"stage": "fine_metrics", "kind": "stage_start", "time": 0})
        model.accept({"stage": "fine_metrics", "kind": "work", "overall": True, "completed": 50, "total": 100})
        self.assertEqual(model.remaining("fine_metrics", 30), 30)

    def test_missing_band_and_overdue_prior_are_never_zero_eta(self):
        model = Forecast(None, RESOURCES)
        model.counts.update({0: 20, 1: 10})
        model.accept({"stage": "pairs", "kind": "stage_start", "time": 0})
        model.accept({"stage": "pairs", "kind": "geometry_done", "id": "1", "task": "worker", "size_bin": 0,
                      "kernel_seconds": 1, "output_pairs": 10})
        self.assertIsNone(model.remaining("pairs", 1))
        model.base["tiles"] = 10
        model.accept({"stage": "tiles", "kind": "stage_start", "time": 0})
        self.assertIsNone(model.remaining("tiles", 20))
        self.assertIsNone(model.total(["pairs", "tiles"], 20)[0])

    def test_compatible_prior_is_discovered_and_stale_explicit_prior_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_json(root / "acquisition/current.json", {"sources": {}})
            report = {**prior(), "status": "passed", "profile_sha256": "profile",
                      "acquisition_manifest_sha256": sha256(root / "acquisition/current.json")}
            path = root / "benchmarks/pipeline/run/benchmark-report.json"
            atomic_json(path, report)
            atomic_json(path.with_name("pairs-report.json"), {"observations": []})
            atomic_json(path.with_name("population.json"), report["population"])
            self.assertIsNotNone(load_prior(root, "profile")[0])
            self.assertIsNone(load_prior(root, "changed")[0])
            with self.assertRaisesRegex(ValueError, "Cannot use"):
                load_prior(root, "changed", path)


class EventAndDisplayTests(unittest.TestCase):
    def test_percentage_rows_are_stable_single_lines_and_total_is_honest(self):
        for width, height in ((95, 54), (128, 45), (80, 24)):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as temporary:
                output = io.StringIO()
                console = Console(file=output, width=width, height=height, color_system=None)
                display = Dashboard(["pairs", "tiles"], Path(temporary), RESOURCES, ui="plain", console=console)
                display.forecast.counts.update({0: 997})
                display.forecast.done.update({0: 996})
                display.accept({"stage": "pairs", "kind": "stage_start", "time": 0})
                display.accept({"stage": "pairs", "kind": "detail", "task": "worker", "time": 1,
                                "phase": "Worker detail"})
                console.print(display.render(now=2))
                text = output.getvalue()
                self.assertIn("99.9%", text)
                self.assertIn("996 / 997 polygons", text)
                self.assertNotIn("●", text)
                self.assertNotIn("known", text.replace("Unknown", ""))
                self.assertIn("unestimated", text)

    def test_tile_stage_label_does_not_flash_between_worker_messages(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            console = Console(file=output, width=95, height=40, color_system=None)
            display = Dashboard(["tiles"], Path(temporary), RESOURCES, ui="plain", console=console)
            display.accept({"stage": "tiles", "kind": "stage_start", "time": 0})
            display.accept({"stage": "tiles", "kind": "work", "time": 10, "task": "tile-stream",
                            "overall": True, "scope": "phase", "phase": "Stream map features",
                            "completed": 100, "total": 1000, "unit": "features streamed"})
            for task in ("features", "compiler"):
                display.accept({"stage": "tiles", "kind": "detail", "time": 11,
                                "task": task, "phase": f"Different detail for {task}"})
                self.assertEqual(display.states["tiles"]["phase"], "Stream map features")
            console.print(display.render(now=11))
            text = output.getvalue()
            self.assertIn("10.0%", text)
            self.assertIn("100 / 1,000 features streamed", text)
            self.assertIn("+ ?", text)
            display.accept({"stage": "tiles", "kind": "phase", "time": 12, "phase": "Compile tile zooms"})
            self.assertNotIn("work", display.states["tiles"])
            self.assertEqual(display.states["tiles"]["phase"], "Compile tile zooms")

    def test_spatial_archive_progress_overrides_polygon_forecast_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            console = Console(file=output, width=110, height=40, color_system=None)
            display = Dashboard(
                ["pairs"], Path(temporary), RESOURCES, ui="plain", console=console
            )
            display.forecast.counts.update({0: 997})
            display.forecast.done.update({0: 997})
            display.accept({"stage": "pairs", "kind": "stage_start", "time": 0})
            display.accept(
                {
                    "stage": "pairs",
                    "kind": "work",
                    "time": 1,
                    "overall": True,
                    "completed": 2,
                    "total": 31,
                    "unit": "source archives",
                }
            )
            console.print(display.render(now=2))
            text = output.getvalue()
            self.assertIn("Spatial sources", text)
            self.assertIn("2 / 31 source archives", text)
            self.assertNotIn("997 / 997 polygons", text)
            self.assertIn("Estimating", text)

    def test_ram_used_and_percentage_share_the_same_definition(self):
        with tempfile.TemporaryDirectory() as temporary:
            display = Dashboard([], Path(temporary), RESOURCES, ui="plain")
            memory = SimpleNamespace(total=8 * 2**30, available=1.5 * 2**30, used=2.8 * 2**30, percent=81.25)
            with patch("ark_pipeline.runtime.dashboard.psutil.virtual_memory", return_value=memory):
                display.monitor()
            self.assertEqual(display.stats["ram_used"], 6.5 * 2**30)
            self.assertEqual(display.stats["ram_percent"], 81.25)

    def test_checkpoint_restores_progress_and_learning_without_counting_downtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            output = parent / "run"
            identity = {"sources": "same", "workers": 2}
            display = Dashboard(["lists", "pairs"], output, RESOURCES, prior=prior(), mode="full", ui="plain", identity=identity)
            with display:
                display.accept({"stage": "lists", "kind": "stage_end", "time": time.time(), "elapsed": 11, "status": "passed"})
                display.begin_stage("pairs")
                display.accept({"stage": "pairs", "kind": "geometry_done", "id": "one", "task": "worker", "time": time.time(),
                                "size_bin": 0, "kernel_seconds": 2, "output_pairs": 20})
                display.status = "interrupted"
            checkpoint = output / "dashboard-state.json"
            saved = json.loads(checkpoint.read_text())
            self.assertEqual(saved["states"]["pairs"]["status"], "interrupted")
            saved["saved_at"] -= 3600
            atomic_json(checkpoint, saved)
            self.assertEqual(find_checkpoint(parent, identity), output)
            self.assertIsNone(find_checkpoint(parent, {**identity, "sources": "changed"}))
            restored = Dashboard(["lists", "pairs"], output, RESOURCES, ui="plain", identity=identity)
            with restored:
                self.assertTrue(restored.restored)
                self.assertLess(time.time() - restored.started, 10)
                self.assertEqual(restored.states["lists"]["elapsed"], 11)
                self.assertEqual(restored.forecast.done[0], 1)
                learned = restored.forecast.band_cost(0)
                restored.begin_stage("pairs")
                self.assertEqual(restored.forecast.done[0], 0)  # uncommitted archive must be recalculated
                self.assertEqual(restored.forecast.band_cost(0), learned)
                self.assertEqual(restored.states["lists"]["status"], "passed")

    def test_checkpoint_lock_prevents_two_writers(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            identity = {"sources": "same"}
            with Dashboard(["pairs"], output, RESOURCES, ui="plain", identity=identity):
                other = Dashboard(["pairs"], output, RESOURCES, ui="plain", identity=identity)
                before = (output / "dashboard-state.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "already active"):
                    other.__enter__()
                other.__exit__(None, None, None)
                self.assertEqual((output / "dashboard-state.json").read_bytes(), before)

    def test_cancellation_terminates_stage_and_records_interrupted_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            display = Dashboard(["pairs"], output, RESOURCES, ui="plain")
            popen = subprocess.Popen
            children = []

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                return child

            with patch("ark_pipeline.runtime.dashboard.subprocess.Popen", side_effect=launch), \
                 patch.object(display, "tick", side_effect=KeyboardInterrupt), \
                 self.assertRaises(KeyboardInterrupt):
                run_command({"name": "pairs", "command": [sys.executable, "-c", "import time; time.sleep(30)"]}, output, dict(os.environ), display)
            self.assertIsNotNone(children[0].poll())
            self.assertEqual(display.states["pairs"]["status"], "interrupted")
            self.assertNotIn("pairs", display.forecast.finished)

    def test_log_tail_handles_partial_lines_and_bounds_large_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "stage.log"
            path.write_text("first\npart")
            display = Dashboard(["acquisition"], root, RESOURCES, ui="plain")
            tail = LogTail(path)
            tail.update(display)
            with path.open("a") as stream:
                stream.write("ial\n")
            tail.update(display)
            self.assertEqual([line for _, line in display.logs], ["first", "partial"])
            with path.open("a") as stream:
                stream.write("x" * 100000 + "\n\x1b[32mready\x1b[0m\n")
            tail.update(display)
            self.assertEqual(display.logs[-1][1], "ready")
            self.assertLessEqual(max(len(line) for _, line in display.logs), 500)

    def test_concurrent_append_and_partial_record_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            script = "from ark_pipeline.runtime.progress import emit\nfor i in range(40): emit('geometry_done', id=str(i))"
            env = {**os.environ, "PIPELINE_PROGRESS_PATH": str(path), "PIPELINE_STAGE": "pairs"}
            processes = [subprocess.Popen([sys.executable, "-c", script], env=env) for _ in range(3)]
            for process in processes:
                self.assertEqual(process.wait(timeout=20), 0)
            reader = EventReader(path)
            records = reader.read()
            self.assertEqual(len(records), 120)
            self.assertEqual(len({record["pid"] for record in records}), 3)
            with path.open("a") as stream:
                stream.write('{"kind":"message"')
            self.assertEqual(reader.read(), [])
            with path.open("a") as stream:
                stream.write(',"stage":"pairs"}\n')
            self.assertEqual(reader.read(), [{"kind": "message", "stage": "pairs"}])
            with patch.dict(os.environ, {"PIPELINE_PROGRESS_PATH": str(Path(temporary) / "absent/events")}):
                emit("message", message="telemetry failure cannot stop a build")

    def test_render_wide_and_compact_with_literal_log_text(self):
        for width, height in [(128, 45), (80, 24)]:
            with self.subTest(width=width), tempfile.TemporaryDirectory() as temporary:
                output = io.StringIO()
                console = Console(file=output, width=width, height=height, color_system=None)
                display = Dashboard(["pairs", "fine_metrics", "tiles"], Path(temporary), RESOURCES, prior=prior(), ui="plain", console=console)
                display.accept({"stage": "pairs", "kind": "stage_start", "time": 100})
                display.accept({"stage": "pairs", "kind": "detail", "task": "worker", "pid": 1, "time": 105,
                                "phase": "[red]literal filename[/]", "fraction": .45, "unit": "polygon grid tiles"})
                console.print(display.render(now=110))
                text = output.getvalue()
                self.assertIn("DATA LAB", text)
                self.assertIn("RAM", text)
                self.assertIn("LIVE ACTIVITY", text)
                self.assertIn("45.0%", text)
                self.assertIn("[red]literal filename[/]", text)

    def test_query_percentage_does_not_become_whole_stage_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            display = Dashboard(["pairs"], Path(temporary), RESOURCES, ui="plain")
            display.accept({"stage": "pairs", "kind": "detail", "task": "sql", "time": 10, "fraction": .99, "unit": "current query"})
            self.assertNotIn("work", display.states["pairs"])
            self.assertIsNone(display.forecast.remaining("pairs", 20))


if __name__ == "__main__":
    unittest.main()
