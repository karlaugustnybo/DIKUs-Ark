from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import h3.api.numpy_int as h3
import pyarrow as pa
import pyarrow.parquet as pq

from ark_pipeline.aggregation.species_lists import (
    PAIR_SCHEMA,
    RES3_FILENAME,
    export_serving_lists,
    write_receipt,
)


class SpatialServingTests(unittest.TestCase):
    def make_relations(
        self, root: Path, *, version: int = 1, duplicate: bool = False
    ) -> set[tuple[int, int]]:
        cells = [
            int(h3.latlng_to_cell(55.6761, 12.5683, 7)),
            int(h3.latlng_to_cell(-33.8688, 151.2093, 7)),
        ]
        rows = [(cells[0], 101), (cells[0], 102), (cells[1], 102)]
        if duplicate:
            rows.append(rows[0])
        res3 = sorted({(int(h3.cell_to_parent(cell, 3)), species) for cell, species in rows})
        directory = root / "relations"
        directory.mkdir(exist_ok=True)
        outputs = {}
        for name, values in [("res7", rows), ("res3", res3)]:
            path = directory / f"{name}_pairs.parquet"
            pq.write_table(
                pa.Table.from_pylist(
                    [{"h3_index": cell, "iucn_sis_id": species} for cell, species in values],
                    schema=PAIR_SCHEMA,
                ),
                path,
            )
            outputs[name] = path
        write_receipt(
            directory / "receipt.json",
            {"fixture_version": version},
            outputs,
            {"res7_relationships": len(rows), "res3_relationships": len(res3)},
        )
        return set(rows)

    def export(self, root: Path) -> dict:
        return export_serving_lists(
            root, scratch_dir=root / "scratch", memory_limit="256MB", threads=1
        )

    def test_exports_lossless_sorted_lists_in_existing_serving_format_and_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = self.make_relations(root)
            report = self.export(root)
            self.assertEqual(report["status"], "built")
            current = root / "serving/current"
            self.assertTrue(current.is_symlink())
            self.assertTrue((current / RES3_FILENAME).is_file())
            paths = list((current / "res7_merged_parts").glob("base_*.parquet"))
            self.assertEqual(len(paths), 2)
            with duckdb.connect() as connection:
                actual = set(
                    connection.execute(
                        "SELECT h3_cell, unnest(species_ids) FROM read_parquet(?)",
                        [[str(p) for p in paths]],
                    ).fetchall()
                )
            self.assertEqual(actual, expected)
            for path in paths:
                base = int(path.stem.removeprefix("base_"))
                for row in pq.read_table(path).to_pylist():
                    self.assertEqual((row["h3_cell"] >> 45) & 127, base)
                    self.assertEqual(row["species_ids"], sorted(set(row["species_ids"])))
            previous = current.resolve()
            self.assertEqual(self.export(root)["status"], "reused")
            self.assertEqual(current.resolve(), previous)

    def test_interrupted_export_resumes_partitions_and_preserves_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_relations(root)
            self.export(root)
            current = root / "serving/current"
            previous = current.resolve()
            self.make_relations(root, version=2)

            def fail_before_publication(path, *args):
                if path.name == "receipt.json":
                    raise RuntimeError("interrupted before publication")
                return write_receipt(path, *args)

            with patch("ark_pipeline.aggregation.species_lists.write_receipt", side_effect=fail_before_publication):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    self.export(root)
            self.assertEqual(current.resolve(), previous)
            resumed = self.export(root)
            self.assertEqual(resumed["reused_partitions"], 3)
            self.assertNotEqual(current.resolve(), previous)
            self.assertTrue(previous.is_dir())

    def test_rejects_duplicate_pairs_without_replacing_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_relations(root)
            self.export(root)
            previous = (root / "serving/current").resolve()
            self.make_relations(root, version=2, duplicate=True)
            with self.assertRaisesRegex(ValueError, "uniqueness"):
                self.export(root)
            self.assertEqual((root / "serving/current").resolve(), previous)

    def test_corrupt_finalized_pairs_are_rejected_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_relations(root)
            with (root / "relations/res7_pairs.parquet").open("ab") as handle:
                handle.write(b"corrupt")
            with self.assertRaisesRegex(ValueError, "stale or corrupt"):
                self.export(root)
            self.assertFalse((root / "serving").exists())


if __name__ == "__main__":
    unittest.main()
