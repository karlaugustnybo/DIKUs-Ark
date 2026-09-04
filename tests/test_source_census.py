from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pyogrio
import shapely
from shapely.geometry import MultiPolygon, Polygon, box

from ark_pipeline.cli.benchmark_pipeline import DEFAULT_PROFILE, source_scan
from ark_pipeline.cli.benchmark_sample import SIZE_BREAKS, _size_bin
from ark_pipeline.cli.spatial_pairs import exclusion_reason_values, iter_arrow_batches
from ark_pipeline.runtime.provenance import atomic_json, sha256
from ark_pipeline.spatial.census import census_bounds, iter_census_batches, read_census_bounds
from ark_pipeline.spatial.coverage import load_spatial_profile


class SourceCensusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = load_spatial_profile(DEFAULT_PROFILE)

    def write_layer(self, path, geometry, **attributes):
        fields = {"id_no": list(range(1, len(geometry) + 1)),
                  "presence": [1] * len(geometry), "origin": [1] * len(geometry),
                  "seasonal": [1] * len(geometry)}
        fields.update(attributes)
        pyogrio.write_dataframe(gpd.GeoDataFrame(fields, geometry=geometry, crs="EPSG:4326"), path)

    def register(self, path):
        atomic_json(self.root / "acquisition/current.json", {
            "schema_version": 1, "sources": {"iucn-spatial": {
                "validation_status": "passed", "release": "fixture", "files": [{
                    "logical_name": path.name, "path": str(path),
                    "bytes": path.stat().st_size, "sha256": sha256(path),
                }],
            }},
        })
        return {"root": str(self.root), "output": str(self.root / "out"), "profile": str(DEFAULT_PROFILE)}

    def test_zip_multilayer_envelopes_and_policy_match_original_reader(self):
        shapes = self.root / "shapes"
        shapes.mkdir()
        # Holes, disjoint parts, dateline-spanning coordinates, Z, exact bin
        # boundaries, nulls and overlapping exclusion reasons.
        geometries = [
            Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], holes=[[(1, 1), (1, 2), (2, 2), (2, 1)]]),
            MultiPolygon([box(-10, -10, -9, -9), box(20, 20, 21, 21)]),
            box(-179, -1, 179, 1),
            Polygon([(0, 0, 2), (1, 0, 3), (1, 1, 4), (0, 0, 2)]),
            *[box(0, 0, value, 1) for value in SIZE_BREAKS],
            None, None, box(0, 0, 1, 1), box(0, 0, 1, 1), box(0, 0, 1, 1),
        ]
        count = len(geometries)
        self.write_layer(shapes / "ranges.shp", geometries,
                         id_no=[*range(1, count - 4), None, count - 3, count - 2, count - 1, count],
                         presence=[1] * (count - 5) + [99, 99, 99, 1, 1],
                         origin=[1] * (count - 2) + [99, 1],
                         seasonal=[1] * (count - 1) + [99])
        self.write_layer(shapes / "other.shp", [box(1, 1, 2, 2)])
        archive = self.root / "ranges.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for path in shapes.iterdir():
                package.write(path, path.name)
        original = {}
        for layer, geom, fid, batch in iter_arrow_batches(archive, 3):
            for row in batch.to_pylist():
                wkb = row.pop(geom)
                original[layer, row[fid]] = (row, wkb)
        profile = replace(self.profile, seasonality=(1,))
        reasons = Counter()
        observed = set()
        for layer, _ in pyogrio.list_layers(archive):
            for batch in iter_census_batches(archive, layer, 3):
                for row, wkb, envelope in batch:
                    key = (layer, row["OGC_FID"])
                    observed.add(key)
                    old_row, old_wkb = original[key]
                    def reason(record, geometry):
                        return exclusion_reason_values(record["id_no"], geometry, record["presence"], record["origin"], profile, seasonal=record["seasonal"])
                    self.assertEqual(reason(row, wkb), reason(old_row, old_wkb))
                    if reason(row, wkb):
                        reasons[reason(row, wkb)] += 1
                    else:
                        np.testing.assert_array_equal(census_bounds(wkb, envelope), shapely.from_wkb(old_wkb).bounds)
        self.assertEqual(observed, set(original))
        self.assertEqual(reasons, dict.fromkeys(["null_iucn_sis_id", "null_geometry", "presence_policy", "origin_policy", "seasonality_policy"], 1))
        with contextlib.redirect_stdout(io.StringIO()):
            source_scan(self.register(archive))
        report = json.loads((self.root / "out/population.json").read_text())
        expected = [0] * (len(SIZE_BREAKS) + 1)
        for row, wkb in original.values():
            if exclusion_reason_values(row["id_no"], wkb, row["presence"], row["origin"], self.profile, seasonal=row["seasonal"]) is None:
                x0, y0, x1, y1 = shapely.from_wkb(wkb).bounds
                expected[_size_bin(max(0, x1 - x0) * max(0, y1 - y0))] += 1
        self.assertEqual(report["size_bin_counts"], expected)
        self.assertEqual(report["archives"][0]["scanned"], len(original))

    def test_empty_geometry_still_fails_but_null_is_excluded(self):
        path = self.root / "ranges.gpkg"
        self.write_layer(path, [None, Polygon(), box(0, 0, 1, 1)])
        config = self.register(path)
        with self.assertRaisesRegex(ValueError, "empty or non-finite"):
            source_scan(config)
        self.assertFalse((self.root / "out/population.json").exists())

    def test_bounds_are_joined_by_fid_and_missing_rows_fail(self):
        path = self.root / "ranges.gpkg"
        self.write_layer(path, [box(0, 0, 1, 1), box(2, 2, 5, 5)])
        fids, bounds = read_census_bounds(path, "ranges")
        with patch("ark_pipeline.spatial.census.read_census_bounds", return_value=(fids[::-1], bounds[:, ::-1])):
            rows = [row for batch in iter_census_batches(path, "ranges", 1) for row in batch]
        np.testing.assert_array_equal(rows[0][2], [0, 0, 1, 1])
        with patch("ark_pipeline.spatial.census.read_census_bounds", return_value=(fids[:1], bounds[:, :1])):
            with self.assertRaisesRegex(ValueError, "FID mismatch"):
                list(iter_census_batches(path, "ranges", 1))

    def test_topology_setting_restored_after_success_and_failure(self):
        previous = pyogrio.get_gdal_config_option("OGR_ORGANIZE_POLYGONS")
        self.addCleanup(pyogrio.set_gdal_config_options, {"OGR_ORGANIZE_POLYGONS": previous})
        pyogrio.set_gdal_config_options({"OGR_ORGANIZE_POLYGONS": "DEFAULT"})
        path = self.root / "ranges.gpkg"
        self.write_layer(path, [box(0, 0, 1, 1)])
        read_census_bounds(path, "ranges")
        self.assertEqual(pyogrio.get_gdal_config_option("OGR_ORGANIZE_POLYGONS"), "DEFAULT")
        with patch("ark_pipeline.spatial.census.pyogrio.read_bounds", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                read_census_bounds(path, "ranges")
        self.assertEqual(pyogrio.get_gdal_config_option("OGR_ORGANIZE_POLYGONS"), "DEFAULT")

    def test_checksum_verification_is_retained(self):
        path = self.root / "ranges.gpkg"
        self.write_layer(path, [box(0, 0, 1, 1)])
        config = self.register(path)
        with path.open("r+b") as handle:
            handle.write(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum changed"):
            source_scan(config)


if __name__ == "__main__":
    unittest.main()
