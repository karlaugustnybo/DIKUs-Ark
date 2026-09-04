from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ark_pipeline.cli.boundaries_prepare import build, normalize
from ark_pipeline.cli.sources_download_adm2 import acquire_country, validate_country
from ark_pipeline.runtime.provenance import sha256


def country(iso="DNK", units=2):
    return {"iso": iso, "name": iso, "area_type": "Municipality", "year": "2023",
            "units": units, "boundary_id": f"{iso}-ADM2-test", "source_license": "CC BY 4.0",
            "download_url": f"https://example.test/{iso}-simplified.json",
            "full_geometry_url": f"https://example.test/{iso}-full.json"}


def features(iso="DNK", count=2):
    return [{"type": "Feature", "properties": {"shapeID": f"{iso}-{index}",
             "shapeName": f"Area {index}", "shapeGroup": iso, "shapeType": "ADM2"},
             "geometry": {"type": "Polygon", "coordinates": [[[index, 0], [index + 1, 0],
                 [index + 1, 1], [index, 1], [index, 0]]]}}
            for index in range(count)]


def geojson(rows):
    return json.dumps({"type": "FeatureCollection", "features": rows}).encode()


class ADM2AcquisitionTests(unittest.TestCase):
    def test_stale_provider_count_is_audited_against_full_ids_and_reused(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            item = country(units=3)  # API count is stale; both pinned files contain two IDs.

            def fetch(url, target):
                target.write_bytes(geojson(features()))

            with patch("ark_pipeline.cli.sources_download_adm2.fetch", side_effect=fetch) as download:
                result = acquire_country(item, root)
                self.assertEqual(download.call_count, 2)
            self.assertEqual(result["units"], 2)
            self.assertEqual(result["count_audit"]["provider_reported_units"], 3)
            self.assertEqual(result["count_audit"]["restored_ids"], [])
            with patch("ark_pipeline.cli.sources_download_adm2.fetch", side_effect=AssertionError("must reuse")):
                self.assertTrue(acquire_country(item, root)["reused"])

    def test_simplification_cannot_drop_an_administrative_area(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            item = country()

            def fetch(url, target):
                target.write_bytes(geojson(features(count=1 if "simplified" in url else 2)))

            with patch("ark_pipeline.cli.sources_download_adm2.fetch", side_effect=fetch):
                result = acquire_country(item, root)
            self.assertEqual(result["count_audit"]["restored_ids"], ["DNK-1"])
            self.assertEqual(validate_country(root / "DNK.geojson", item)["units"], 2)

    def test_wrong_level_duplicates_and_lost_geometry_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            rows = features()
            rows[0]["properties"]["shapeType"] = "ADM1"
            path.write_bytes(geojson(rows))
            with self.assertRaisesRegex(ValueError, "administrative level"):
                validate_country(path, country())
            rows = features()
            rows[1]["properties"]["shapeID"] = rows[0]["properties"]["shapeID"]
            path.write_bytes(geojson(rows))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                validate_country(path, country())
            rows = features()
            rows[0]["geometry"] = None
            path.write_bytes(geojson(rows))
            with self.assertRaisesRegex(ValueError, "polygon"):
                validate_country(path, country())


class ADM2BuildTests(unittest.TestCase):
    def fixture(self, root, iso="DNK", parent_code="DNK"):
        archive = root / "source.zip"
        source = root / "source.json"
        source.write_bytes(geojson(features(iso)))
        record = {"country": country(iso), **validate_country(source, country(iso))}
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("metadata.json", json.dumps({"release": "test", "geometry": "provider-simplified",
                              "countries": {iso: record}, "units": 2}))
            package.write(source, f"{iso}.geojson")
        static = root / "static"
        (static / "boundary-catalogs").mkdir(parents=True)
        (static / "boundary-catalogs/admin0.json").write_text(json.dumps({"features": [{"code": parent_code}, {"code": "USA"}]}))
        manifest = {"sources": {"geoboundaries-adm2": {"validation_status": "passed", "files": [{
                    "logical_name": "geoboundaries-adm2.zip", "path": str(archive),
                    "bytes": archive.stat().st_size, "sha256": sha256(archive)}]}}}
        return archive, static, manifest

    def test_source_country_codes_match_the_existing_country_selector(self):
        for iso, parent in [("PSE", "PSX"), ("SSD", "SDS"), ("XKX", "KOS")]:
            with self.subTest(iso=iso), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, static, manifest = self.fixture(root, iso, parent)
                with patch("ark_pipeline.cli.boundaries_prepare.load_manifest", return_value=manifest):
                    report = build(root, root / "output", static)
                self.assertEqual(report["unavailable_country_codes"], ["USA"])
                self.assertEqual(report["countries"][iso]["parent_code"], parent)
                catalogues = static / "adm2-catalogs"
                self.assertEqual(json.loads((catalogues / "framework.json").read_text())["available_parent_codes"], [parent])
                rows = json.loads((catalogues / f"{parent.lower()}.json").read_text())["features"]
                self.assertTrue(all(row["parent_code"] == parent for row in rows))
                self.assertEqual(rows[0]["code"], f"{iso}-0")

    def test_publishes_geometry_and_small_catalogues_with_explicit_missing_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, static, manifest = self.fixture(root)
            output = root / "output"
            with patch("ark_pipeline.cli.boundaries_prepare.load_manifest", return_value=manifest):
                report = build(root, output, static)
                self.assertEqual(report["area_count"], 2)
                self.assertEqual(report["unavailable_country_codes"], ["USA"])
                asset = static / "adm2-catalogs"
                self.assertTrue(asset.is_symlink())
                catalogue = json.loads((asset / "dnk.json").read_text())
                self.assertTrue(all("geometry" not in row for row in catalogue["features"]))
                self.assertEqual(json.loads((asset / "usa.json").read_text())["coverage_status"], "unavailable")
                self.assertTrue(build(root, output, static)["reused"])
                old = (output / "current").resolve()
                (asset / "dnk.json").write_text("broken")
                self.assertFalse(build(root, output, static)["reused"])
                self.assertNotEqual((output / "current").resolve(), old)
                self.assertTrue(build(root, output, static)["reused"])
                previous = (output / "current").resolve()
                archive.write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    build(root, output, static)
                self.assertEqual((output / "current").resolve(), previous)

    def test_repairs_invalid_polygons_without_dropping_the_unit(self):
        rows = features(count=1)
        rows[0]["geometry"]["coordinates"] = [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]
        result, repairs = normalize(rows, country(units=1))
        self.assertEqual(len(result), 1)
        self.assertEqual(repairs, 1)
        self.assertEqual(result[0]["geometry"]["type"], "MultiPolygon")


if __name__ == "__main__":
    unittest.main()
