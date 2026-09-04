from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from shapely.geometry import MultiPolygon, Polygon, box

from ark_pipeline.spatial.coverage import (
    _simplified_decision_geometry,
    exact_intersecting_cells_native,
    load_spatial_profile,
)
from ark_pipeline.spatial.tile_parallel import TileBudget


class TileParallelTests(unittest.TestCase):
    def test_parallel_matches_serial_for_holes_components_dateline_and_tile_edges(self):
        profile = replace(load_spatial_profile(Path(__file__).resolve().parents[1] / "config/spatial_semantics_iucn_richness_v3.toml"),
                          resolution=5, candidate_tile_degrees=0.25, decision_simplification_degrees=0)
        geometries = [
            Polygon([(0, 0), (2, 0), (2, 2), (0, 2)], holes=[[(0.5, 0.5), (0.5, 1.5), (1.5, 1.5), (1.5, 0.5)]]),
            MultiPolygon([box(0, 0, 1, 1), box(5, 5, 6, 6)]),
            box(179, -0.5, 181, 0.5), box(10, 10, 10.5, 12),
        ]
        for geometry in geometries:
            with self.subTest(bounds=geometry.bounds):
                serial = exact_intersecting_cells_native(geometry, profile)
                slots = threading.BoundedSemaphore(4)
                budget = TileBudget(slots, 4)
                with slots:
                    parallel = exact_intersecting_cells_native(geometry, profile, tile_budget=budget)
                np.testing.assert_array_equal(parallel.cells, serial.cells)
                self.assertEqual(parallel.candidate_cells, serial.candidate_cells)
                self.assertGreater(budget.peak_workers, 1)

    def test_two_polygons_share_one_budget_without_oversubscription(self):
        slots = threading.BoundedSemaphore(4)
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active = peak = 0

        def job(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.005)
            with lock:
                active -= 1
            return value

        def polygon(offset):
            with slots:
                barrier.wait(timeout=5)
                return list(TileBudget(slots, 4).map(job, range(offset, offset + 24)))

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(polygon, (0, 24)))
        self.assertEqual(sorted(results[0] + results[1]), list(range(48)))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)

    def test_exception_and_early_close_release_borrowed_slots(self):
        for failure in (False, True):
            slots = threading.BoundedSemaphore(4)

            def job(value):
                if failure and value == 3:
                    raise ValueError("deliberate tile failure")
                time.sleep(0.002)
                return value

            with slots:
                iterator = TileBudget(slots, 4).map(job, range(100))
                if failure:
                    with self.assertRaisesRegex(ValueError, "tile failure"):
                        list(iterator)
                else:
                    next(iterator)
                    iterator.close()
            self.assertTrue(all(slots.acquire(False) for _ in range(4)))
            self.assertFalse(slots.acquire(False))

    def test_simplification_audit_explains_each_rejected_attempt(self):
        source = MultiPolygon([box(0, 0, 2, 2), box(10, 10, 10.001, 10.001)])
        invalid = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
        audit = {}
        with patch("ark_pipeline.spatial.coverage.shapely.simplify", side_effect=[box(0, 0, 2, 2), invalid]):
            result = _simplified_decision_geometry(source, 0.01, audit=audit)
        self.assertIs(result, source)
        self.assertEqual(audit["method"], "original")
        self.assertIn("component count changed: 2 -> 1", audit["rejections"][0]["reasons"])
        self.assertTrue(any(reason.startswith("invalid:") for reason in audit["rejections"][1]["reasons"]))
        self.assertEqual(audit["original_coordinates"], audit["result_coordinates"])


if __name__ == "__main__":
    unittest.main()
