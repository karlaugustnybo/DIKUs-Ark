from __future__ import annotations

import contextlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
import h3
import pyarrow as pa
import pyarrow.parquet as pq

from ark_pipeline.aggregation.metrics import aggregate_species_lists
from ark_pipeline.aggregation.pairs import LIST_SCHEMA
from ark_pipeline.builders.coarse_cache import METRICS, SYSTEMS, wide_feature
from ark_pipeline.builders.fine_metrics import (
    aggregate_part,
    prepare_species,
    record_aggregate_receipt,
)
from ark_pipeline.cli.serving_tiles import (
    BOUNDARIES,
    build,
    compile_shard,
    parser,
    read_prepared,
    record_prepared,
)
from ark_pipeline.spatial.boundaries import JurisdictionIndex
from ark_pipeline.tiles import BoundaryBatchIndex, feature_batch


def boundary_fixture(path: Path) -> None:
    def polygon(code, coordinates):
        return {"type": "Feature", "properties": {"code": code, "name": code},
                "geometry": {"type": "Polygon", "coordinates": coordinates}}
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        polygon("B", [[[-180, -80], [180, -80], [180, 80], [-180, 80], [-180, -80]],
                      [[-5, -5], [-5, 5], [5, 5], [5, -5], [-5, -5]]]),
        polygon("A", [[[0, -80], [180, -80], [180, 80], [0, 80], [0, -80]]]),
    ]}))


class TileFeatureTests(unittest.TestCase):
    def test_batch_matches_legacy_for_dateline_pentagons_holes_and_overlaps(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "boundaries.json"
            boundary_fixture(path)
            index = JurisdictionIndex(path)
            batched = BoundaryBatchIndex(index)
            for resolution in (3, 7):
                cells = sorted({
                    h3.latlng_to_cell(lat, lon, resolution)
                    for lat in (-89, -80, -45, 0, 5, 55, 80, 89)
                    for lon in (-179.9999, -120, -5, 0, 5, 12, 120, 179.9999)
                } | set(h3.get_pentagons(resolution)))
                rows = [(cell, *range(len(SYSTEMS) * len(METRICS))) for cell in cells]
                expected = [wide_feature(row, resolution, {"admin0": index.codes_for_cell(row[0])}) for row in rows]
                for batch_size in (1, 7, 2048):
                    actual = []
                    for offset in range(0, len(rows), batch_size):
                        actual.extend(feature_batch(rows[offset:offset + batch_size], resolution, {"admin0": batched}))
                    self.assertEqual(actual, expected)


def prepared_fixture(root: Path):
    h3_root = root / "lists"
    sources = h3_root / "res7_merged_parts"
    sources.mkdir(parents=True)
    parts = root / "metrics"
    parts.mkdir()
    species, systems = root / "species.parquet", root / "systems.parquet"
    pq.write_table(pa.Table.from_pylist([{
        "gbif_accepted_id": "1", "redlist_category": "Endangered",
        "has_dna_species_level": False, "genus_has_dna": True,
        "family_has_dna": True, "goat_data_deficient": False,
    }]), species)
    pq.write_table(pa.table({"gbif_accepted_id": ["1"], "system": ["Terrestrial"]}), systems)
    # Different H3 base cells in the same web tile exercise merging shards.
    cells = ["873e9a68affffff", "875869a48ffffff"]
    coarse = sorted({h3.cell_to_parent(cell, 3) for cell in cells})
    coarse_source = h3_root / "h3_res3_species_global_merged.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"h3_cell": h3.str_to_int(cell), "species_ids": [1]} for cell in coarse
    ], schema=LIST_SCHEMA), coarse_source)
    metric_args = SimpleNamespace(species=species, species_systems=systems)
    with duckdb.connect(str(root / "build.duckdb")) as connection:
        prepare_species(connection, species, systems)
        for cell in cells:
            name = f"base_{h3.get_base_cell_number(cell)}.parquet"
            pq.write_table(pa.Table.from_pylist([
                {"h3_cell": h3.str_to_int(cell), "species_ids": [1]},
            ], schema=LIST_SCHEMA), sources / name)
            aggregate_part(connection, sources / name, parts / name)
            record_aggregate_receipt(metric_args, sources / name, parts / name)
        aggregate_species_lists(connection, coarse_source, root / "coarse.parquet")
        for system in SYSTEMS:
            projection = ", ".join(f'"{metric}__{system.lower()}" AS "{metric}"' for metric in METRICS)
            connection.execute(f"CREATE TABLE h3_res3_agg_{system} AS SELECT h3_index, {projection} FROM read_parquet(?)",
                               [str(root / "coarse.parquet")])
    template = root / "metadata.json"
    template.write_text(json.dumps({key: {system.lower(): {"min": 0, "max": 1} for system in SYSTEMS}
                                    for key in ("score_domains", "species_normalized_score_domains")}))
    record_args = SimpleNamespace(h3_root=h3_root, parts_dir=parts, species=species,
                                  species_systems=systems, build_duckdb=root / "build.duckdb",
                                  metadata_template=template, output=root / "prepared.json")
    record_prepared(record_args)
    boundaries = root / "boundaries.json"
    boundary_fixture(boundaries)
    options = ["build", "--prepared-inputs", str(record_args.output), "--output-dir", str(root / "tiles"),
               "--scratch-dir", str(root / "scratch")]
    for key in BOUNDARIES:
        options.extend(["--" + key.replace("_", "-"), str(boundaries)])
    return parser().parse_args(options), record_args, cells


