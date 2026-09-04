from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import duckdb
import geopandas as gpd
import h3
import pyarrow.parquet as pq
import pyogrio
from shapely.geometry import box

from ark_pipeline.cli.benchmark_pipeline import (
    DEFAULT_PROFILE,
    build_pairs,
    parser,
    plan,
    run,
    run_stage,
    select_sample,
)
from ark_pipeline.cli.benchmark_sample import build_sample
from ark_pipeline.runtime.benchmark_estimates import estimate
from ark_pipeline.runtime.provenance import atomic_json, sha256
from ark_pipeline.spatial.coverage import load_spatial_profile
from tests import test_global_adm2
from tests.test_refresh_crosswalk import fixture as crosswalk_fixture
from tests.test_refresh_crosswalk import register
from tests.test_tile_export import boundary_fixture


def fixture(root: Path) -> tuple[Path, Path]:
    crosswalk_fixture(root)
    path = root / "sources/assessments.csv"
    with path.open() as handle:
        assessments = list(csv.DictReader(handle))
    with path.open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*assessments[0], "systems"])
        writer.writeheader()
        writer.writerows({**row, "systems": "Terrestrial"} for row in assessments)
    register(root)
    _, _, adm2_manifest = test_global_adm2.ADM2BuildTests().fixture(root)
    manifest = json.loads((root / "acquisition/current.json").read_text())
    manifest["sources"].update(adm2_manifest["sources"])
    spatial = root / "shape"
    spatial.mkdir()
    # Deliberate duplicate polygon/species verifies deduplication; multiple H3
    # base cells force real metric worker dispatch across partitions.
    frame = gpd.GeoDataFrame({"id_no": [101, 101, 102, 103], "presence": [1] * 4,
                              "origin": [1] * 4, "seasonal": [1] * 4},
                             geometry=[box(12.4, 55.5, 12.411, 55.511)] * 2 +
                             [box(-70, 20, -69.988, 20.012), box(30, -20, 30.013, -19.987)], crs="EPSG:4326")
    pyogrio.write_dataframe(frame, spatial / "ranges.shp")
    archive = root / "ranges.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for path in spatial.iterdir():
            package.write(path, path.name)
    manifest["sources"]["iucn-spatial"] = {"validation_status": "passed", "release": "fixture", "files": [{
        "logical_name": "ranges.zip", "path": str(archive), "sha256": sha256(archive), "bytes": archive.stat().st_size}]}
    atomic_json(root / "acquisition/current.json", manifest)
    sample = root / "sample.parquet"
    build_sample(root, sample, DEFAULT_PROFILE, 4, 75, set())
    boundary = root / "boundary.geojson"
    boundary_fixture(boundary)
    return sample, boundary


class BenchmarkModelTests(unittest.TestCase):
    def observations(self):
        return {"pair_rows": 103, "write_seconds": 2, "observations": [
            {"size_bin": 0, "kernel_seconds": 1, "output_pairs": 1, "forced_extreme": False},
            {"size_bin": 0, "kernel_seconds": 2, "output_pairs": 2, "forced_extreme": False},
            {"size_bin": 0, "kernel_seconds": 100, "output_pairs": 100, "forced_extreme": True}]}

    def test_extremes_do_not_bias_band_means_and_missing_bands_prevent_total(self):
        pairs = self.observations()
        lists = {"res3_cells": 1, "res3_relationships": 3, "res7_cells": 10, "res7_relationships": 100}
        stages = [{"name": "pairs", "wall_seconds": 12}, {"name": "crosswalk", "wall_seconds": 5},
                  {"name": "lists", "wall_seconds": 3}, {"name": "fine_metrics", "wall_seconds": 4},
                  {"name": "tiles", "wall_seconds": 2}, {"name": "sample_setup", "wall_seconds": 500}]
        result = estimate(stages, {"size_bin_counts": [100]}, pairs, lists)
        self.assertEqual(result["projected_workload"]["raw_pairs"], 150)
        self.assertEqual(result["bands"][0]["observations"], 2)
        self.assertEqual(result["stages"][1]["estimated_seconds"], 5)
        self.assertAlmostEqual(result["stages"][0]["estimated_seconds"], 12 * 150 / 103)
        self.assertEqual(result["total_seconds"], sum(row["estimated_seconds"] for row in result["stages"]))
        self.assertLessEqual(result["total_seconds"], result["scenario_seconds"][1])
        result = estimate(stages, {"size_bin_counts": [100, 10]}, pairs, lists)
        self.assertIsNone(result["total_seconds"])
        self.assertEqual(result["missing_size_bins"], [1])
        self.assertEqual(result["stages"][1]["estimated_seconds"], 5)

    def test_census_includes_extremes_and_h3_projection_is_capped(self):
        pairs = self.observations()
        lists = {"res3_cells": 1, "res3_relationships": 3, "res7_cells": 10, "res7_relationships": 100}
        result = estimate([{"name": "pairs", "wall_seconds": 12}], {"size_bin_counts": [3]}, pairs, lists)
        self.assertAlmostEqual(result["total_seconds"], 12)
        result = estimate([], {"size_bin_counts": [10**15]}, pairs, lists)
        self.assertEqual(result["projected_workload"]["res7_cells"], h3.get_num_cells(7))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            estimate([], {"size_bin_counts": [2]}, pairs, lists)


