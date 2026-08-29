from __future__ import annotations

import io
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
import h3
import pyarrow.ipc as pa_ipc
from rich.console import Console

from app.build_cache import (
    METRICS,
    SYSTEMS,
    antimeridian_safe_polygon,
    export_coarse_snapshot,
    export_map_metadata,
    export_parquet,
    score_expression,
    stream_tile_features,
    validate_materialized_data,
)
from app.build_db import build_h3_table, validate_crosswalk
from app.build_global_species import build_global_species
from app.build_res7_preview import (
    BuildDisplay,
    _build_parts_parallel,
    aggregate_part,
    completed_parts,
    configure_connection,
    prepare_species,
    validate_aggregate_part,
    write_preview_metadata,
)
from app.jurisdictions import JurisdictionIndex, load_jurisdiction_index
from backend.res7_tiles import aggregate_coverage, available_base_cells, base_cell, render_tile


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


class GlobalSpeciesDimensionTests(unittest.TestCase):
    def test_build_retains_h3_only_ids_as_explicit_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            connection = duckdb.connect()
            crosswalk = root / "crosswalk.parquet"
            h3_path = root / "h3.parquet"
            connection.execute(
                f"""
                COPY (
                    SELECT * FROM (VALUES
                        (101::BIGINT, 1001::BIGINT, 'Species one', 'GENUS', 'FAMILY',
                         'Genus', 'Family', 'Endangered', '501', 'MATCHED', true),
                        (102::BIGINT, 1002::BIGINT, 'Species two', 'GENUS', 'FAMILY',
                         NULL, NULL, 'Least Concern', NULL, 'NO_GOAT_NCBI_CANDIDATE', false)
                    ) AS rows(
                        iucn_sis_id, iucn_assessment_id, iucn_scientific_name,
                        iucn_genus, iucn_family, ncbi_genus, ncbi_family,
                        iucn_redlist_category, matched_ncbi_species_taxid,
                        match_status, safe_for_automatic_species_trait_transfer
                    )
                ) TO '{str(crosswalk).replace("'", "''")}' (FORMAT PARQUET)
                """
            )
            h3_index = h3.str_to_int(h3.latlng_to_cell(0, 0, 3))
            connection.execute(
                f"""
                COPY (
                    SELECT {h3_index}::UBIGINT AS h3_cell,
                           [101::BIGINT, 102::BIGINT, 999::BIGINT] AS species_ids
                ) TO '{str(h3_path).replace("'", "''")}' (FORMAT PARQUET)
                """
            )
            connection.close()

            assessments = root / "assessments.csv"
            assessments.write_text(
                "assessmentId,systems\n1001,Terrestrial|Freshwater (=Inland waters)\n"
                "1002,Marine\n"
            )
            goat = root / "goat.tsv"
            goat.write_text(
                "taxon_id\tassembly_level\tbusco_completeness\t"
                "ebp_standard_criteria\tin_progress\tresampling_required\t"
                "sample_acquired\tsequencing_status\n"
                "501\tChromosome\t98\t6.7\t\t\t\tinsdc_open\n"
            )
            gbif = root / "gbif.tsv"
            gbif.write_text(
                "taxonID\tscientificName\tcanonicalName\ttaxonRank\t"
                "kingdom\tphylum\tclass\torder\tfamily\tgenus\t"
                "specificEpithet\ttaxonomicStatus\n"
                "7001\tSpecies one\tSpecies one\tspecies\tAnimalia\t\t\t\t"
                "Family\tSpecies\tone\taccepted\n"
                "7002\tSpecies two\tSpecies two\tspecies\tAnimalia\t\t\t\t"
                "Family\tSpecies\ttwo\taccepted\n"
            )
            edge = root / "edge.tsv"
            edge.write_text(
                "group_name\trl_id\tedge_rank\nAmphibians\t101\t1\n"
            )
            output = root / "output"
            report = build_global_species(
                crosswalk_path=crosswalk,
                assessments_path=assessments,
                goat_species_path=goat,
                gbif_backbone_path=gbif,
                edge_species_path=edge,
                h3_paths=[h3_path],
                output_dir=output,
            )

            self.assertEqual(report["species"], 3)
            self.assertEqual(report["species_with_gbif_taxon_id"], 2)
            self.assertEqual(report["edge_species"], 1)
            self.assertEqual(report["goat_data_deficient_species"], 2)
            self.assertEqual(report["h3_species_missing_iucn_ids"], ["999"])
            check = duckdb.connect()
            rows = check.execute(
                "SELECT gbif_accepted_id, species_name, has_dna_species_level, "
                "gbif_taxon_id, goat_taxon_id, goat_data_deficient, edge_group_name "
                "FROM read_parquet(?) ORDER BY gbif_accepted_id",
                [str(output / "species.parquet")],
            ).fetchall()
            systems = check.execute(
                "SELECT system FROM read_parquet(?) ORDER BY system",
                [str(output / "species_systems.parquet")],
            ).fetchall()
            check.close()
            self.assertEqual(
                rows[-1],
                ("999", "IUCN taxon 999", False, None, None, True, None),
            )
            self.assertTrue(rows[0][2])
            self.assertEqual(rows[0][3:], ("7001", "501", False, "Amphibians"))
            self.assertEqual(
                systems, [("Freshwater",), ("Marine",), ("Terrestrial",)]
            )

    def test_dna_status_rules_are_shared_by_species_and_lineages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            connection = duckdb.connect()
            crosswalk = root / "crosswalk.parquet"
            h3_path = root / "h3.parquet"
            connection.execute(
                f"""
                COPY (
                    SELECT * FROM (VALUES
                        (101::BIGINT, 1001::BIGINT, 'Sampled species', 'GENUS_A', 'FAMILY_A',
                         'Genus_A', 'Family_A', 'Endangered', '501', 'MATCHED', true),
                        (102::BIGINT, 1002::BIGINT, 'Related target', 'GENUS_B', 'FAMILY_A',
                         'Genus_B', 'Family_A', 'Critically Endangered', '502', 'MATCHED', true),
                        (103::BIGINT, 1003::BIGINT, 'Weak assembly', 'GENUS_C', 'FAMILY_B',
                         'Genus_C', 'Family_B', 'Endangered', '503', 'MATCHED', true),
                        (104::BIGINT, 1004::BIGINT, 'In progress species', 'GENUS_D', 'FAMILY_C',
                         'Genus_D', 'Family_C', 'Endangered', '504', 'MATCHED', true),
                        (105::BIGINT, 1005::BIGINT, 'Unsafe match', 'GENUS_E', 'FAMILY_D',
                         'Genus_E', 'Family_D', 'Endangered', '505', 'MATCHED', false),
                        (106::BIGINT, 1006::BIGINT, 'Extinct representative', 'GENUS_F', 'FAMILY_E',
                         NULL, NULL, 'Extinct', NULL, 'NO_GOAT_NCBI_CANDIDATE', false),
                        (107::BIGINT, 1007::BIGINT, 'Extant relative', 'GENUS_G', 'FAMILY_E',
                         NULL, NULL, 'Critically Endangered', NULL, 'NO_GOAT_NCBI_CANDIDATE', false)
                    ) AS rows(
                        iucn_sis_id, iucn_assessment_id, iucn_scientific_name,
                        iucn_genus, iucn_family, ncbi_genus, ncbi_family,
                        iucn_redlist_category, matched_ncbi_species_taxid,
                        match_status, safe_for_automatic_species_trait_transfer
                    )
                ) TO '{str(crosswalk).replace("'", "''")}' (FORMAT PARQUET)
                """
            )
            h3_index = h3.str_to_int(h3.latlng_to_cell(0, 0, 3))
            connection.execute(
                f"""
                COPY (
                    SELECT {h3_index}::UBIGINT AS h3_cell,
                           [101::BIGINT, 102::BIGINT, 103::BIGINT, 104::BIGINT,
                            105::BIGINT, 106::BIGINT, 107::BIGINT] AS species_ids
                ) TO '{str(h3_path).replace("'", "''")}' (FORMAT PARQUET)
                """
            )
            connection.close()

            assessments = root / "assessments.csv"
            assessments.write_text(
                "assessmentId,systems\n"
                + "".join(f"{assessment},Terrestrial\n" for assessment in range(1001, 1008))
            )
            goat = root / "goat.tsv"
            goat.write_text(
                "taxon_id\tassembly_level\tbusco_completeness\t"
                "ebp_standard_criteria\tin_progress\tresampling_required\t"
                "sample_acquired\tsequencing_status\n"
                "501\t\t\t\t\t\tvoucher\t\n"
                "503\tScaffold\t99\t\t\t\t\tinsdc_open\n"
                "504\t\t\t\tsequencing\t\t\t\n"
                "505\t\t\t\t\t\tvoucher\t\n"
            )
            output = root / "output"
            build_global_species(
                crosswalk_path=crosswalk,
                assessments_path=assessments,
                goat_species_path=goat,
                h3_paths=[h3_path],
                output_dir=output,
            )

            check = duckdb.connect()
            rows = check.execute(
                "SELECT gbif_accepted_id, has_dna_species_level, genus_has_dna, "
                "family_has_dna FROM read_parquet(?) ORDER BY gbif_accepted_id",
                [str(output / "species.parquet")],
            ).fetchall()
            check.close()

            self.assertEqual(
                rows,
                [
                    ("101", True, True, True),
                    ("102", False, False, True),
                    ("103", False, False, False),
                    ("104", True, True, True),
                    ("105", False, False, False),
                    ("106", True, True, True),
                    ("107", False, False, True),
                ],
            )


class JurisdictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = (
            Path(__file__).resolve().parents[1]
            / "app" / "static" / "data" / "boundaries" / "admin0.geojson"
        )
        self.index = JurisdictionIndex(self.path)
        self.admin1_index = JurisdictionIndex(
            self.path.with_name("admin1.geojson")
        )
        self.municipality_index = JurisdictionIndex(
            self.path.with_name("municipality.geojson")
        )
        self.conservation_index = JurisdictionIndex(
            self.path.with_name("conservation-framework.geojson")
        )

    def test_catalogue_covers_small_and_large_jurisdictions(self) -> None:
        self.assertGreaterEqual(len(self.index.codes), 250)
        self.assertTrue(all(properties.get("name") for properties in self.index.properties.values()))
        self.assertTrue(all(properties.get("name") for properties in self.admin1_index.properties.values()))
        self.assertEqual(self.index.code_for_point(56.1629, 10.2039), "DNK")
        self.assertEqual(self.index.code_for_point(41.9033, 12.4538), "VAT")
        self.assertEqual(self.index.code_for_point(0, -140), "")
        self.assertEqual(
            self.admin1_index.code_for_point(56.1629, 10.2039), "DNK-3416"
        )
        municipality = self.municipality_index.code_for_point(56.1629, 10.2039)
        self.assertEqual(
            self.municipality_index.properties[municipality]["name"], "Aarhus"
        )
        self.assertEqual(
            self.conservation_index.code_for_point(56.0, 10.0), "ECO-647"
        )

    def test_hierarchical_catalogues_are_geometry_free_and_parent_scoped(self) -> None:
        data_root = self.path.parent.parent
        manifest = json.loads((data_root / "boundary-frameworks.json").read_text())
        frameworks = {item["id"]: item for item in manifest["frameworks"]}
        self.assertIn("{parent}", frameworks["admin1"]["catalog_partition_url"])
        self.assertIn("{parent}", frameworks["municipality"]["catalog_partition_url"])

        for framework in ("admin1", "municipality"):
            catalogue = json.loads(
                (data_root / "boundary-catalogs" / framework / "dnk.json").read_text()
            )
            self.assertTrue(catalogue["features"])
            self.assertTrue(all(
                feature["parent_code"] == "DNK" for feature in catalogue["features"]
            ))
            self.assertTrue(all("geometry" not in feature for feature in catalogue["features"]))
            self.assertTrue(all(
                feature["geometry_url"].endswith("/dnk.geojson")
                for feature in catalogue["features"]
            ))

    def test_cell_membership_includes_a_boundary_touched_away_from_its_centre(self) -> None:
        h3_index = h3.latlng_to_cell(56.0, 10.0, 3)
        centre_latitude, centre_longitude = h3.cell_to_latlng(h3_index)
        vertex_latitude, vertex_longitude = h3.cell_to_boundary(h3_index)[0]
        delta = 0.02
        geometry = {
            "type": "Polygon",
            "coordinates": [[
                [vertex_longitude - delta, vertex_latitude - delta],
                [vertex_longitude + delta, vertex_latitude - delta],
                [vertex_longitude + delta, vertex_latitude + delta],
                [vertex_longitude - delta, vertex_latitude + delta],
                [vertex_longitude - delta, vertex_latitude - delta],
            ]],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "touch.geojson"
            target.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"code": "TOUCH", "name": "Touch only"},
                    "geometry": geometry,
                }],
            }))
            index = JurisdictionIndex(target)

        self.assertEqual(
            index.code_for_point(centre_latitude, centre_longitude), ""
        )
        self.assertEqual(index.codes_for_cell(h3_index), ("TOUCH",))


class ServingBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect()
        self.h3_index = h3.latlng_to_cell(56.0, 10.0, 3)
        self.connection.execute(
            """
            CREATE TABLE SpecInfo (
                gbif_accepted_id VARCHAR,
                iucn_sis_id VARCHAR,
                iucn_assessment_id VARCHAR,
                gbif_taxon_id VARCHAR,
                goat_taxon_id VARCHAR,
                species_name VARCHAR,
                family VARCHAR,
                redlist_category VARCHAR,
                has_dna_species_level BOOLEAN,
                genus_has_dna BOOLEAN,
                family_has_dna BOOLEAN,
                goat_data_deficient BOOLEAN,
                edge_group_name VARCHAR,
                meets_ebp BOOLEAN
            );
            INSERT INTO SpecInfo VALUES
                ('gbif-1', 'iucn-1', 'assessment-1', 'gbif-1', 'goat-1',
                 'Species one', 'Family', 'Least Concern',
                 false, true, true, false, NULL, false);
            """
        )
        self.connection.execute(
            """
            CREATE TABLE SpecSystems (
                gbif_accepted_id VARCHAR,
                system VARCHAR
            );
            INSERT INTO SpecSystems VALUES ('gbif-1', 'Terrestrial');
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
            populated_metrics = {
                "total_species",
                "least_concern_count",
                "missing_species_dna",
                "priority_lc_sp_count",
            }
            metric_columns = ",\n                        ".join(
                f"{int(metric in populated_metrics)}::BIGINT AS {metric}"
                for metric in METRICS
            )
            for system in SYSTEMS:
                self.connection.execute(
                    f"""
                    CREATE TABLE h3_res{resolution}_agg_{system} AS
                    SELECT
                        ?::VARCHAR AS h3_index,
                        {metric_columns}
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
        root = Path(__file__).resolve().parents[1] / "app" / "static" / "data" / "boundaries"
        jurisdiction_index = {
            "admin0": JurisdictionIndex(root / "admin0.geojson"),
            "admin1": JurisdictionIndex(root / "admin1.geojson"),
            "municipality": JurisdictionIndex(root / "municipality.geojson"),
            "conservation_framework": JurisdictionIndex(
                root / "conservation-framework.geojson"
            ),
        }
        count = stream_tile_features(
            self.connection, output, batch_size=1,
            jurisdiction_index=jurisdiction_index,
        )
        features = [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(count, 2)
        self.assertEqual(len(features), 2)
        self.assertEqual(
            {item["tippecanoe"]["layer"] for item in features},
            {"res3", "res7"},
        )
        self.assertEqual(
            {
                item["properties"]["resolution"]: (
                    item["tippecanoe"]["minzoom"],
                    item["tippecanoe"]["maxzoom"],
                )
                for item in features
            },
            {3: (0, 6), 7: (8, 12)},
        )
        self.assertEqual(features[0]["properties"]["a_total"], 1)
        self.assertEqual(features[0]["properties"]["t_total"], 1)
        self.assertIn("DNK", features[0]["properties"]["j"].split("|"))
        self.assertIn("DNK-3416", features[0]["properties"]["a1"].split("|"))
        self.assertTrue(features[0]["properties"]["mun"].split("|"))
        self.assertIn("ECO-647", features[0]["properties"]["eco"].split("|"))

    def test_coarse_snapshot_contains_each_res3_cell_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            descriptor = export_coarse_snapshot(self.connection, output)
            target = output / Path(descriptor["url"]).name
            table = pa_ipc.open_file(target).read_all()

        self.assertEqual(descriptor["format"], "arrow-ipc-v1")
        self.assertEqual(descriptor["cells"], 1)
        self.assertEqual(table.num_rows, 1)
        self.assertEqual(table.column("h3_index")[0].as_py(), self.h3_index)
        self.assertEqual(table.column("a_total")[0].as_py(), 1)
        self.assertEqual(table.column("t_total")[0].as_py(), 1)
        self.assertEqual(table.column("f_total")[0].as_py(), 1)

    def test_map_metadata_contains_local_jurisdiction_domains(self) -> None:
        root = Path(__file__).resolve().parents[1] / "app" / "static" / "data" / "boundaries"
        jurisdiction_index = {
            "admin0": JurisdictionIndex(root / "admin0.geojson"),
            "admin1": JurisdictionIndex(root / "admin1.geojson"),
            "municipality": JurisdictionIndex(root / "municipality.geojson"),
            "conservation_framework": JurisdictionIndex(
                root / "conservation-framework.geojson"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "metadata.json"
            export_map_metadata(
                self.connection, target, (3, 7), jurisdiction_index
            )
            metadata = json.loads(target.read_text())
        self.assertEqual(metadata["jurisdiction_assignment"], "cell-intersection")
        self.assertEqual(metadata["boundary_assignment"], "cell-intersection")
        self.assertEqual(metadata["boundary_tile_properties"], {
            "admin0": "j",
            "admin1": "a1",
            "municipality": "mun",
            "eez": "eez",
            "conservation_framework": "eco",
        })
        self.assertEqual(
            metadata["jurisdiction_score_domains"]["all"]["DNK"],
            {"min": 0.2, "max": 0.2},
        )
        self.assertEqual(metadata["tile_layout"], "wide-v2-joint-priority")
        self.assertEqual(metadata["tile_schema_version"], 9)
        self.assertEqual(metadata["resolution_tile_ranges"], {
            "3": {"min": 0, "max": 6},
            "7": {"min": 8, "max": 12},
        })
        self.assertEqual(
            metadata["species_normalized_score_domains"]["all"],
            {"min": 0.2, "max": 0.2},
        )
        self.assertEqual(
            metadata["boundary_species_normalized_score_domains"]["admin0"]["all"]["DNK"],
            {"min": 0.2, "max": 0.2},
        )

    def test_export_includes_source_ids_and_compact_species_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            export_parquet(self.connection, output)
            species = self.connection.execute(
                "SELECT iucn_sis_id, iucn_assessment_id, gbif_taxon_id, "
                "goat_taxon_id, goat_data_deficient FROM read_parquet(?)",
                [str(output / "species.parquet")],
            ).fetchone()
            coverage = self.connection.execute(
                "SELECT gbif_accepted_id, resolution, len(h3_indexes) "
                "FROM read_parquet(?)",
                [str(output / "species_cells_res3.parquet")],
            ).fetchone()

        self.assertEqual(
            species,
            ("iucn-1", "assessment-1", "gbif-1", "goat-1", False),
        )
        self.assertEqual(coverage, ("gbif-1", 3, 1))

    def test_antimeridian_cells_are_split_instead_of_spanning_world(self) -> None:
        geometry = antimeridian_safe_polygon([
            [179.5, 10.0], [-179.5, 10.0], [-179.5, 11.0], [179.5, 11.0]
        ])

        self.assertEqual(geometry["type"], "MultiPolygon")
        for polygon in geometry["coordinates"]:
            longitudes = [point[0] for point in polygon[0]]
            self.assertLessEqual(max(longitudes) - min(longitudes), 1.0)


class Resolution7PreviewTests(unittest.TestCase):
    def test_static_preview_metadata_uses_current_joint_priority_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            part = root / "base_1.parquet"
            template = root / "template.json"
            target = root / "metadata.json"
            columns = ", ".join(
                f"1::INTEGER AS {metric}__{system.lower()}"
                for system in SYSTEMS for metric in METRICS
            )
            connection = duckdb.connect()
            connection.execute(
                f"COPY (SELECT '871ea64c2ffffff' AS h3_index, {columns}) "
                f"TO '{str(part).replace("'", "''")}' (FORMAT PARQUET)"
            )
            template.write_text(json.dumps({
                "score_domains": {
                    system.lower(): {"min": 0.0, "max": 0.1}
                    for system in SYSTEMS
                },
                "species_normalized_score_domains": {
                    system.lower(): {"min": 0.0, "max": 0.1}
                    for system in SYSTEMS
                },
            }))
            write_preview_metadata(
                connection=connection, template=template, target=target,
                parts=[part], source_parts_dir=None,
            )
            metadata = json.loads(target.read_text())

        self.assertEqual(metadata["tile_layout"], "wide-v2-joint-priority")
        self.assertEqual(metadata["tile_schema_version"], 9)
        self.assertEqual(metadata["resolution_tile_ranges"], {
            "3": {"min": 0, "max": 6},
            "7": {"min": 8, "max": 12},
        })
        self.assertGreater(metadata["score_domains"]["all"]["max"], 0.1)
        self.assertGreater(
            metadata["species_normalized_score_domains"]["all"]["max"], 0.1
        )

    def test_eta_uses_parallel_partition_progress_not_compressed_bytes(self) -> None:
        display = BuildDisplay(
            total=121,
            workers=2,
            memory_limit="750MB",
            output_dir=Path(tempfile.gettempdir()),
            work_weights={base_cell: 1_000_000 for base_cell in range(121)},
            enabled=False,
        )
        display.completed = 7
        display.aggregation_start_units = 6
        display.aggregation_started_at = time.monotonic() - 131
        display.active = {6: time.monotonic() - 131, 8: time.monotonic() - 11}
        display.active_progress = {6: 0.34, 8: 0.07}

        self.assertTrue(display._eta_text().startswith("ETA 2h 55m"))

    def test_parallel_aggregation_relays_worker_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            species = root / "species.parquet"
            systems = root / "systems.parquet"
            output_dir = root / "output"
            connection = duckdb.connect()
            connection.execute(
                f"""
                COPY (SELECT '101'::VARCHAR AS gbif_accepted_id,
                    'Species one'::VARCHAR AS species_name,
                    'Family'::VARCHAR AS family,
                    'Endangered'::VARCHAR AS redlist_category,
                    false AS has_dna_species_level, true AS genus_has_dna,
                    true AS family_has_dna, false AS goat_data_deficient,
                    NULL::VARCHAR AS edge_group_name, false AS meets_ebp)
                TO '{str(species).replace("'", "''")}' (FORMAT PARQUET);
                COPY (SELECT '101'::VARCHAR AS gbif_accepted_id,
                    'Terrestrial'::VARCHAR AS system)
                TO '{str(systems).replace("'", "''")}' (FORMAT PARQUET);
                """
            )
            sources = {}
            for latitude in (0, 60):
                h3_index = h3.latlng_to_cell(latitude, 0, 7)
                h3_base_cell = base_cell(h3_index)
                source = root / f"base_{h3_base_cell}.parquet"
                connection.execute(
                    f"COPY (SELECT {h3.str_to_int(h3_index)}::UBIGINT AS h3_cell, "
                    f"[101::BIGINT] AS species_ids) TO "
                    f"'{str(source).replace("'", "''")}' (FORMAT PARQUET)"
                )
                sources[h3_base_cell] = source
            connection.close()

            pending = sorted(sources)
            display = BuildDisplay(
                total=2,
                workers=2,
                memory_limit="512MB",
                output_dir=output_dir,
                work_weights={base_cell: 1 for base_cell in pending},
                enabled=False,
            )
            updates = []
            original_progress = display.partition_progress

            def record_progress(base_cell, phase, fraction):
                updates.append((base_cell, phase, fraction))
                original_progress(base_cell, phase, fraction)

            display.partition_progress = record_progress
            display.begin_aggregation(2)
            _build_parts_parallel(
                SimpleNamespace(
                    workers=2,
                    species=species,
                    species_systems=systems,
                    scratch_dir=root / "scratch",
                    memory_limit="512MB",
                    threads=1,
                    output_dir=output_dir,
                ),
                sources,
                pending,
                display,
            )

            self.assertEqual(display.completed, 2)
            self.assertEqual(
                completed_parts(output_dir),
                {
                    base_cell: output_dir / f"base_{base_cell}.parquet"
                    for base_cell in pending
                },
            )
            self.assertIn("Aggregating", {phase for _, phase, _ in updates})

    def test_progress_display_has_a_non_interactive_lifecycle(self) -> None:
        output = io.StringIO()
        display = BuildDisplay(
            total=3,
            workers=1,
            memory_limit="750MB",
            output_dir=Path(tempfile.gettempdir()),
            enabled=True,
            console=Console(file=output, force_terminal=False, color_system=None),
        )
        report = {
            "rows": 10,
            "relationships": 20,
            "output_relationships": 20,
            "validation_seconds": 0.1,
            "bytes": 1_000_000,
            "seconds": 2.0,
        }

        with display:
            self.assertFalse(display.enabled)
            display.begin_validation(1)
            display.validated_existing(1, report)
            display.invalid_existing(2, RuntimeError("bad totals"))
            display.begin_aggregation(2)
            display.partition_started(2)
            self.assertIn(2, display.active)
            display.partition_progress(2, "Aggregating", 0.5)
            self.assertEqual(display.active_phase[2], "Aggregating")
            self.assertEqual(display.active_progress[2], 0.5)
            self.assertEqual(display._live_completed_work(), 1.5)
            display.partition_completed(2, report)
            display.partition_started(3)
            display.partition_failed(3, RuntimeError("worker stopped"))

        self.assertEqual(display.completed, 2)
        self.assertEqual(display.rebuilt, 1)
        self.assertFalse(display.active)
        text = output.getvalue()
        self.assertIn("Base 1 already valid", text)
        self.assertIn("Base 2 will be rebuilt", text)
        self.assertIn("Base 2 built", text)
        self.assertIn("Base 3 failed", text)
        self.assertIn("2/3 base cells validated", text)

    def test_partition_aggregation_is_wide_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            species = root / "species.parquet"
            systems = root / "systems.parquet"
            h3_index = h3.latlng_to_cell(56, 10, 7)
            h3_base_cell = base_cell(h3_index)
            source = root / f"base_{h3_base_cell}.parquet"
            target = root / "output" / f"base_{h3_base_cell}.parquet"
            connection = duckdb.connect()
            connection.execute(
                f"""
                COPY (SELECT '101'::VARCHAR AS gbif_accepted_id,
                    'Species one'::VARCHAR AS species_name, 'Family'::VARCHAR AS family,
                    'Endangered'::VARCHAR AS redlist_category,
                    false AS has_dna_species_level, true AS genus_has_dna,
                    true AS family_has_dna, false AS goat_data_deficient,
                    NULL::VARCHAR AS edge_group_name,
                    false AS meets_ebp)
                TO '{str(species).replace("'", "''")}' (FORMAT PARQUET);
                COPY (SELECT '101'::VARCHAR AS gbif_accepted_id,
                    'Terrestrial'::VARCHAR AS system)
                TO '{str(systems).replace("'", "''")}' (FORMAT PARQUET);
                COPY (SELECT {h3.str_to_int(h3_index)}::UBIGINT
                    AS h3_cell, [101::BIGINT] AS species_ids)
                TO '{str(source).replace("'", "''")}' (FORMAT PARQUET);
                """
            )
            configure_connection(
                connection, scratch_dir=root / "scratch",
                memory_limit="512MB", threads=1,
            )
            prepare_species(connection, species, systems)
            progress_updates = []
            report = aggregate_part(
                connection,
                source,
                target,
                progress=lambda phase, fraction: progress_updates.append(
                    (phase, fraction)
                ),
            )
            score_sql = score_expression(lambda metric: f'"{metric}__all"')
            row = connection.execute(
                f'SELECT "total_species__all", "endangered_count__all", '
                f'"priority_en_sp_count__all", "priority_en_fam_count__all", '
                f'"total_species__terrestrial", "total_species__marine", '
                f'{score_sql} AS score FROM read_parquet(?)',
                [str(target)],
            ).fetchone()
            connection.close()

            self.assertEqual(report["rows"], 1)
            self.assertEqual(report["relationships"], 1)
            self.assertEqual(progress_updates[-1], ("Complete", 1.0))
            self.assertTrue(
                {"Aggregating", "Checking source", "Checking output", "Publishing"}
                <= {phase for phase, _ in progress_updates}
            )
            self.assertEqual(row, (1, 1, 1, 0, 1, 0, 6.0))
            self.assertEqual(
                completed_parts(target.parent), {h3_base_cell: target}
            )

    def test_partition_validation_rejects_dropped_species_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            species = root / "species.parquet"
            systems = root / "systems.parquet"
            h3_index = h3.latlng_to_cell(56, 10, 7)
            h3_base_cell = base_cell(h3_index)
            source = root / f"base_{h3_base_cell}.parquet"
            target = root / "output" / f"base_{h3_base_cell}.parquet"
            connection = duckdb.connect()
            connection.execute(
                f"""
                COPY (SELECT '101'::VARCHAR AS gbif_accepted_id,
                    'Species one'::VARCHAR AS species_name, 'Family'::VARCHAR AS family,
                    'Endangered'::VARCHAR AS redlist_category,
                    false AS has_dna_species_level, true AS genus_has_dna,
                    true AS family_has_dna, false AS goat_data_deficient,
                    NULL::VARCHAR AS edge_group_name,
                    false AS meets_ebp)
                TO '{str(species).replace("'", "''")}' (FORMAT PARQUET);
                COPY (SELECT '101'::VARCHAR AS gbif_accepted_id,
                    'Terrestrial'::VARCHAR AS system)
                TO '{str(systems).replace("'", "''")}' (FORMAT PARQUET);
                COPY (SELECT {h3.str_to_int(h3_index)}::UBIGINT AS h3_cell,
                    [101::BIGINT, 999::BIGINT] AS species_ids)
                TO '{str(source).replace("'", "''")}' (FORMAT PARQUET);
                """
            )
            configure_connection(
                connection, scratch_dir=root / "scratch",
                memory_limit="512MB", threads=1,
            )
            prepare_species(connection, species, systems)

            with self.assertRaisesRegex(
                RuntimeError, "dropped_relationships=1"
            ):
                aggregate_part(connection, source, target)
            connection.close()

            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".building.parquet").exists())

    def test_partition_validation_rejects_inconsistent_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            h3_index = h3.latlng_to_cell(56, 10, 7)
            h3_base_cell = base_cell(h3_index)
            source = root / f"base_{h3_base_cell}.parquet"
            aggregate = root / "aggregate.parquet"
            columns = ", ".join(
                f"{int(system == 'all' and metric in {'total_species', 'endangered_count'})}::INTEGER "
                f'AS "{metric}__{system.lower()}"'
                for system in SYSTEMS
                for metric in METRICS
            )
            connection = duckdb.connect()
            connection.execute(
                f"COPY (SELECT {h3.str_to_int(h3_index)}::UBIGINT AS h3_cell, "
                f"[101::BIGINT] AS species_ids) TO "
                f"'{str(source).replace("'", "''")}' (FORMAT PARQUET)"
            )
            connection.execute(
                f"COPY (SELECT '{h3_index}' AS h3_index, {columns}) TO "
                f"'{str(aggregate).replace("'", "''")}' (FORMAT PARQUET)"
            )

            with self.assertRaisesRegex(
                RuntimeError, "output_inconsistent_metric_cells=1"
            ):
                validate_aggregate_part(connection, source, aggregate)
            connection.close()

    def test_on_demand_tile_reads_only_visible_aggregate_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            h3_index = h3.latlng_to_cell(45, 10, 7)
            target = root / f"base_{base_cell(h3_index)}.parquet"
            columns = ", ".join(
                f"{int(system == 'all' and metric == 'total_species')}::INTEGER "
                f"AS {metric}__{system.lower()}"
                for system in SYSTEMS
                for metric in METRICS
            )
            connection = duckdb.connect()
            connection.execute(
                f"COPY (SELECT '{h3_index}'::VARCHAR AS h3_index, {columns}) "
                f"TO '{str(target).replace("'", "''")}' (FORMAT PARQUET)"
            )
            connection.close()
            zoom = 8
            scale = 2**zoom
            x = int((10 + 180) / 360 * scale)
            y = int(
                (1 - math.asinh(math.tan(math.radians(45))) / math.pi)
                / 2
                * scale
            )
            render_tile.cache_clear()
            jurisdiction_path = str(
                Path(__file__).resolve().parents[1]
                / "app" / "static" / "data" / "boundaries" / "admin0.geojson"
            )
            admin1_path = str(Path(jurisdiction_path).with_name("admin1.geojson"))
            municipality_path = str(
                Path(jurisdiction_path).with_name("municipality.geojson")
            )
            conservation_path = str(
                Path(jurisdiction_path).with_name("conservation-framework.geojson")
            )
            with patch(
                "backend.res7_tiles.load_jurisdiction_index",
                wraps=load_jurisdiction_index,
            ) as load_index:
                payload = json.loads(render_tile(
                    str(root), zoom, x, y, "all", 1,
                    jurisdiction_path, ("ITA",), admin1_path, ("ITA-5361",),
                    municipality_path, (), conservation_path, ("ECO-675",),
                ))

            self.assertCountEqual(
                [call.args[0] for call in load_index.call_args_list],
                [jurisdiction_path, admin1_path, conservation_path],
            )
            self.assertNotIn(
                municipality_path,
                [call.args[0] for call in load_index.call_args_list],
            )

            self.assertEqual(len(payload["cells"]), 1)
            cell = payload["cells"][0]
            self.assertEqual(cell[0], h3_index)
            self.assertEqual(cell[1], 1)
            self.assertEqual(cell[2:], [0] * (len(METRICS) - 1))
            render_tile.cache_clear()
            excluded = json.loads(render_tile(
                str(root), zoom, x, y, "all", 1,
                jurisdiction_path, ("DNK",), admin1_path, (),
            ))
            self.assertEqual(excluded["cells"], [])

    def test_old_metric_partitions_are_not_advertised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            stale = root / "base_1.parquet"
            current = root / "base_2.parquet"
            current_columns = ", ".join(
                f"0::INTEGER AS {metric}__{system.lower()}"
                for system in SYSTEMS
                for metric in METRICS
            )
            connection = duckdb.connect()
            connection.execute(
                f"COPY (SELECT 'stale' AS h3_index, 1::INTEGER AS total_species__all) "
                f"TO '{str(stale).replace("'", "''")}' (FORMAT PARQUET)"
            )
            connection.execute(
                f"COPY (SELECT 'current' AS h3_index, {current_columns}) "
                f"TO '{str(current).replace("'", "''")}' (FORMAT PARQUET)"
            )
            connection.close()

            self.assertEqual(available_base_cells(root), [2])

    def test_coverage_version_changes_when_a_partition_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            current = root / "base_2.parquet"
            replacement = root / "replacement.parquet"
            columns = ", ".join(
                f"0::INTEGER AS {metric}__{system.lower()}"
                for system in SYSTEMS for metric in METRICS
            )
            connection = duckdb.connect()
            for path, h3_index in (
                (current, "872830828ffffff"),
                (replacement, "87283082affffff"),
            ):
                connection.execute(
                    f"COPY (SELECT '{h3_index}' AS h3_index, {columns}) TO "
                    f"'{str(path).replace("'", "''")}' (FORMAT PARQUET)"
                )
            connection.close()

            cells_before, version_before = aggregate_coverage(root)
            replacement.replace(current)
            cells_after, version_after = aggregate_coverage(root)

            self.assertEqual(cells_before, cells_after)
            self.assertNotEqual(version_before, version_after)


if __name__ == "__main__":
    unittest.main()
