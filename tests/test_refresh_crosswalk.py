from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from ark_pipeline.cli.crosswalk_refresh import INPUTS, refresh
from ark_pipeline.runtime.provenance import atomic_json, sha256


def fixture(root: Path) -> None:
    sources = root / "sources"
    sources.mkdir()
    taxonomy_fields = ["internalTaxonId", "scientificName", "kingdomName", "phylumName", "className",
                       "orderName", "familyName", "genusName", "speciesName", "authority"]
    with (sources / "taxonomy.csv").open("w") as handle:
        writer = csv.writer(handle)
        writer.writerow(taxonomy_fields)
        for sis, name in [(101, "Panthera leo"), (102, "Felis tigris"),
                          (103, "Panthera ambigua"), (104, "Panthera fakeus")]:
            writer.writerow([sis, name, "Animalia", "Chordata", "Mammalia", "Carnivora", "Felidae",
                             "Panthera", name.split()[1], "Fixture"])
    with (sources / "assessments.csv").open("w") as handle:
        writer = csv.writer(handle)
        writer.writerow(["internalTaxonId", "assessmentId", "redlistCategory", "yearPublished", "assessmentDate", "scopes"])
        for sis in range(101, 105):
            writer.writerow([sis, sis + 1000, "VU", 2026, "2026-01-01", "Global"])
    goat_fields = ["taxon_id", "scientific_name", "taxon_rank", "assembly_level", "bioproject",
                   "busco_completeness", "ebp_standard_criteria", "in_progress", "insdc_submitted",
                   "published", "resampling_required", "sample_acquired", "sample_available",
                   "sample_collected", "sequencing_status", "sequencing_status_ebp", "other_priority", "family_representative"]
    with (sources / "tol_species_all_ranks.tsv").open("w") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(goat_fields)
        for taxid, name, rank in [(10, "Panthera leo", "species"), (11, "Panthera tigris", "species"),
                                  (12, "Panthera ambigua", "species"), (13, "Panthera ambigua", "species"),
                                  (20, "Panthera fakeus", "subspecies")]:
            writer.writerow([taxid, name, rank] + [""] * (len(goat_fields) - 3))
    nodes = [(1, 1, "no rank", "root"), (2, 1, "kingdom", "Animalia"), (3, 2, "phylum", "Chordata"),
             (4, 3, "class", "Mammalia"), (5, 4, "order", "Carnivora"), (6, 5, "family", "Felidae"),
             (7, 6, "genus", "Panthera"), (10, 7, "species", "Panthera leo"),
             (11, 7, "species", "Panthera tigris"), (12, 7, "species", "Panthera ambigua"),
             (13, 7, "species", "Panthera ambigua"), (20, 10, "subspecies", "Panthera fakeus")]
    (sources / "nodes.dmp").write_text("".join(
        "\t|\t".join([str(taxid), str(parent), rank] + [""] * 10) + "\t|\n"
        for taxid, parent, rank, _ in nodes
    ))
    (sources / "names.dmp").write_text("".join(
        f"{taxid}\t|\t{name}\t|\t\t|\tscientific name\t|\n" for taxid, _, _, name in nodes
    ) + "11\t|\tFelis tigris\t|\t\t|\tsynonym\t|\n")
    (sources / "merged.dmp").write_text("99\t|\t10\t|\n")
    (sources / "delnodes.dmp").write_text("98\t|\n")
    register(root)


def register(root: Path) -> None:
    manifest = {"schema_version": 1, "sources": {}}
    for source_id, logical_name, _ in INPUTS:
        path = root / "sources" / logical_name
        source = manifest["sources"].setdefault(source_id, {"validation_status": "passed", "release": "fixture", "files": []})
        source["files"].append({"logical_name": logical_name, "path": str(path.relative_to(root)),
                                "bytes": path.stat().st_size, "sha256": sha256(path)})
    atomic_json(root / "acquisition/current.json", manifest)


class RefreshCrosswalkTests(unittest.TestCase):
    def test_real_matcher_preserves_identities_unresolved_cases_and_reuses_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            output = root / "crosswalk"
            first = refresh(root, output, memory_limit="256MB", threads=1)
            self.assertEqual(first["status"], "built")
            self.assertEqual(first["counts"], {"iucn_taxa": 4, "MATCHED": 2,
                                              "REVIEW_UNRESOLVED": 1, "NO_GOAT_NCBI_CANDIDATE": 1})
            with duckdb.connect() as con:
                rows = con.execute("SELECT iucn_sis_id, matched_ncbi_species_taxid, match_status FROM read_parquet(?) ORDER BY 1",
                                   [first["crosswalk"]]).fetchall()
            self.assertEqual(rows, [(101, "10", "MATCHED"), (102, "11", "MATCHED"),
                                    (103, None, "REVIEW_UNRESOLVED"), (104, None, "NO_GOAT_NCBI_CANDIDATE")])
            with patch("ark_pipeline.cli.crosswalk_refresh.subprocess.run") as run:
                second = refresh(root, output, memory_limit="256MB", threads=1)
                run.assert_not_called()
            self.assertEqual(second["status"], "reused")
            self.assertEqual(first["crosswalk"], second["crosswalk"])
            summary = json.loads(Path(first["crosswalk"]).with_name("match_summary.json").read_text())
            self.assertTrue(summary["automatic_refresh"])
            self.assertTrue(all(Path(path).is_file() for path in summary["outputs"].values()))

            # A newly registered snapshot triggers a refresh; failure keeps the
            # complete previous generation selected and removes partial work.
            path = root / "sources/tol_species_all_ranks.tsv"
            path.write_text(path.read_text().replace("Panthera leo\t", "Panthera leo updated\t"))
            register(root)
            old_current = (output / "current").resolve()
            with patch("ark_pipeline.cli.crosswalk_refresh.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["matcher"])):
                with self.assertRaises(subprocess.CalledProcessError):
                    refresh(root, output, memory_limit="256MB", threads=1)
            self.assertEqual((output / "current").resolve(), old_current)
            self.assertFalse(list((output / "generations").glob(".building-*")))
            third = refresh(root, output, memory_limit="256MB", threads=1)
            self.assertEqual(third["status"], "built")
            self.assertNotEqual(first["crosswalk"], third["crosswalk"])
            self.assertTrue(Path(first["crosswalk"]).is_file())

            # Missing or modified published files cannot be reused.
            Path(third["crosswalk"]).with_name("unresolved_candidates.parquet").unlink()
            with patch("ark_pipeline.cli.crosswalk_refresh.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["matcher"])) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    refresh(root, output, memory_limit="256MB", threads=1)
                run.assert_called_once()

    def test_duplicate_source_identities_fail_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            taxonomy = root / "sources/taxonomy.csv"
            taxonomy.write_text(taxonomy.read_text() + taxonomy.read_text().splitlines()[1] + "\n")
            register(root)
            with self.assertRaisesRegex(ValueError, "reconciliation"):
                refresh(root, root / "crosswalk", memory_limit="256MB", threads=1)
            self.assertFalse((root / "crosswalk/current").exists())


if __name__ == "__main__":
    unittest.main()
