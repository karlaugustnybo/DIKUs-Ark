from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import h3
import h3.api.numpy_int as h3_int
import numpy as np
import pyarrow.parquet as pq
import pyogrio
from shapely.geometry import MultiPolygon, Polygon, box

from ark_pipeline.cli.spatial_pairs import (
    GeometryWorkerFailure,
    build,
    build_parser,
    exclusion_reason_values,
    finalize_relations,
    main,
    pair_stage_identities,
)
from ark_pipeline.runtime.provenance import sha256
from ark_pipeline.spatial.coverage import (
    GeometryCoverageError,
    _candidate_tile_size,
    _simplified_decision_geometry,
    cell_polygon,
    direct_any_touch_intersecting_cells_native,
    exact_intersecting_cells_native,
    load_spatial_profile,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ANY_TOUCH_PROFILE_PATH = REPOSITORY_ROOT / "config" / "spatial_semantics_any_touch_v2.toml"
RICHNESS_PROFILE_PATH = REPOSITORY_ROOT / "config" / "spatial_semantics_iucn_richness_v3.toml"


class AnyTouchSpatialSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = load_spatial_profile(ANY_TOUCH_PROFILE_PATH)

    def test_production_rejects_superseded_kernel_profiles(self) -> None:
        profile = replace(self.profile, production_kernel="direct-tiled-exact-v1")
        with self.assertRaisesRegex(ValueError, "historical kernels"):
            exact_intersecting_cells_native(box(12.4, 55.5, 12.41, 55.51), profile)

    def test_shared_edges_and_vertices_add_all_neighbours(self) -> None:
        centre = h3.latlng_to_cell(55.6761, 12.5683, 7)
        result = direct_any_touch_intersecting_cells_native(cell_polygon(centre), self.profile)
        expected = np.sort(
            np.asarray(
                [h3_int.str_to_int(cell) for cell in h3.grid_disk(centre, 1)],
                dtype=np.uint64,
            )
        )
        np.testing.assert_array_equal(result.cells, expected)

    def test_hole_boundary_counts_as_contact(self) -> None:
        centre = h3.latlng_to_cell(55.6761, 12.5683, 7)
        centre_polygon = cell_polygon(centre)
        shell = centre_polygon.buffer(0.04)
        with_hole = Polygon(shell.exterior.coords, [centre_polygon.exterior.coords])
        result = direct_any_touch_intersecting_cells_native(with_hole, self.profile)
        self.assertIn(h3_int.str_to_int(centre), result.cells)

    def test_large_ranges_select_the_measured_smaller_partition(self) -> None:
        self.assertEqual(_candidate_tile_size(box(0, 0, 1, 1), self.profile), 10.0)
        self.assertEqual(_candidate_tile_size(box(0, 0, 20, 10), self.profile), 2.5)

    def test_iucn_richness_profile_filters_all_three_attributes(self) -> None:
        profile = load_spatial_profile(RICHNESS_PROFILE_PATH)
        self.assertEqual(profile.presence, (1, 4))
        self.assertEqual(profile.origin, (1, 2, 6))
        self.assertEqual(profile.seasonality, (1, 2, 3, 5))
        self.assertEqual(profile.decision_simplification_degrees, 0.01)
        self.assertEqual(profile.decision_simplification_min_bbox_degrees2, 100.0)
        self.assertLessEqual(
            profile.decision_simplification_degrees * 111_693.98,
            profile.max_decision_displacement_metres,
        )
        self.assertIsNone(exclusion_reason_values(1, b"wkb", 4, 6, profile, seasonal=5))
        self.assertEqual(
            exclusion_reason_values(1, b"wkb", 1, 1, profile, seasonal=4),
            "seasonality_policy",
        )

    def test_bounded_simplification_keeps_remote_components(self) -> None:
        source = MultiPolygon(
            [
                box(0, 0, 10, 10).buffer(0.001, quad_segs=16),
                box(100, 20, 100.001, 20.001),
            ]
        )
        simplified = _simplified_decision_geometry(source, 0.01)
        self.assertEqual(len(source.geoms), len(simplified.geoms))

    def test_production_result_audits_simplification_bound(self) -> None:
        profile = replace(
            load_spatial_profile(RICHNESS_PROFILE_PATH),
            decision_simplification_min_bbox_degrees2=0.0,
        )
        result = direct_any_touch_intersecting_cells_native(box(12.4, 55.5, 12.41, 55.51), profile)
        self.assertTrue(result.decision_simplification_applied)
        self.assertLess(result.decision_simplification_bound_metres, 2_000)

class SpatialBuildTests(unittest.TestCase):
    def test_common_cli_options_work_before_or_after_subcommand(self) -> None:
        for args in (
            ["--root", "/tmp/fixture", "--profile", str(RICHNESS_PROFILE_PATH), "build"],
            ["build", "--root", "/tmp/fixture", "--profile", str(RICHNESS_PROFILE_PATH)],
        ):
            parsed = build_parser().parse_args(args)
            self.assertEqual(parsed.root, Path("/tmp/fixture"))
            self.assertEqual(parsed.profile, RICHNESS_PROFILE_PATH)

    def _data_pack(self, root: Path) -> Path:
        source_dir = root / "source"
        source_dir.mkdir()
        shapefile = source_dir / "fixture.shp"
        frame = gpd.GeoDataFrame(
            {
                "id_no": [101, 101, 102, 103, 104],
                "presence": [1, 1, 2, 1, 1],
                "origin": [1, 1, 1, 3, 2],
                "seasonal": [1, 1, 1, 1, 1],
                "geometry": [
                    box(12.5680, 55.6760, 12.5682, 55.6762),
                    box(12.5680, 55.6760, 12.5682, 55.6762),
                    box(12.6, 55.6, 12.61, 55.61),
                    box(12.7, 55.7, 12.71, 55.71),
                    None,
                ],
            },
            crs="EPSG:4326",
        )
        pyogrio.write_dataframe(frame, shapefile)
        archive = root / "acquisition" / "incoming" / "iucn-spatial" / "test-1" / "fixture.zip"
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as handle:
            for member in source_dir.iterdir():
                handle.write(member, member.name)
        manifest = {
            "schema_version": 1,
            "updated_at": "2026-08-30T00:00:00Z",
            "sources": {
                "iucn-spatial": {
                    "release": "test-1",
                    "validation_status": "passed",
                    "files": [
                        {
                            "logical_name": "fixture.zip",
                            "path": str(archive.relative_to(root)),
                            "bytes": archive.stat().st_size,
                            "sha256": sha256(archive),
                        }
                    ],
                }
            },
        }
        manifest_path = root / "acquisition" / "current.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return archive

    def test_build_reconciles_rows_and_only_reuses_verified_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._data_pack(root)
            output = root / "derived"
            profile = load_spatial_profile(RICHNESS_PROFILE_PATH)
            first = build(root, output, profile, set(), force=False, workers=2)
            archive = first["archives"][0]
            self.assertEqual(archive["status"], "built")
            self.assertEqual(archive["source_rows"], 5)
            self.assertEqual(
                archive["decisions"],
                {
                    "included": 2,
                    "null_geometry": 1,
                    "origin_policy": 1,
                    "presence_policy": 1,
                },
            )
            pairs = output / "archives" / "fixture" / "res7_pairs.parquet"
            audit = output / "archives" / "fixture" / "row_audit.parquet"
            self.assertGreater(pq.read_metadata(pairs).num_rows, 0)
            self.assertEqual(pq.read_metadata(audit).num_rows, 5)
            for row in pq.read_table(audit).to_pylist():
                if row["decision"] == "included":
                    self.assertFalse(row["decision_simplification_applied"])
                    self.assertEqual(row["decision_simplification_bound_metres"], 0.0)
                else:
                    self.assertIsNone(row["decision_simplification_applied"])

            second = build(root, output, profile, set(), force=False)
            self.assertEqual(second["archives"][0]["status"], "reused")

            with pairs.open("ab") as handle:
                handle.write(b"changed")
            third = build(root, output, profile, set(), force=False)
            self.assertEqual(third["archives"][0]["status"], "built")

            finalized = finalize_relations(
                output,
                profile,
                scratch_dir=root / "scratch",
                memory_limit="256MB",
                threads=1,
                force=False,
                expected_archives={"fixture.zip"},
            )
            self.assertEqual(finalized["status"], "built")
            self.assertGreater(finalized["exact_duplicates_removed"], 0)
            self.assertLessEqual(finalized["res3_relationships"], finalized["res7_relationships"])
            reused = finalize_relations(
                output,
                profile,
                scratch_dir=root / "scratch",
                memory_limit="256MB",
                threads=1,
                force=False,
                expected_archives={"fixture.zip"},
            )
            self.assertEqual(reused["status"], "reused")

    def test_run_command_builds_through_serving_export_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._data_pack(root)
            output = root / "derived"
            arguments = [
                "run",
                "--root",
                str(root),
                "--output-root",
                str(output),
                "--profile",
                str(RICHNESS_PROFILE_PATH),
                "--workers",
                "1",
                "--threads",
                "1",
                "--memory-limit",
                "256MB",
                "--scratch-dir",
                str(root / "scratch"),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 0)
                self.assertEqual(main(arguments), 0)
            report = json.loads((output / "pipeline-report.json").read_text())
            self.assertEqual(report["stages"]["build"]["archives"][0]["status"], "reused")
            self.assertEqual(report["stages"]["finalize"]["status"], "reused")
            self.assertEqual(report["stages"]["export"]["status"], "reused")
            self.assertTrue((output / "serving/current/res7_merged_parts").is_dir())

            # A new source release with the same archive name cannot be finalized
            # using the previous release's archive output.
            manifest_path = root / "acquisition/current.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["sources"]["iucn-spatial"]["release"] = "test-2"
            manifest_path.write_text(json.dumps(manifest))
            final_args = [
                "finalize",
                "--root",
                str(root),
                "--output-root",
                str(output),
                "--profile",
                str(RICHNESS_PROFILE_PATH),
            ]
            with contextlib.redirect_stdout(io.StringIO()) as console:
                self.assertEqual(main(final_args), 1)
            self.assertIn("source or code is stale", console.getvalue())

    def test_status_does_not_create_a_missing_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "missing"
            with contextlib.redirect_stdout(io.StringIO()) as console:
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            self.assertEqual(json.loads(console.getvalue())["status"], "needs-work")
            self.assertFalse(root.exists())

    def test_build_records_applied_simplification_in_row_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._data_pack(root)
            profile = replace(
                load_spatial_profile(RICHNESS_PROFILE_PATH),
                decision_simplification_min_bbox_degrees2=0.0,
            )
            output = root / "derived"
            build(root, output, profile, set(), force=False)
            rows = pq.read_table(output / "archives/fixture/row_audit.parquet").to_pylist()
            included = [row for row in rows if row["decision"] == "included"]
            self.assertEqual(len(included), 2)
            for row in included:
                self.assertTrue(row["decision_simplification_applied"])
                self.assertAlmostEqual(row["decision_simplification_bound_metres"], 1116.9398)

    def test_points_and_hydrobasins_flow_into_the_shared_pair_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._data_pack(root)
            incoming = root / "acquisition/incoming"

            point_archive = incoming / "iucn-spatial-tables/test-1/points_fixture.zip"
            point_archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(point_archive, "w") as archive:
                archive.writestr(
                    "points.csv",
                    "id_no,presence,origin,seasonal,dec_lat,dec_long\n"
                    "201,1,1,1,55.6761,12.5683\n"
                    "202,2,1,1,55.6761,12.5683\n"
                    "203,1,1,1,91,12.5683\n",
                )

            relation_archive = incoming / "iucn-spatial-tables/test-1/hydro_fixture.zip"
            with zipfile.ZipFile(relation_archive, "w") as archive:
                archive.writestr(
                    "relations.csv",
                    "hybas_id,id_no,presence,origin,seasonal\n"
                    "1080000010,204,1,1,1\n"
                    "1080000010,204,1,1,1\n"
                    "1080000010,205,1,3,1\n",
                )

            basin_source = root / "basin-source"
            basin_source.mkdir()
            basin_shape = basin_source / "hybas_af_lev08_v1c.shp"
            pyogrio.write_dataframe(
                gpd.GeoDataFrame(
                    {"HYBAS_ID": [1080000010], "geometry": [box(12.56, 55.67, 12.57, 55.68)]},
                    crs="EPSG:4326",
                ),
                basin_shape,
            )
            basin_archive = incoming / "hydrobasins/v1c/hybas_af_lev01-12_v1c.zip"
            basin_archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(basin_archive, "w") as archive:
                for member in basin_source.iterdir():
                    archive.write(member, member.name)

            manifest_path = root / "acquisition/current.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["sources"]["iucn-spatial-tables"] = {
                "release": "test-1",
                "validation_status": "passed",
                "files": [
                    {
                        "logical_name": "points_fixture.zip",
                        "format": "point",
                        "path": str(point_archive.relative_to(root)),
                        "bytes": point_archive.stat().st_size,
                        "sha256": sha256(point_archive),
                    },
                    {
                        "logical_name": "hydro_fixture.zip",
                        "format": "hydrobasin",
                        "path": str(relation_archive.relative_to(root)),
                        "bytes": relation_archive.stat().st_size,
                        "sha256": sha256(relation_archive),
                    },
                ],
            }
            manifest["sources"]["hydrobasins"] = {
                "release": "v1c",
                "validation_status": "passed",
                "files": [
                    {
                        "logical_name": "hybas_af_lev01-12_v1c.zip",
                        "path": str(basin_archive.relative_to(root)),
                        "bytes": basin_archive.stat().st_size,
                        "sha256": sha256(basin_archive),
                    }
                ],
            }
            manifest_path.write_text(json.dumps(manifest))

            output = root / "derived"
            profile = load_spatial_profile(RICHNESS_PROFILE_PATH)
            report = build(
                root,
                output,
                profile,
                set(),
                force=False,
                workers=1,
                scratch_dir=root / "scratch",
                memory_limit="256MB",
                threads=1,
            )
            by_name = {item["logical_name"]: item for item in report["archives"]}
            self.assertEqual(by_name["points_fixture.zip"]["decisions"]["included"], 1)
            self.assertEqual(
                by_name["points_fixture.zip"]["decisions"]["presence_policy"], 1
            )
            self.assertEqual(
                by_name["points_fixture.zip"]["decisions"]["coordinate_out_of_range"],
                1,
            )
            point_pairs = pq.read_table(
                output / "archives/points_fixture/res7_pairs.parquet"
            ).to_pylist()
            self.assertEqual(
                point_pairs,
                [
                    {
                        "h3_index": h3_int.str_to_int(
                            h3.latlng_to_cell(55.6761, 12.5683, 7)
                        ),
                        "iucn_sis_id": 201,
                    }
                ],
            )
            self.assertEqual(
                by_name["hydro_fixture.zip"]["distinct_basin_species_relationships"],
                1,
            )
            hydro_pairs = pq.read_table(
                output / "archives/hydro_fixture/res7_pairs.parquet"
            )
            self.assertGreater(hydro_pairs.num_rows, 0)
            self.assertEqual(set(hydro_pairs["iucn_sis_id"].to_pylist()), {204})
            self.assertEqual(report["hydrobasin_index"]["referenced_basins"], 1)

            manifest = json.loads(manifest_path.read_text())
            records, identities = pair_stage_identities(root, manifest, profile)
            finalized = finalize_relations(
                output,
                profile,
                scratch_dir=root / "scratch",
                memory_limit="256MB",
                threads=1,
                force=False,
                expected_archives={record["logical_name"] for record in records},
                expected_identities=identities,
            )
            self.assertEqual(finalized["status"], "built")
            species = set(
                pq.read_table(output / "relations/res7_pairs.parquet")[
                    "iucn_sis_id"
                ].to_pylist()
            )
            self.assertTrue({201, 204}.issubset(species))

    def test_worker_failure_reports_geometry_error_without_publishing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._data_pack(root)
            output = root / "derived"
            profile = load_spatial_profile(RICHNESS_PROFILE_PATH)
            with patch(
                "ark_pipeline.cli.spatial_pairs._polyfill_wkb",
                return_value=GeometryWorkerFailure("invalid fixture geometry", 0.1),
            ):
                with self.assertRaisesRegex(
                    GeometryCoverageError, "failed: invalid fixture geometry"
                ):
                    build(root, output, profile, set(), force=False)
            stage = output / "archives/fixture"
            failure = json.loads((stage / "failure.json").read_text())
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["totals"]["decisions"]["geometry_failure"], 1)
            self.assertFalse((stage / "receipt.json").exists())
            self.assertFalse((stage / "res7_pairs.parquet").exists())
            self.assertFalse(list(stage.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
