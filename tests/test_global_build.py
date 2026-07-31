from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import duckdb
import h3

from app.build_cache import SYSTEMS, stream_tile_features, validate_materialized_data
from app.build_db import build_h3_table, validate_crosswalk


class GlobalH3InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.connection = duckdb.connect()
        self.h3_index = h3.latlng_to_cell(56.0, 10.0, 3)
        self.h3_integer = h3.str_to_int(self.h3_index)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def write_global_h3(self) -> Path:
        target = self.root / "global.parquet"
        escaped = str(target).replace("'", "''")
        self.connection.execute(
            f"""
            COPY (
                SELECT ?::UBIGINT AS h3_cell, [101::BIGINT, 102::BIGINT] AS species_ids
            ) TO '{escaped}' (FORMAT PARQUET)
            """,
            [self.h3_integer],
        )
        return target

    def write_crosswalk(self) -> Path:
        target = self.root / "crosswalk.parquet"
        escaped = str(target).replace("'", "''")
        self.connection.execute(
            f"""
            COPY (
                SELECT * FROM (
                    VALUES ('101', 'gbif-1'), ('102', 'gbif-2')
                ) AS mappings(source_species_id, gbif_accepted_id)
            ) TO '{escaped}' (FORMAT PARQUET)
            """
        )
        return target

    def test_global_input_requires_crosswalk(self) -> None:
        with self.assertRaisesRegex(ValueError, "H3_ID_CROSSWALK_PATH"):
            build_h3_table(self.connection, 3, self.write_global_h3(), None)

    def test_global_input_is_normalized_without_loss(self) -> None:
        h3_path = self.write_global_h3()
        crosswalk_path = self.write_crosswalk()
        self.assertEqual(validate_crosswalk(self.connection, crosswalk_path)["rows"], 2)

        report = build_h3_table(
            self.connection, 3, h3_path, crosswalk_path
        )
        row = self.connection.execute(
            "SELECT h3_index, gbif_ids FROM H3Res3Species"
        ).fetchone()

        self.assertEqual(row, (self.h3_index, ["gbif-1", "gbif-2"]))
        self.assertEqual(report["source_relationships"], 2)
        self.assertEqual(report["mapped_relationships"], 2)
        self.assertEqual(report["unmatched_relationships"], 0)

    def test_crosswalk_rejects_many_to_one_mapping(self) -> None:
        target = self.root / "bad-crosswalk.parquet"
        escaped = str(target).replace("'", "''")
        self.connection.execute(
            f"""
            COPY (
                SELECT * FROM (
                    VALUES ('101', 'gbif-1'), ('102', 'gbif-1')
                ) AS mappings(source_species_id, gbif_accepted_id)
            ) TO '{escaped}' (FORMAT PARQUET)
            """
        )
        with self.assertRaisesRegex(ValueError, "many_to_one_targets=1"):
            validate_crosswalk(self.connection, target)

    def test_crosswalk_accepts_iucn_goat_output_identity(self) -> None:
        target = self.root / "iucn-goat-crosswalk.parquet"
        escaped = str(target).replace("'", "''")
        self.connection.execute(
            f"""
            COPY (
                SELECT * FROM (
                    VALUES (101::BIGINT, '123'::VARCHAR, 'MATCHED'),
                           (102::BIGINT, NULL::VARCHAR, 'NO_GOAT_NCBI_CANDIDATE')
                ) AS mappings(iucn_sis_id, matched_ncbi_species_taxid, match_status)
            ) TO '{escaped}' (FORMAT PARQUET)
            """
        )
        report = validate_crosswalk(self.connection, target)
        self.assertEqual(report["source_column"], "iucn_sis_id")
        self.assertEqual(report["target_column"], "iucn_sis_id")
        build_report = build_h3_table(
            self.connection, 3, self.write_global_h3(), target
        )
        self.assertTrue(build_report["identity_mapping"])
        self.assertEqual(
            self.connection.execute(
                "SELECT gbif_ids FROM H3Res3Species"
            ).fetchone()[0],
            ["101", "102"],
        )


class ServingBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect()
        self.h3_index = h3.latlng_to_cell(56.0, 10.0, 3)
        self.connection.execute(
            """
            CREATE TABLE SpecInfo (
                gbif_accepted_id VARCHAR,
                species_name VARCHAR,
                family VARCHAR,
                redlist_category VARCHAR,
                has_dna_species_level BOOLEAN,
                genus_has_dna BOOLEAN,
                family_has_dna BOOLEAN,
                edge_group_name VARCHAR,
                meets_ebp BOOLEAN
            );
            INSERT INTO SpecInfo VALUES
                ('gbif-1', 'Species one', 'Family', 'Least Concern',
                 false, true, true, NULL, false);
            """
        )
        for resolution in (3, 7):
            self.connection.execute(
                f"""
                CREATE TABLE H3Res{resolution}Species (
                    h3_index VARCHAR,
                    gbif_ids VARCHAR[]
                );
                INSERT INTO H3Res{resolution}Species
                VALUES (?, ['gbif-1']);
                """,
                [self.h3_index],
            )
            for system in SYSTEMS:
                self.connection.execute(
                    f"""
                    CREATE TABLE h3_res{resolution}_agg_{system} AS
                    SELECT
                        ?::VARCHAR AS h3_index,
                        1::BIGINT AS total_species,
                        0::BIGINT AS crit_endangered_count,
                        0::BIGINT AS endangered_count,
                        0::BIGINT AS vulnerable_count,
                        0::BIGINT AS near_threatened_count,
                        0::BIGINT AS data_deficient_count,
                        1::BIGINT AS least_concern_count,
                        1::BIGINT AS missing_species_dna,
                        0::BIGINT AS missing_genus_dna,
                        0::BIGINT AS missing_family_dna
                    """,
                    [self.h3_index],
                )

    def tearDown(self) -> None:
        self.connection.close()

    def test_validation_reports_no_loss(self) -> None:
        report = validate_materialized_data(self.connection)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["failures"], [])

    def test_tile_features_are_streamed_with_layer_names(self) -> None:
        output = io.StringIO()
        count = stream_tile_features(self.connection, output, batch_size=1)
        features = [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(count, 8)
        self.assertEqual(len(features), 8)
        self.assertEqual(
            {item["tippecanoe"]["layer"] for item in features},
            {
                "res3_all", "res3_terrestrial", "res3_freshwater", "res3_marine",
                "res7_all", "res7_terrestrial", "res7_freshwater", "res7_marine",
            },
        )


if __name__ == "__main__":
    unittest.main()
