from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import h3.api.basic_int as h3
import pyarrow as pa
import pyarrow.parquet as pq

from ark_pipeline.aggregation.pairs import sorted_pair_lists
from ark_pipeline.aggregation.species_lists import (
    PAIR_SCHEMA,
    RES3_FILENAME,
    export_serving_lists,
    write_receipt,
)
from ark_pipeline.cli.spatial_aggregate import aggregation_status, main
from ark_pipeline.cli.spatial_pairs import build
from ark_pipeline.runtime.provenance import sha256
from ark_pipeline.spatial.coverage import load_spatial_profile
from tests import test_spatial_pipeline as spatial_fixtures

RICHNESS_PROFILE_PATH = spatial_fixtures.RICHNESS_PROFILE_PATH


def pairs_table(rows):
    return pa.Table.from_pylist([
        {"h3_index": cell, "iucn_sis_id": species} for cell, species in rows
    ], schema=PAIR_SCHEMA)


class PairAggregationTests(unittest.TestCase):
    def test_groups_and_deduplicates_across_every_batch_boundary(self) -> None:
        cells = sorted(h3.grid_disk(h3.latlng_to_cell(55, 12, 7), 1))
        rows = [(cells[0], 1)] * 11 + [(cells[0], 2), (cells[1], -1), (cells[1], 3)]
        rows += [(cells[2], species) for species in range(23)]
        rows += [(cells[3], 99)] * 6
        expected = sorted(set(rows))
        table = pairs_table(rows)
        for size in (1, 2, 3, 7, 16, 250_000):
            tables = list(sorted_pair_lists(table.to_batches(max_chunksize=size), resolution=7, deduplicate=True))
            actual = [(r["h3_cell"], species) for r in pa.concat_tables(tables).to_pylist()
                      for species in r["species_ids"]]
            self.assertEqual(actual, expected, f"batch size {size}")

    def test_strict_stream_checks_duplicates_order_and_invalid_cells(self) -> None:
        cell = h3.latlng_to_cell(0, 0, 7)
        for rows, error in [
            ([(cell, 1), (cell, 1)], "uniqueness"),
            ([(cell, 2), (cell, 1)], "not sorted"),
            ([(h3.cell_to_parent(cell, 3), 1)], "resolution"),
            ([(cell, None)], "null"),
        ]:
            with self.assertRaisesRegex(ValueError, error):
                list(sorted_pair_lists(pairs_table(rows).to_batches(max_chunksize=1), resolution=7))

    def inputs(self, root):
        # Two fine cells sharing a parent, a second base cell, and a pentagon.
        parent = h3.latlng_to_cell(55, 12, 3)
        children = sorted(h3.cell_to_children(parent, 7))[:2]
        other = h3.latlng_to_cell(-33, 151, 7)
        pentagon = h3.get_pentagons(7)[0]
        groups = [[(children[0], 101), (children[1], 101), (other, 102), (pentagon, 103)],
                  [(children[0], 101), (children[0], 104), (other, 102)]]
        inputs = []
        for index, rows in enumerate(groups):
            path = root / f"archive-{index}.parquet"
            pq.write_table(pairs_table(rows), path)
            inputs.append({"logical_name": path.name, "path": path, "rows": len(rows),
                           "bytes": path.stat().st_size, "sha256": sha256(path)})
        return inputs, set(sum(groups, []))

    def export(self, root, inputs, version=1):
        return export_serving_lists(
            root, scratch_dir=root / "scratch", memory_limit="128MB", threads=1,
            archive_inputs=inputs, archive_identity={"test_generation": version},
        )

    def test_direct_export_is_lossless_without_global_intermediates_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, expected = self.inputs(root)
            report = self.export(root, inputs)
            self.assertEqual(report["archive_pair_rows"], 7)
            self.assertEqual(report["exact_duplicates_removed"], 2)
            current = root / "serving/current"
            with duckdb.connect() as connection:
                fine = set(connection.execute(
                    "SELECT h3_cell, unnest(species_ids) FROM read_parquet(?)",
                    [str(current / "res7_merged_parts/base_*.parquet")],
                ).fetchall())
                coarse = set(connection.execute(
                    "SELECT h3_cell, unnest(species_ids) FROM read_parquet(?)",
                    [str(current / RES3_FILENAME)],
                ).fetchall())
            self.assertEqual(fine, expected)
            self.assertEqual(coarse, {(h3.cell_to_parent(cell, 3), species) for cell, species in expected})
            self.assertFalse((root / "relations").exists())
            self.assertFalse((current / "pair-partitions").exists())
            self.assertEqual(self.export(root, inputs)["status"], "reused")

            previous = current.resolve()
            def interrupt(path, *args):
                if path.name == "receipt.json":
                    raise RuntimeError("interrupted")
                return write_receipt(path, *args)
            with patch("ark_pipeline.aggregation.species_lists.write_receipt", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    self.export(root, inputs, version=2)
            self.assertEqual(current.resolve(), previous)
            # Missing intermediate partitions must rebuild, even if the remaining
            # files still have valid hashes. Completed final lists remain reusable.
            interrupted = next(p for p in (root / "serving/generations").iterdir() if p.resolve() != previous)
            next((interrupted / "pair-partitions").glob("base_cell=*/data_*.parquet")).unlink()
            resumed = self.export(root, inputs, version=2)
            self.assertEqual(resumed["reused_partitions"], 6)
            self.assertNotEqual(current.resolve(), previous)

    def test_command_builds_from_verified_pairs_and_detects_stale_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spatial_fixtures.SpatialBuildTests()._data_pack(root)
            output = root / "derived"
            profile = load_spatial_profile(RICHNESS_PROFILE_PATH)
            build(root, output, profile, set(), force=False, workers=1)
            before = aggregation_status(root, output, profile)
            self.assertEqual(before["serving_lists"], "missing")
            self.assertEqual(before["relations"], "not-required-for-direct-aggregation")
            self.assertEqual(before["next_command"], "just data-aggregate")
            args = ["--root", str(root), "--output-root", str(output), "--profile", str(RICHNESS_PROFILE_PATH),
                    "--memory-limit", "128MB", "--threads", "1", "--scratch-dir", str(root / "scratch")]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads((output / "aggregation-report.json").read_text())["status"], "reused")
            status = aggregation_status(root, output, profile)
            self.assertEqual(status["serving_lists"], "present-unverified")
            self.assertEqual(status["relations"], "not-required-for-direct-aggregation")
            manifest_path = root / "acquisition/current.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["sources"]["iucn-spatial"]["release"] = "changed"
            manifest_path.write_text(json.dumps(manifest))
            previous = (output / "serving/current").resolve()
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(main(args), 1)
            self.assertIn("source or code is stale", captured.getvalue())
            self.assertEqual((output / "serving/current").resolve(), previous)
            self.assertEqual(aggregation_status(root, output, profile)["aggregation"], "blocked-by-archives")


if __name__ == "__main__":
    unittest.main()