class BenchmarkRunnerTests(unittest.TestCase):
    def test_plan_uses_shared_resources_and_isolates_all_output_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "benchmark"
            args = parser().parse_args(["--root", str(root), "--output-root", str(output), "--workers", "3", "--dry-run"])
            config, stages, env = plan(args)
            self.assertEqual(config["resources"]["spatial_workers"], 3)
            self.assertEqual(env["RES7_WORKERS"], "3")
            self.assertEqual(env["TIPPECANOE_MAX_THREADS"], "3")
            self.assertEqual(env["RES7_THREADS"], "1")
            self.assertEqual(env["TILE_DUCKDB_THREADS"], "1")
            for variable in ("SOURCE_DUCKDB_PATH", "BUILD_DUCKDB_PATH", "SOURCE_VALIDATION_REPORT_PATH", "VALIDATION_REPORT_PATH",
                             "TILE_DIR", "EXPORT_DIR", "MAP_METADATA_PATH", "PMTILES_PATH", "DUCKDB_SCRATCH_DIR"):
                self.assertTrue(Path(env[variable]).is_relative_to(output.resolve()))
            self.assertTrue(all("just" not in s["command"] for s in stages))
            self.assertEqual(run(args)["status"], "planned")
            self.assertFalse(output.exists())

    def test_failed_subprocess_keeps_log_and_timings(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_stage({"name": "failure", "command": [sys.executable, "-c", "print('deliberate failure'); raise SystemExit(7)"]}, Path(temporary), dict(os.environ))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exit_code"], 7)
            self.assertGreater(result["wall_seconds"], 0)
            self.assertIn("deliberate failure", Path(result["log"]).read_text())

    def test_selection_rejects_stale_data_and_reports_policy_changes(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            sample, _ = fixture(root)
            profile = load_spatial_profile(DEFAULT_PROFILE)
            selection = select_sample(sample, root, profile, None)
            self.assertEqual(selection["selected_rows"], 4)
            self.assertEqual(selection["warnings"], [])
            metadata = json.loads(sample.with_suffix(".json").read_text())
            metadata.pop("row_policy")
            atomic_json(sample.with_suffix(".json"), metadata)
            self.assertTrue(select_sample(sample, root, profile, 1)["warnings"])
            manifest = json.loads((root / "acquisition/current.json").read_text())
            manifest["sources"]["iucn-spatial"]["files"][0]["sha256"] = "changed"
            atomic_json(root / "acquisition/current.json", manifest)
            with self.assertRaisesRegex(ValueError, "stale"):
                select_sample(sample, root, profile, None)

    @unittest.skipUnless(shutil.which("tippecanoe"), "Tippecanoe required for end-to-end benchmark")
    def test_actual_sample_runs_through_every_stage_without_live_output_changes(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            sample, boundary = fixture(root)
            output = root / "benchmark"
            options = ["--root", str(root), "--sample", str(sample), "--output-root", str(output), "--workers", "2", "--memory-limit", "512MB", "--ui", "rich"]
            for key in ("admin0", "admin1", "eez", "conservation-framework"):
                options += ["--" + key, str(boundary)]
            # Any accidental write through default .env paths would alter these
            # production pointers/files. Actual stages must all remain isolated.
            repo = DEFAULT_PROFILE.parents[1]
            protected = [repo / "data/global", repo / "app/static/data/adm2-catalogs"]

            def snapshot():
                return {str(path): (str(path.resolve()), path.lstat().st_mtime_ns)
                        for directory in protected for path in [directory, *directory.rglob("*")]
                        if path.exists() or path.is_symlink()}

            before = snapshot()
            def interrupt_before_pairs(stage, *args, **kwargs):
                if stage["name"] == "pairs":
                    kwargs["dashboard"].begin_stage("pairs")
                    raise KeyboardInterrupt
                return run_stage(stage, *args, **kwargs)

            with patch("ark_pipeline.cli.benchmark_pipeline.run_stage", side_effect=interrupt_before_pairs):
                interrupted = run(parser().parse_args(options))
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertEqual([stage["name"] for stage in interrupted["stages"]], ["source_scan", "crosswalk"])
            saved_state = json.loads((output / "dashboard-state.json").read_text())
            self.assertEqual(saved_state["states"]["pairs"]["status"], "interrupted")
            report = run(parser().parse_args(options))
            if report["status"] != "passed":
                logs = "\n".join(path.read_text()[-3500:] for path in sorted((output / "logs").glob("*.log")))
                self.fail(f"{report.get('error')}\n{logs}")
            self.assertEqual(before, snapshot())
            self.assertEqual(report["estimate"]["status"], "planning-estimate")
            self.assertGreater(report["estimate"]["total_seconds"], 0)
            self.assertEqual(len(report["stages"]), 11)
            self.assertEqual(report["stages"][:2], interrupted["stages"])
            self.assertTrue(any("Resumed after interruption" in warning for warning in report["warnings"]))
            self.assertTrue(all(s["status"] == "passed" for s in report["stages"]))
            self.assertTrue((output / "tiles/current/priorities.pmtiles").is_file())
            self.assertTrue((output / "benchmark-report.md").is_file())
            events = [json.loads(line) for line in (output / "progress.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["kind"] == "geometry_done" for e in events), 4)
            self.assertTrue(any(e["kind"] == "pair_write" for e in events))
            self.assertTrue(any(e["stage"] == "fine_metrics" and e["kind"] == "work" for e in events))
            self.assertTrue(any(e["stage"] == "tiles" and e["kind"] == "detail" for e in events))
            stream_work = [e for e in events if e["stage"] == "tiles" and e["kind"] == "work"]
            self.assertTrue(stream_work)
            self.assertTrue(all(e["scope"] == "phase" and e["total"] == report["workload"]["res3_cells"] + report["workload"]["res7_cells"]
                                for e in stream_work))
            self.assertTrue(any(e["stage"] == "tiles" and e["kind"] == "phase" for e in events))
            coarse_metadata = json.loads((output / "coarse-tiles/map-metadata.json").read_text())
            self.assertEqual(coarse_metadata["score_domains"]["marine"], {"min": 0.0, "max": 0.0})
            self.assertEqual(coarse_metadata["species_normalized_score_domains"]["freshwater"], {"min": 0.0, "max": 0.0})
            pairs = pq.read_table(output / "pairs.parquet").to_pylist()
            self.assertEqual({row["iucn_sis_id"] for row in pairs}, {101, 102, 103})
            expected = {(row["h3_index"], row["iucn_sis_id"]) for row in pairs}
            parts = output / "spatial/serving/current/res7_merged_parts"
            actual = {(row["h3_cell"], sis) for part in parts.glob("*.parquet")
                      for row in pq.read_table(part).to_pylist() for sis in row["species_ids"]}
            self.assertEqual(actual, expected)
            self.assertLess(len(actual), len(pairs))
            metric_cells = {row["h3_index"] for part in (output / "metrics").glob("*.parquet")
                            for row in pq.read_table(part, columns=["h3_index"]).to_pylist()}
            self.assertEqual(metric_cells, {h3.int_to_str(cell) for cell, _ in expected})
            tile_report = json.loads((output / "tiles/current/build-report.json").read_text())
            self.assertEqual(tile_report["features"], report["workload"]["res3_cells"] + report["workload"]["res7_cells"])
            # The memory-aware default may select one worker. Its inline kernel
            # path must preserve exactly the same pairs as process-pool mode.
            serial = root / "serial-pairs"
            serial.mkdir()
            shutil.copy2(output / "selection.json", serial / "selection.json")
            config = json.loads((output / "config.json").read_text())
            config["output"] = str(serial)
            config["resources"]["spatial_workers"] = 1
            build_pairs(config)
            serial_rows = pq.read_table(serial / "pairs.parquet").to_pylist()
            self.assertEqual(sorted((r["h3_index"], r["iucn_sis_id"]) for r in serial_rows),
                             sorted((r["h3_index"], r["iucn_sis_id"]) for r in pairs))
            with duckdb.connect(str(output / "source.duckdb"), read_only=True) as connection:
                self.assertGreater(len(connection.execute("SHOW TABLES").fetchall()), 0)
            with self.assertRaisesRegex(ValueError, "already exists"):
                run(parser().parse_args(options))

    def test_orchestration_stops_after_failure_and_preserves_partial_report(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            sample, boundary = fixture(root)
            output = root / "failed-benchmark"
            options = ["--root", str(root), "--sample", str(sample), "--output-root", str(output)]
            for key in ("admin0", "admin1", "municipality", "eez", "conservation-framework"):
                options += ["--" + key, str(boundary)]
            with patch("ark_pipeline.cli.benchmark_pipeline.run_stage", return_value={"name": "source_scan", "status": "failed", "wall_seconds": 1, "log": "failure.log"}) as measured:
                report = run(parser().parse_args(options))
            self.assertEqual(measured.call_count, 1)
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("estimate", report)
            self.assertEqual(json.loads((output / "benchmark-report.json").read_text())["status"], "failed")

    def test_default_command_rediscovers_interruption_and_fresh_uses_new_directory(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            sample, boundary = fixture(root)
            options = ["--root", str(root), "--sample", str(sample), "--workers", "2", "--ui", "plain"]
            for key in ("admin0", "admin1", "municipality", "eez", "conservation-framework"):
                options += ["--" + key, str(boundary)]

            def interrupt(*args, **kwargs):
                raise KeyboardInterrupt

            with patch("ark_pipeline.cli.benchmark_pipeline.run_stage", side_effect=interrupt):
                first = run(parser().parse_args(options))
                resumed = run(parser().parse_args(options))
                fresh = run(parser().parse_args([*options, "--fresh"]))
            self.assertEqual(first["output"], resumed["output"])
            self.assertNotEqual(first["output"], fresh["output"])
            self.assertEqual(resumed["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