@unittest.skipUnless(all(shutil.which(tool) for tool in ("tippecanoe", "tile-join", "tippecanoe-decode")),
                     "Tippecanoe binaries are required for the actual archive integration test")
class TilePublicationTests(unittest.TestCase):
    def test_real_tiles_resume_and_publish_metadata_together(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            args, record_args, cells = prepared_fixture(root)
            args.checkpoint_shards = True
            report = build(args)
            self.assertEqual(report["features"], 4)
            current = root / "tiles/current"
            first = current.resolve()
            metadata = json.loads((current / "map-metadata.json").read_text())
            self.assertEqual(metadata["complete_resolutions"], [3, 7])
            for cell in cells:
                lat, lon = h3.cell_to_latlng(cell)
                zoom = 8
                x = int((lon + 180) / 360 * (1 << zoom))
                y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * (1 << zoom))
                decoded = json.loads(subprocess.check_output([
                    "tippecanoe-decode", str(current / "priorities.pmtiles"), str(zoom), str(x), str(y),
                ], stderr=subprocess.DEVNULL))
                features = [f for layer in decoded["features"] for f in layer["features"]]
                found = [f for f in features if f["properties"]["h3_index"] == cell]
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["properties"]["a_en_sp"], 1)
                self.assertEqual(found[0]["properties"]["a_total"], 1)
            with patch("ark_pipeline.cli.serving_tiles.compile_shard", side_effect=AssertionError("should reuse")):
                self.assertTrue(build(args)["reused"])

            # A metadata-only refresh reuses every geometric shard.
            metadata = json.loads(record_args.metadata_template.read_text())
            metadata["release"] = "new"
            record_args.metadata_template.write_text(json.dumps(metadata))
            record_prepared(record_args)
            with patch("ark_pipeline.cli.serving_tiles.subprocess.run", side_effect=RuntimeError("interrupted merge")):
                with self.assertRaisesRegex(RuntimeError, "interrupted merge"):
                    build(args)
            self.assertEqual(current.resolve(), first)
            with patch("ark_pipeline.cli.serving_tiles.compile_shard", side_effect=AssertionError("should reuse shards")):
                resumed = build(args)
            self.assertTrue(all(part["reused"] for part in resumed["shards"].values()))
            self.assertNotEqual(current.resolve(), first)
            self.assertEqual(json.loads((current / "map-metadata.json").read_text())["release"], "new")
            # Corruption is detected even when the output length is unchanged.
            archive = current / "priorities.pmtiles"
            with archive.open("r+b") as stream:
                stream.write(b"broken!")
            repaired = build(args)
            self.assertFalse(repaired["reused"])
            self.assertTrue(build(args)["reused"])

            # A failed shard leaves the current bundle intact and its completed
            # predecessor reusable when the build resumes.
            previous = current.resolve()
            args.admin0.write_text(args.admin0.read_text() + "\n")
            calls = 0

            def interrupted(*positional, **keywords):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("interrupted shard")
                return compile_shard(*positional, **keywords)

            with patch("ark_pipeline.cli.serving_tiles.compile_shard", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "interrupted shard"):
                    build(args)
            self.assertEqual(current.resolve(), previous)
            resumed = build(args)
            self.assertEqual(sum(part["reused"] for part in resumed["shards"].values()), 1)

    def test_default_build_streams_once_without_a_merge_and_reuses_archive(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            args, _, _ = prepared_fixture(root)
            with patch("ark_pipeline.cli.serving_tiles.compile_shard", side_effect=AssertionError("no shards")), \
                 patch("ark_pipeline.cli.serving_tiles.subprocess.run", side_effect=AssertionError("no merge")):
                report = build(args)
                self.assertEqual(report["compilation"]["features"], 4)
                self.assertTrue(build(args)["reused"])
            previous = (root / "tiles/current").resolve()
            args.admin0.write_text(args.admin0.read_text() + "\n")
            with patch("ark_pipeline.cli.serving_tiles.stream_query", side_effect=ValueError("stream failed")):
                with self.assertRaisesRegex(ValueError, "stream failed"):
                    build(args)
            self.assertEqual((root / "tiles/current").resolve(), previous)

    def test_source_changes_and_incomplete_coverage_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, record_args, cells = prepared_fixture(root)
            source = next((record_args.h3_root / "res7_merged_parts").glob("*.parquet"))
            original = source.read_bytes()
            source.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            with self.assertRaisesRegex(ValueError, "Prepared input changed"):
                read_prepared(args.prepared_inputs)
            source.write_bytes(original)
            original_database = record_args.build_duckdb.read_bytes()
            with duckdb.connect(str(record_args.build_duckdb)) as connection:
                connection.execute("UPDATE h3_res3_agg_all SET total_species=99")
            with self.assertRaisesRegex(ValueError, "Coarse metrics do not match"):
                record_prepared(record_args)
            record_args.build_duckdb.write_bytes(original_database)
            target = record_args.parts_dir / source.name
            target.unlink()
            with self.assertRaisesRegex(ValueError, "coverage changed"):
                read_prepared(args.prepared_inputs)
            with self.assertRaisesRegex(ValueError, "exactly"):
                record_prepared(record_args)


if __name__ == "__main__":
    unittest.main()
