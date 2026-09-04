from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ark_pipeline.cli.serving_metadata import source_file, validate_crosswalk_sources
from ark_pipeline.runtime.provenance import sha256


class GlobalMetadataInputTests(unittest.TestCase):
    def test_registered_snapshot_wins_and_corruption_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot.csv"
            fallback = root / "legacy.csv"
            snapshot.write_text("new")
            fallback.write_text("old")
            manifest = {
                "sources": {
                    "iucn-red-list-tabular": {
                        "validation_status": "passed",
                        "files": [
                            {
                                "logical_name": "assessments.csv",
                                "path": "snapshot.csv",
                                "bytes": 3,
                                "sha256": sha256(snapshot),
                            }
                        ],
                    }
                }
            }
            self.assertEqual(
                source_file(root, manifest, "iucn-red-list-tabular", "assessments.csv", fallback),
                snapshot.resolve(),
            )
            snapshot.write_text("bad")
            with self.assertRaisesRegex(ValueError, "checksum changed"):
                source_file(root, manifest, "iucn-red-list-tabular", "assessments.csv", fallback)
            self.assertEqual(
                source_file(root, {}, "iucn-red-list-tabular", "assessments.csv", fallback),
                fallback,
            )

    def test_crosswalk_must_match_registered_source_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crosswalk = root / "iucn_goat_crosswalk.parquet"
            manifest = {
                "sources": {
                    "goat-species": {
                        "files": [{"logical_name": "tol_species_all_ranks.tsv", "sha256": "new"}]
                    }
                }
            }
            with self.assertRaisesRegex(ValueError, "provenance is missing"):
                validate_crosswalk_sources(crosswalk, manifest)
            summary = root / "match_summary.json"
            summary.write_text(json.dumps({"sources": {"goat_species": {"sha256": "old"}}}))
            with self.assertRaisesRegex(ValueError, "crosswalk is stale"):
                validate_crosswalk_sources(crosswalk, manifest)
            summary.write_text(json.dumps({"sources": {"goat_species": {"sha256": "new"}}}))
            validate_crosswalk_sources(crosswalk, manifest)


if __name__ == "__main__":
    unittest.main()
