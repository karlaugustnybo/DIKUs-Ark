from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ark_pipeline.cli.serving_prepare import main, parser, preparation_plan
from ark_pipeline.cli.spatial_pairs import DEFAULT_PROFILE
from ark_pipeline.runtime.dashboard import Dashboard
from tests.test_pipeline_dashboard import prior


class PrepareServingTests(unittest.TestCase):
    def test_rich_build_tracks_nested_stages_and_drops_stale_prior_after_acquisition(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            h3_root = root / "lists"
            h3_root.mkdir()
            crosswalk = root / "crosswalk.parquet"
            crosswalk.touch()
            displays, calls = [], []

            def make_display(*args, **kwargs):
                display = Dashboard(*args, **kwargs)
                displays.append(display)
                return display

            def run_stage(stage, output, environment, dashboard, cwd):
                calls.append(stage["name"])
                self.assertEqual(environment["SPATIAL_WORKERS"], "2")
                names = ["boundaries", "metadata", "coarse_db", "coarse_cache", "fine_metrics", "prepared_inputs"] if stage["name"] == "serving" else [stage["name"]]
                for name in names:
                    dashboard.accept({"kind": "stage_start", "stage": name, "time": 1})
                    dashboard.accept({"kind": "stage_end", "stage": name, "time": 2, "elapsed": 1, "status": "passed"})
                return {"exit_code": 0}

            with patch("ark_pipeline.cli.serving_prepare.Dashboard", side_effect=make_display), \
                 patch("ark_pipeline.cli.serving_prepare.load_prior", side_effect=[({**prior(), "path": "old"}, "Old benchmark"), (None, "Sources changed")]), \
                 patch("ark_pipeline.cli.serving_prepare.load_manifest", return_value={}), \
                 patch("ark_pipeline.cli.serving_prepare.run_command", side_effect=run_stage):
                result = main(["--root", str(root), "--h3-root", str(h3_root), "--crosswalk", str(crosswalk),
                               "--preview-root", str(root / "preview"), "--workers", "2", "--ui", "rich", "--acquire", "update", "--tiles"])
            self.assertEqual(result, 0)
            self.assertEqual(calls, ["acquisition", "serving", "tiles"])
            display = displays[0]
            self.assertTrue(all(state["status"] == "passed" for state in display.states.values()))
            self.assertIsNone(display.forecast.prior)
            self.assertIn("acquisition", display.forecast.finished)
            self.assertEqual(display.forecast.total(display.names, 3), (0, 0))

    def test_automatic_refresh_precedes_pairs_and_pins_matches_through_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            crosswalk_root = root / "derived/iucn-goat-crosswalk"
            first, second = crosswalk_root / "generations/first", crosswalk_root / "generations/second"
            spatial = root / "spatial"
            received = []

            def run(command, *, cwd, env, check):
                received.append((command, dict(env)))
                if command[1:3] == ["-m", "ark_pipeline.cli.crosswalk_refresh"]:
                    first.mkdir(parents=True)
                    second.mkdir()
                    (first / "iucn_goat_crosswalk.parquet").touch()
                    (second / "iucn_goat_crosswalk.parquet").touch()
                    (crosswalk_root / "current").symlink_to("generations/first")
                elif command[1:3] == ["-m", "ark_pipeline.cli.spatial_aggregate"]:
                    (spatial / "serving/current").mkdir(parents=True)
                elif command == ["just", "global-prepare"]:
                    (crosswalk_root / "current").unlink()
                    (crosswalk_root / "current").symlink_to("generations/second")

            args = ["--root", str(root), "--preview-root", str(root / "preview"),
                    "--spatial-root", str(spatial), "--crosswalk", str(root / "old-missing.parquet"),
                    "--acquire", "download", "--crosswalk-mode", "refresh", "--build-pairs", "--tiles"]
            with patch("ark_pipeline.cli.serving_prepare.subprocess.run", side_effect=run), \
                 patch("ark_pipeline.cli.serving_prepare.load_manifest", return_value={}):
                self.assertEqual(main(args), 0)
            self.assertEqual([command[2] for command, _ in received[:4]],
                             ["ark_pipeline.cli.sources_sync", "ark_pipeline.cli.crosswalk_refresh", "ark_pipeline.cli.spatial_pairs", "ark_pipeline.cli.spatial_aggregate"])
            self.assertTrue(all(env["GLOBAL_CROSSWALK_PATH"] == str((first / "iucn_goat_crosswalk.parquet").resolve())
                                for _, env in received[2:]))

            received.clear()

            def fail_refresh(command, **kwargs):
                received.append(command)
                if command[1:3] == ["-m", "ark_pipeline.cli.crosswalk_refresh"]:
                    raise subprocess.CalledProcessError(1, command)

            with patch("ark_pipeline.cli.serving_prepare.subprocess.run", side_effect=fail_refresh):
                self.assertEqual(main(args), 1)
            self.assertEqual(len(received), 2)

    def test_complete_build_checks_updated_crosswalk_before_pairs_and_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            crosswalk = root / "crosswalk.parquet"
            crosswalk.touch()
            summary = root / "match_summary.json"
            summary.write_text(json.dumps({"sources": {"goat_species": {"sha256": "old"}}}))
            spatial = root / "spatial"
            generation = spatial / "serving/generations/new"
            received = []

            def run(command, *, cwd, env, check):
                received.append((command, dict(env)))
                if command[1:3] == ["-m", "ark_pipeline.cli.sources_sync"]:
                    manifest = root / "acquisition/current.json"
                    manifest.parent.mkdir(exist_ok=True)
                    manifest.write_text(json.dumps({"schema_version": 1, "sources": {
                        "goat-species": {"files": [{
                            "logical_name": "tol_species_all_ranks.tsv", "sha256": "new",
                        }]},
                    }}))
                elif command[1:3] == ["-m", "ark_pipeline.cli.spatial_aggregate"]:
                    generation.mkdir(parents=True)
                    (spatial / "serving/current").symlink_to("generations/new")

            args = ["--root", str(root), "--spatial-root", str(spatial),
                    "--crosswalk", str(crosswalk), "--preview-root", str(root / "preview"),
                    "--acquire", "update", "--build-pairs", "--tiles"]
            with patch("ark_pipeline.cli.serving_prepare.subprocess.run", side_effect=run):
                self.assertEqual(main(args), 1)
                self.assertEqual(len(received), 1)
                report = json.loads((root / "preview/prepare-report.json").read_text())
                self.assertIn("crosswalk is stale for goat_species", report["error"])
                summary.write_text(json.dumps({"sources": {"goat_species": {"sha256": "new"}}}))
                received.clear()
                self.assertEqual(main(args), 0)
            self.assertEqual([command[2] for command, _ in received[:3]],
                             ["ark_pipeline.cli.sources_sync", "ark_pipeline.cli.spatial_pairs", "ark_pipeline.cli.spatial_aggregate"])
            self.assertEqual([command for command, _ in received[3:]],
                             [["just", "global-prepare"], ["just", "data-tiles"]])
            self.assertTrue(all(env["GLOBAL_H3_ROOT"] == str(generation.resolve()) for _, env in received[3:]))
            report = json.loads((root / "preview/prepare-report.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(report["completed_commands"]), 5)

    def test_complete_build_preserves_acquisition_action_required_and_dry_run_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            preview = root / "preview"
            args = ["--root", str(root), "--preview-root", str(preview),
                    "--acquire", "download", "--crosswalk-mode", "refresh", "--build-pairs", "--tiles"]
            with patch("ark_pipeline.cli.serving_prepare.subprocess.run") as run:
                self.assertEqual(main([*args, "--dry-run"]), 0)
                run.assert_not_called()
                self.assertFalse(preview.exists())
                run.side_effect = subprocess.CalledProcessError(2, ["source-acquisition"])
                self.assertEqual(main(args), 2)
                self.assertEqual(run.call_count, 1)
            report = json.loads((preview / "prepare-report.json").read_text())
            self.assertEqual(report["status"], "action-required")
            self.assertEqual(report["completed_commands"], [])

    def test_new_profile_is_selected_even_when_environment_still_points_at_legacy_pack(self) -> None:
        with patch.dict("os.environ", {"GLOBAL_H3_ROOT": "/previous/pack"}):
            args = parser().parse_args(["--root", "/tmp/new release", "--profile", str(DEFAULT_PROFILE)])
            commands, environment = preparation_plan(args)
        self.assertEqual(commands[0][1:4], ["-m", "ark_pipeline.cli.spatial_aggregate", "run"])
        self.assertEqual(commands[-1], ["just", "global-prepare"])
        self.assertEqual(environment["GLOBAL_H3_ROOT"], str(
            Path("/tmp/new release/derived/iucn-richness-any-touch-v3/serving/current").resolve()
        ))
        self.assertEqual(commands[0][5], str(Path("/tmp/new release").resolve()))

    def test_explicit_legacy_pack_skips_export(self) -> None:
        args = parser().parse_args(["--h3-root", "/tmp/old lists"])
        commands, environment = preparation_plan(args)
        self.assertEqual(commands, [["just", "global-prepare"]])
        self.assertEqual(environment["GLOBAL_H3_ROOT"], str(Path("/tmp/old lists").resolve()))

    def test_preparation_pins_the_generation_created_by_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crosswalk = root / "crosswalk.parquet"
            crosswalk.touch()
            spatial = root / "spatial"
            generation = spatial / "serving/generations/new"
            generation.mkdir(parents=True)
            received = []

            def run(command, *, cwd, env, check):
                received.append((command, env["GLOBAL_H3_ROOT"]))
                if len(received) == 1:
                    (spatial / "serving/current").symlink_to("generations/new")

            with patch("ark_pipeline.cli.serving_prepare.subprocess.run", side_effect=run), \
                 patch("ark_pipeline.cli.serving_prepare.load_manifest", return_value={}), \
                 contextlib.redirect_stdout(io.StringIO()):
                status = main([
                    "--root", str(root), "--spatial-root", str(spatial),
                    "--crosswalk", str(crosswalk), "--preview-root", str(root / "preview"),
                ])
            self.assertEqual(status, 0)
            self.assertEqual(len(received), 2)
            self.assertEqual(received[-1][1], str(generation.resolve()))
            self.assertTrue((root / "preview/prepare-report.json").is_file())

    def test_missing_crosswalk_stops_before_export_and_dry_run_does_not_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
             patch("ark_pipeline.cli.serving_prepare.subprocess.run") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            args = ["--root", temporary, "--crosswalk", str(Path(temporary) / "absent"),
                    "--preview-root", str(Path(temporary) / "preview")]
            self.assertEqual(main(args), 1)
            self.assertEqual(main([*args, "--dry-run"]), 0)
            run.assert_not_called()

    def test_tiles_continue_with_the_same_pinned_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            current = root / "current"
            current.symlink_to(first)
            crosswalk = root / "crosswalk"
            crosswalk.touch()
            received = []

            def run(command, *, cwd, env, check):
                received.append((command, env["GLOBAL_H3_ROOT"]))
                if command == ["just", "global-prepare"]:
                    current.unlink()
                    current.symlink_to(second)

            with patch("ark_pipeline.cli.serving_prepare.subprocess.run", side_effect=run), \
                 patch("ark_pipeline.cli.serving_prepare.load_manifest", return_value={}):
                status = main(["--root", str(root), "--h3-root", str(current),
                               "--crosswalk", str(crosswalk), "--preview-root", str(root / "preview"), "--tiles"])
            self.assertEqual(status, 0)
            self.assertEqual(received, [(["just", "global-prepare"], str(first.resolve())),
                                        (["just", "data-tiles"], str(first.resolve()))])


if __name__ == "__main__":
    unittest.main()
