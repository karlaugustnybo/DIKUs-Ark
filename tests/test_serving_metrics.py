from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import h3
import pyarrow as pa
import pyarrow.parquet as pq

from ark_pipeline.aggregation.metrics import METRIC_SCHEMA, aggregate_species_lists
from ark_pipeline.aggregation.species_lists import LIST_SCHEMA
from ark_pipeline.builders.fine_metrics import aggregate_columns, aggregate_part


class ServingMetricsTests(unittest.TestCase):
    def test_native_matches_sql_for_all_traits_nulls_systems_and_batch_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, duckdb.connect() as connection:
            root = Path(temporary)
            categories = [
                "Critically Endangered", "Endangered", "Vulnerable", "Near Threatened",
                "Data Deficient", "Least Concern", "Extinct", None,
            ]
            rows = []
            for identifier, traits in enumerate(itertools.product(
                categories, (True, False, None), (True, False, None),
                (True, False, None), (True, False, None), range(8),
            ), start=1):
                category, species, genus, family, deficient, mask = traits
                rows.append({
                    "gbif_accepted_id": str(identifier), "redlist_category": category,
                    "has_dna_species_level": species, "genus_has_dna": genus,
                    "family_has_dna": family, "goat_data_deficient": deficient,
                    "is_terrestrial": bool(mask & 1), "is_freshwater": bool(mask & 2),
                    "is_marine": bool(mask & 4),
                })
            # These must not alias numeric species 1 or 2 when the join becomes numeric.
            rows.extend({**rows[0], "gbif_accepted_id": value} for value in ("01", "1.5", "other"))
            connection.register("metadata", pa.Table.from_pylist(rows))
            connection.execute("CREATE TEMP TABLE species AS SELECT * FROM metadata")
            ids = list(range(1, len(rows) - 2))
            cells = sorted(h3.grid_disk(h3.latlng_to_cell(55.67, 12.56, 7), 1))
            source = root / "lists.parquet"
            target = root / "metrics.parquet"
            pq.write_table(pa.Table.from_pylist([
                {"h3_cell": h3.str_to_int(cell), "species_ids": values}
                for cell, values in zip(cells, [ids, ids[::2], [1], ids[1::2], [2, 4], ids, [3]])
            ], schema=LIST_SCHEMA), source, row_group_size=3)
            connection.execute(f"""
                CREATE TEMP TABLE expected AS
                WITH cells AS (
                    SELECT lower(to_hex(h3_cell)) AS h3_index,
                           cast(ids.species_id AS VARCHAR) AS gbif_accepted_id
                    FROM read_parquet(?), unnest(species_ids) AS ids(species_id)
                )
                SELECT h3_index, {', '.join(aggregate_columns())}
                FROM cells JOIN species USING (gbif_accepted_id) GROUP BY h3_index
            """, [str(source)])
            aggregate_species_lists(
                connection, source, target, batch_cells=2, relationship_chunk=7
            )
            self.assertEqual(pq.read_schema(target), METRIC_SCHEMA)
            difference = connection.execute("""
                (SELECT * FROM expected EXCEPT ALL SELECT * FROM read_parquet($target))
                UNION ALL
                (SELECT * FROM read_parquet($target) EXCEPT ALL SELECT * FROM expected)
            """, {"target": str(target)}).fetchall()
            self.assertEqual(difference, [])

    def test_missing_null_and_noncanonical_ids_do_not_acquire_metric_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, duckdb.connect() as connection:
            root = Path(temporary)
            connection.execute("""
                CREATE TEMP TABLE species AS SELECT '01' AS gbif_accepted_id,
                    'Endangered' AS redlist_category, true AS has_dna_species_level,
                    true AS genus_has_dna, true AS family_has_dna,
                    false AS goat_data_deficient, true AS is_terrestrial,
                    false AS is_freshwater, false AS is_marine
            """)
            source, target = root / "lists.parquet", root / "metrics.parquet"
            cell = h3.str_to_int(h3.latlng_to_cell(0, 0, 7))
            pq.write_table(pa.Table.from_pylist([
                {"h3_cell": cell, "species_ids": [1, None, 999]},
            ], schema=LIST_SCHEMA), source)
            aggregate_species_lists(connection, source, target)
            self.assertEqual(pq.read_table(target)["total_species__all"].to_pylist(), [0])

    def test_interrupted_native_write_preserves_previous_output_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, duckdb.connect() as connection:
            root = Path(temporary)
            target = root / "base_1.parquet"
            target.write_bytes(b"previous output")

            def interrupted(connection, source, temporary, **kwargs):
                temporary.write_bytes(b"incomplete")
                raise RuntimeError("interrupted")

            with patch("ark_pipeline.builders.fine_metrics.aggregate_species_lists", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    aggregate_part(connection, root / "source.parquet", target)
            self.assertEqual(target.read_bytes(), b"previous output")
            self.assertFalse(target.with_suffix(".building.parquet").exists())


if __name__ == "__main__":
    unittest.main()
