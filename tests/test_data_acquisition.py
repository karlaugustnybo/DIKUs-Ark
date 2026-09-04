from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from ark_pipeline.cli.sources_acquire import (
    Catalogue,
    Source,
    doctor,
    files_from_inventory_directory,
    is_due,
    load_catalogue,
    load_manifest,
    register_manual,
    required_file_names,
    source_inventory,
    targets,
    update_public,
)
from ark_pipeline.cli.sources_download_iucn import download_catalogue, kingdom_name, only_list
from ark_pipeline.cli.sources_sync import SourceStatus, sync_restricted_source
from ark_pipeline.cli.sources_sync import main as sync_main


class DataAcquisitionTests(unittest.TestCase):
    def test_incomplete_acquisition_stops_chained_builds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for complete, expected_exit in ((False, 2), (True, 0)):
                statuses = [
                    SourceStatus(
                        "iucn-spatial", "current" if complete else "authorization-required"
                    )
                ]
                with unittest.mock.patch(
                    "ark_pipeline.cli.sources_sync.synchronize", return_value=(statuses, complete)
                ):
                    with unittest.mock.patch("builtins.print"):
                        self.assertEqual(
                            sync_main(["download", "--root", str(root)]), expected_exit
                        )
                self.assertEqual(
                    json.loads((root / "acquisition/action-required.json").read_text())["complete"],
                    complete,
                )

    def test_catalogue_keeps_edge_out_of_required_profiles(self) -> None:
        catalogue = load_catalogue(Path("config/data_sources.toml"))
        self.assertNotIn("edge-species", catalogue.sources)
        for profile in catalogue.profiles.values():
            self.assertNotIn("edge-species", profile["required_sources"])

    def test_iucn_spatial_inventory_is_complete_and_versioned(self) -> None:
        catalogue = load_catalogue(Path("config/data_sources.toml"))
        source = catalogue.sources["iucn-spatial"]
        inventory = source_inventory(source)
        self.assertIsNotNone(inventory)
        assert inventory is not None
        files = inventory["files"]
        self.assertEqual(inventory["red_list_version"], "2026-1")
        self.assertEqual(len(files), 30)
        self.assertEqual(len({item["file_id"] for item in files}), len(files))
        self.assertEqual(len({item["provider_filename"] for item in files}), len(files))
        self.assertEqual(required_file_names(source), {item["logical_name"] for item in files})
        self.assertEqual(
            {item["bucket"] for item in files},
            {
                "amphibians",
                "fishes",
                "freshwater_groups",
                "mammals",
                "marine_groups",
                "plants",
                "reptiles",
            },
        )

    def test_iucn_spatial_table_inventory_covers_points_and_hydrobasins(self) -> None:
        catalogue = load_catalogue(Path("config/data_sources.toml"))
        source = catalogue.sources["iucn-spatial-tables"]
        inventory = source_inventory(source)
        self.assertIsNotNone(inventory)
        assert inventory is not None
        files = inventory["files"]
        self.assertEqual(inventory["red_list_version"], "2026-1")
        self.assertEqual(len(files), 31)
        self.assertEqual({item["format"] for item in files}, {"point", "hydrobasin"})
        self.assertEqual(sum(item["format"] == "point" for item in files), 17)
        self.assertEqual(sum(item["format"] == "hydrobasin" for item in files), 14)
        self.assertEqual(len({item["file_id"] for item in files}), len(files))
        self.assertEqual(len({item["provider_filename"] for item in files}), len(files))
        self.assertEqual(required_file_names(source), {item["logical_name"] for item in files})

    def test_targets_can_select_only_one_spatial_format(self) -> None:
        catalogue = load_catalogue(Path("config/data_sources.toml"))
        args = argparse.Namespace(source="iucn-spatial-tables", format=["point"])
        with unittest.mock.patch("builtins.print") as output:
            self.assertEqual(targets(args, catalogue), 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(len(payload["files"]), 17)
        self.assertEqual({item["format"] for item in payload["files"]}, {"point"})

    def test_hydrobasins_inventory_is_complete_and_release_aware(self) -> None:
        catalogue = load_catalogue(Path("config/data_sources.toml"))
        source = catalogue.sources["hydrobasins"]
        inventory = source_inventory(source)
        self.assertIsNotNone(inventory)
        assert inventory is not None
        files = inventory["files"]
        self.assertEqual(inventory["release"], "v1c")
        self.assertEqual(len(files), 9)
        self.assertEqual(
            {item["region"] for item in files},
            {
                "Africa",
                "Arctic",
                "Asia",
                "Australia",
                "Europe",
                "Greenland",
                "North America",
                "South America",
                "Siberia",
            },
        )
        self.assertFalse(is_due(source, {"release": "v1c"}, False))
        self.assertTrue(is_due(source, {"release": "v1b"}, False))
        self.assertTrue(is_due(source, {"release": "v1c"}, True))

    def test_manual_registration_copies_validates_and_detects_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assessments = root / "incoming-assessments.csv"
            taxonomy = root / "incoming-taxonomy.csv"
            assessments.write_text(
                "assessmentId,internalTaxonId,scientificName,redlistCategory,systems\n"
                "10,20,Example species,Least Concern,Terrestrial\n",
                encoding="utf-8",
            )
            taxonomy.write_text(
                "internalTaxonId,scientificName,kingdomName,familyName,genusName,speciesName\n"
                "20,Example species,ANIMALIA,Exampleidae,Example,species\n",
                encoding="utf-8",
            )
            catalogue = load_catalogue(Path("config/data_sources.toml"))
            args = argparse.Namespace(
                root=root,
                source="iucn-red-list-tabular",
                release="test-1",
                file=[f"assessments.csv={assessments}", f"taxonomy.csv={taxonomy}"],
                authorized=True,
                reference=False,
            )
            self.assertEqual(register_manual(args, catalogue), 0)
            manifest = load_manifest(root)
            record = manifest["sources"]["iucn-red-list-tabular"]
            self.assertEqual(record["release"], "test-1")
            self.assertEqual(record["validation_status"], "passed")

            minimal = Catalogue(
                sources={"iucn-red-list-tabular": catalogue.sources["iucn-red-list-tabular"]},
                profiles={"test": {"required_sources": ["iucn-red-list-tabular"]}},
            )
            doctor_args = argparse.Namespace(root=root, profile="test", deep=True, output=None)
            self.assertEqual(doctor(doctor_args, minimal), 0)
            copied = root / record["files"][0]["path"]
            copied.write_text("changed\n", encoding="utf-8")
            self.assertEqual(doctor(doctor_args, minimal), 1)

    def test_inventory_directory_maps_provider_to_logical_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inventory_path = root / "inventory.toml"
            inventory_path.write_text(
                'schema_version = 1\nred_list_version = "test-1"\n'
                '[[files]]\nlogical_name = "logical-a.zip"\n'
                'provider_filename = "PROVIDER_A.zip"\n'
                '[[files]]\nlogical_name = "logical-b.zip"\n'
                'provider_filename = "PROVIDER_B.zip"\n',
                encoding="utf-8",
            )
            source = Source(
                {
                    "id": "inventory-source",
                    "inventory_file": str(inventory_path),
                }
            )
            incoming = root / "incoming"
            incoming.mkdir()
            expected = {
                "logical-a.zip": incoming / "PROVIDER_A.zip",
                "logical-b.zip": incoming / "PROVIDER_B.zip",
            }
            self.assertEqual(files_from_inventory_directory(source, incoming), expected)

    def test_public_tar_download_is_snapshotted_with_selected_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_directory = root / "remote"
            source_directory.mkdir()
            for name in ("names.dmp", "nodes.dmp"):
                (source_directory / name).write_text(name, encoding="utf-8")
            archive = source_directory / "taxdump.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                handle.add(source_directory / "names.dmp", arcname="names.dmp")
                handle.add(source_directory / "nodes.dmp", arcname="nodes.dmp")
            source = Source(
                {
                    "id": "test-download",
                    "provider": "Test",
                    "access": "public-download",
                    "update_policy": "monthly",
                    "interval_days": 28,
                    "download_url": archive.as_uri(),
                    "output_file": "taxdump.tar.gz",
                    "extract_members": ["names.dmp", "nodes.dmp"],
                }
            )
            catalogue = Catalogue(sources={source.id: source}, profiles={})
            args = argparse.Namespace(
                root=root,
                source=[source.id],
                force=False,
                dry_run=False,
            )
            self.assertEqual(update_public(args, catalogue), 0)
            files = load_manifest(root)["sources"][source.id]["files"]
            self.assertEqual(
                {item["logical_name"] for item in files},
                {"taxdump.tar.gz", "names.dmp", "nodes.dmp"},
            )

    def test_release_inventory_downloads_once_then_stays_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            remote = root / "remote"
            remote.mkdir()
            archives = []
            for region in ("aa", "bb"):
                archive = remote / f"hybas_{region}_v1c.zip"
                with zipfile.ZipFile(archive, "w") as handle:
                    for suffix in (".shp", ".dbf", ".shx"):
                        handle.writestr(f"hybas_{region}{suffix}", suffix)
                archives.append(archive)
            inventory_path = root / "hydrobasins.toml"
            inventory_path.write_text(
                'schema_version = 1\nrelease = "v1c"\n'
                + "".join(
                    "[[files]]\n"
                    f'logical_name = "{archive.name}"\n'
                    f'provider_filename = "{archive.name}"\n'
                    f"expected_bytes = {archive.stat().st_size}\n"
                    f'download_url = "{archive.as_uri()}"\n'
                    for archive in archives
                ),
                encoding="utf-8",
            )
            source = Source(
                {
                    "id": "test-hydrobasins",
                    "provider": "Test",
                    "access": "public-download",
                    "update_policy": "when-provider-releases",
                    "inventory_file": str(inventory_path),
                    "allowed_suffixes": [".zip"],
                    "archive_required_suffixes": [".shp", ".dbf", ".shx"],
                }
            )
            catalogue = Catalogue(sources={source.id: source}, profiles={})
            args = argparse.Namespace(
                root=root,
                source=[source.id],
                force=False,
                dry_run=False,
            )
            self.assertEqual(update_public(args, catalogue), 0)
            record = load_manifest(root)["sources"][source.id]
            self.assertEqual(record["release"], "v1c")
            self.assertEqual(len(record["files"]), 2)
            with unittest.mock.patch(
                "ark_pipeline.cli.sources_acquire.download",
                side_effect=AssertionError("current release must not download"),
            ):
                self.assertEqual(update_public(args, catalogue), 0)

    def test_sync_registers_staged_manual_source_only_after_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = Source(
                {
                    "id": "restricted-test",
                    "provider": "Test",
                    "access": "manual",
                    "update_policy": "when-provider-releases",
                    "release": "test-1",
                    "required_files": ["data.csv"],
                }
            )
            incoming = root / "acquisition" / "incoming" / source.id / "test-1"
            incoming.mkdir(parents=True)
            (incoming / "data.csv").write_text("value\n1\n", encoding="utf-8")

            pending = sync_restricted_source(root, source, authorized=False)
            self.assertEqual(pending.status, "authorization-required")
            self.assertNotIn(source.id, load_manifest(root)["sources"])

            registered = sync_restricted_source(root, source, authorized=True)
            self.assertEqual(registered.status, "registered")
            self.assertEqual(load_manifest(root)["sources"][source.id]["release"], "test-1")

    @unittest.skipUnless(shutil.which("just") and shutil.which("uv"), "just and uv required")
    def test_combined_workflows_acquire_sources_before_the_complete_build(self) -> None:
        justfile = Path("justfile").read_text(encoding="utf-8")
        self.assertIn("\ndownload:\n", justfile)
        self.assertIn("\nupdate:\n", justfile)
        with tempfile.TemporaryDirectory() as temporary:
            for recipe, mode in (("data-build", "download"), ("data-update", "update")):
                completed = subprocess.run(
                    ["just", recipe, "--root", temporary, "--workers", "3", "--dry-run"],
                    check=True, capture_output=True, text=True,
                    env={**os.environ, "UV_NO_SYNC": "1", "UV_CACHE_DIR": str(Path(temporary) / "uv-cache")},
                )
                plan = json.loads(completed.stdout)
                self.assertEqual(plan["commands"][0][1:4], ["-m", "ark_pipeline.cli.sources_sync", mode])
                self.assertEqual(plan["commands"][-1], ["just", "data-tiles"])
                self.assertEqual(plan["resources"]["workers"], 3)
                self.assertFalse((Path(temporary) / "derived").exists())
        for old_recipe in (
            "data-plan:",
            "data-iucn-targets:",
            "data-iucn-table-targets",
            "data-hydrobasins-targets:",
            "data-doctor",
        ):
            self.assertNotIn(old_recipe, justfile)


class FakeIucnApi:
    base_url = "https://example.test/api/v4"

    def get(self, endpoint: str, query=None):
        if endpoint == "information/red_list_version":
            return {"red_list_version": "2099-1"}, {}
        if endpoint == "taxa/kingdom":
            return {"kingdoms": [{"name": "ANIMALIA"}, {"name": "PLANTAE"}]}, {}
        if endpoint.endswith("ANIMALIA"):
            return {"assessments": [{"assessment_id": 1}]}, {"total-count": "1"}
        if endpoint.endswith("PLANTAE"):
            return {"assessments": [{"assessment_id": 2}]}, {"total-count": "1"}
        raise AssertionError(endpoint)


class IucnCatalogTests(unittest.TestCase):
    def test_response_helpers_fail_closed_on_ambiguous_shapes(self) -> None:
        self.assertEqual(only_list({"assessments": [1]}, ("assessments",)), [1])
        self.assertEqual(kingdom_name({"kingdom_name": "FUNGI"}), "FUNGI")
        with self.assertRaises(ValueError):
            only_list({"one": [], "two": []})

    def test_catalogue_download_records_release_and_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "catalog.jsonl"
            metadata = root / "iucn_assessment_catalog.metadata.json"
            result = download_catalogue(
                FakeIucnApi(), output, metadata, previous=None, previous_metadata=None
            )
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(rows, [{"assessment_id": 1}, {"assessment_id": 2}])
            self.assertEqual(result["red_list_version"], "2099-1")
            self.assertEqual(result["rows"], 2)
            self.assertEqual(sum(result["rows_by_kingdom"].values()), result["rows"])
            self.assertEqual(json.loads(metadata.read_text())["status"], "complete")


if __name__ == "__main__":
    unittest.main()
