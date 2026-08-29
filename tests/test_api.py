from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from litestar.exceptions import HTTPException

from backend.app import (
    MAX_SEARCH_LENGTH,
    _cell_boundary_memberships,
    _clean_search,
    cell_species,
    resolution7_tile,
    species_cells,
    species_page,
    species_suggestions,
)


class RecordingPool:
    def __init__(
        self, *, best_rank: int = 1, total: int = 1, literal_total: int | None = None
    ) -> None:
        self.best_rank = best_rank
        self.total = total
        self.literal_total = literal_total
        self.calls: list[tuple[str, str, tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *values: object) -> dict[str, object]:
        self.calls.append(("fetchrow", sql, values))
        if self.literal_total is not None and len(self.calls) == 1:
            return {"total": self.literal_total, "best_rank": None}
        return {"total": self.total, "best_rank": self.best_rank}

    async def fetch(self, sql: str, *values: object) -> list[dict[str, object]]:
        self.calls.append(("fetch", sql, values))
        return [{
            "gbif_accepted_id": "15955",
            "iucn_sis_id": "15955",
            "iucn_assessment_id": "140552175",
            "gbif_taxon_id": "5219404",
            "goat_taxon_id": "9689",
            "species_name": "Panthera leo",
            "family": "Felidae",
            "redlist_category": "Vulnerable",
            "threat_score": 2.0,
            "dna_level": "Missing Species (2.0)",
            "priority": 4.0,
        }]


class SuggestionPool:
    def __init__(self, *responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *values: object) -> list[dict[str, object]]:
        self.calls.append((sql, values))
        return self.responses.pop(0) if self.responses else []


SUGGESTION_ROW = {
    "gbif_accepted_id": "15955",
    "species_name": "Panthera leo",
    "family": "Felidae",
}


async def run_search(pool: RecordingPool, search: str):
    with patch("backend.app.get_pool", return_value=pool):
        return await species_page.fn(
            state=object(), search=search, page=1, per_page=10
        )


class SearchInputTests(unittest.TestCase):
    def test_whitespace_is_normalized(self) -> None:
        self.assertEqual(_clean_search("  Panthera\t  leo  "), "Panthera leo")

    def test_regex_and_sql_metacharacters_are_ordinary_text(self) -> None:
        self.assertEqual(_clean_search(r"^[A-Z] 100%_\\$"), r"^[A-Z] 100%_\\$")

    def test_overlong_input_has_a_clear_client_error(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _clean_search("x" * (MAX_SEARCH_LENGTH + 1))
        self.assertEqual(raised.exception.status_code, 400)

    def test_control_characters_are_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            _clean_search("Panthera\x00leo")


class SpeciesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_keeps_global_filters_and_ranks_fuzzy_matches_last(self) -> None:
        pool = RecordingPool(best_rank=4, literal_total=0)
        result = await run_search(pool, "Pantera")
        summary_sql = pool.calls[0][1]
        fuzzy_summary_sql = pool.calls[1][1]
        result_sql = pool.calls[2][1]

        self.assertIn("boundary_species AS", summary_sql)
        self.assertIn("escape_species_like(value)", summary_sql)
        self.assertNotIn("length(search_query.value) >= 3", summary_sql)
        self.assertIn("length(search_query.value) >= 3", fuzzy_summary_sql)
        self.assertIn("ORDER BY match_rank ASC", result_sql)
        self.assertIn("similarity(normalize_species_search(species_name)", result_sql)
        self.assertTrue(result.suggested)

    async def test_species_page_exposes_exact_source_identifiers(self) -> None:
        result = await run_search(RecordingPool(), "Panthera")
        row = result.rows[0]
        self.assertEqual(row.iucn_sis_id, "15955")
        self.assertEqual(row.gbif_taxon_id, "5219404")
        self.assertEqual(row.goat_taxon_id, "9689")

    async def test_equal_dna_scores_rank_missing_family_before_goat_data_deficient(self) -> None:
        pool = RecordingPool()
        with patch("backend.app.get_pool", return_value=pool):
            await species_page.fn(
                state=object(), search="", sort="dna_level", order="desc",
                page=1, per_page=10,
            )

        result_sql = pool.calls[-1][1]
        self.assertIn("dna_level_score DESC, dna_level_rank ASC", result_sql)
        self.assertIn("WHEN goat_data_deficient = true THEN 2", result_sql)
        self.assertIn("WHEN family_has_dna = false THEN 1", result_sql)

    async def test_equal_priorities_rank_missing_family_before_goat_data_deficient(self) -> None:
        pool = RecordingPool()
        with patch("backend.app.get_pool", return_value=pool):
            await species_page.fn(
                state=object(), search="", sort="priority", order="desc",
                page=1, per_page=10,
            )

        result_sql = pool.calls[-1][1]
        self.assertIn("priority DESC, dna_level_rank ASC", result_sql)

    async def test_species_cells_returns_available_compact_resolution(self) -> None:
        class CoveragePool:
            async def fetchrow(self, _sql: str, *values: object) -> dict[str, object]:
                self.values = values
                return {
                    "species_name": "Panthera leo",
                    "resolution": 3,
                    "cells": ["831f8dfffffffff", "831f8cfffffffff"],
                }

        pool = CoveragePool()
        with patch("backend.app.get_pool", return_value=pool):
            result = await species_cells.fn(
                state=object(), gbif_accepted_id="15955", resolution=7
            )

        self.assertEqual(result.resolution, 3)
        self.assertEqual(result.cells, ["831f8dfffffffff", "831f8cfffffffff"])
        self.assertEqual(pool.values, ("15955", 7))

    async def test_empty_cell_details_keep_resolution_and_boundary_context(self) -> None:
        class EmptyCellPool:
            async def fetchval(self, _sql: str, *_values: object) -> None:
                return None

        memberships = [SimpleNamespace(
            framework="admin0", framework_name="Countries & territories",
            code="ITA", name="Italy",
        )]
        with (
            patch("backend.app.get_pool", return_value=EmptyCellPool()),
            patch("backend.app._cell_boundary_memberships", return_value=memberships),
            patch("backend.app._external_res7_species_ids", return_value=[]),
        ):
            result = await cell_species.fn(
                state=object(), h3_index="871ea6d65ffffff", resolution=7
            )

        self.assertEqual(result.resolution, 7)
        self.assertEqual(result.boundaries, memberships)
        self.assertEqual(result.stats.total, 0)

    async def test_suggestions_use_one_compact_prefix_query(self) -> None:
        pool = SuggestionPool([SUGGESTION_ROW])
        with patch("backend.app.get_pool", return_value=pool):
            result = await species_suggestions.fn(
                state=object(), search="  Panthera  ", limit=8
            )

        self.assertEqual(result.rows[0].species_name, "Panthera leo")
        self.assertFalse(result.suggested)
        self.assertEqual(len(pool.calls), 1)
        sql, values = pool.calls[0]
        self.assertIn("LIKE search_query.pattern || '%'", sql)
        self.assertIn("LIKE '%' || search_query.pattern || '%'", sql)
        self.assertNotIn("COUNT(", sql)
        self.assertNotIn("threat_score", sql)
        self.assertEqual(values, ("Panthera", 8))

    async def test_suggestions_only_try_fuzzy_search_after_prefix_misses(self) -> None:
        pool = SuggestionPool([], [SUGGESTION_ROW])
        with patch("backend.app.get_pool", return_value=pool):
            result = await species_suggestions.fn(
                state=object(), search="Pantera", limit=5
            )

        self.assertEqual(len(pool.calls), 2)
        self.assertIn(" % search_query.value", pool.calls[1][0])
        self.assertEqual(pool.calls[1][1], ("Pantera", 5))
        self.assertTrue(result.suggested)

    async def test_suggestions_skip_database_for_single_character_query(self) -> None:
        pool = SuggestionPool([SUGGESTION_ROW])
        with patch("backend.app.get_pool", return_value=pool):
            result = await species_suggestions.fn(
                state=object(), search="P", limit=8
            )

        self.assertEqual(result.rows, [])
        self.assertEqual(pool.calls, [])


class Resolution7TileTests(unittest.IsolatedAsyncioTestCase):
    def settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            res7_aggregate_parts_dir=Path("/tmp/res7-aggregates"),
            jurisdictions_path=Path("/boundaries/admin0.geojson"),
            admin1_boundaries_path=Path("/boundaries/admin1.geojson"),
            municipality_boundaries_path=Path("/boundaries/municipality.geojson"),
            eez_boundaries_path=Path("/boundaries/eez.geojson"),
            conservation_boundaries_path=Path("/boundaries/conservation.geojson"),
        )

    async def test_unfiltered_tile_does_not_load_any_boundary_index(self) -> None:
        with (
            patch("backend.app.settings", self.settings()),
            patch("backend.app._aggregate_coverage", return_value=((1,), 123)),
            patch("backend.app.load_jurisdiction_index") as load_index,
            patch(
                "backend.app.render_tile",
                return_value=b'{"cells":[]}',
            ) as render,
        ):
            await resolution7_tile.fn(z=8, x=128, y=128)

        load_index.assert_not_called()
        self.assertEqual(
            render.call_args.args[6:],
            ("", (), "", (), "", (), "", (), "", ()),
        )

    async def test_only_active_boundary_index_is_validated_and_rendered(self) -> None:
        settings = self.settings()
        index = SimpleNamespace(codes=("ITA-5361",))
        with (
            patch("backend.app.settings", settings),
            patch("backend.app._aggregate_coverage", return_value=((1,), 123)),
            patch("backend.app.load_jurisdiction_index", return_value=index) as load_index,
            patch(
                "backend.app.render_tile",
                return_value=b'{"cells":[]}',
            ) as render,
        ):
            await resolution7_tile.fn(
                z=8, x=128, y=128, admin1="ITA-5361"
            )

        load_index.assert_called_once_with(str(settings.admin1_boundaries_path))
        self.assertEqual(
            render.call_args.args[6:],
            (
                "", (),
                str(settings.admin1_boundaries_path), ("ITA-5361",),
                "", (),
                "", (),
                "", (),
            ),
        )


class CellBoundaryMembershipTests(unittest.TestCase):
    def test_memberships_include_every_ready_framework_and_deduplicate_codes(self) -> None:
        test_settings = SimpleNamespace(
            jurisdictions_path=Path("/boundaries/admin0.geojson"),
            admin1_boundaries_path=Path("/boundaries/admin1.geojson"),
            municipality_boundaries_path=Path("/boundaries/municipality.geojson"),
            eez_boundaries_path=Path("/boundaries/eez.geojson"),
            conservation_boundaries_path=Path("/boundaries/conservation.geojson"),
        )
        indexes = {
            str(test_settings.jurisdictions_path): SimpleNamespace(
                names={"ITA": "Italy"}, codes_for_cell=lambda _h3: ("ITA", "ITA")
            ),
            str(test_settings.admin1_boundaries_path): SimpleNamespace(
                names={"ITA-5362": "Lombardia"},
                codes_for_cell=lambda _h3: ("ITA-5362",),
            ),
            str(test_settings.municipality_boundaries_path): SimpleNamespace(
                names={}, codes_for_cell=lambda _h3: ()
            ),
            str(test_settings.eez_boundaries_path): SimpleNamespace(
                names={}, codes_for_cell=lambda _h3: ()
            ),
            str(test_settings.conservation_boundaries_path): SimpleNamespace(
                names={"ECO-675": "Po Basin mixed forests"},
                codes_for_cell=lambda _h3: ("ECO-675",),
            ),
        }
        with (
            patch("backend.app.settings", test_settings),
            patch("backend.app.Path.is_file", return_value=True),
            patch(
                "backend.app.load_jurisdiction_index",
                side_effect=lambda path: indexes[path],
            ),
        ):
            memberships = _cell_boundary_memberships("871ea6d65ffffff")

        self.assertEqual(
            [(item.framework, item.code, item.name) for item in memberships],
            [
                ("admin0", "ITA", "Italy"),
                ("admin1", "ITA-5362", "Lombardia"),
                ("conservation_framework", "ECO-675", "Po Basin mixed forests"),
            ],
        )

if __name__ == "__main__":
    unittest.main()
