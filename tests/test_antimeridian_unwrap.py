from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon

from ark_pipeline.spatial.coverage import (
    GeometryCoverageError,
    exact_intersecting_cells_native,
    load_spatial_profile,
    unwrap_antimeridian,
)


class AntimeridianUnwrapTests(unittest.TestCase):
    def test_global_shell_keeps_distant_holes_and_original_coordinates(self):
        # No edge crosses the seam, but the western vertex density makes the
        # arithmetic mean far from the eastern hole. This reproduced the bug.
        shell = [(-179, -20), (-90, -20), (0, -20), (90, -20), (179, -20),
                 (179, 20), (90, 20), (0, 20), (-90, 20), (-179, 20),
                 *[(-179, y) for y in range(19, -20, -1)]]
        hole = [(171, -1), (173, -1), (173, 1), (171, 1)]
        source = Polygon(shell, [hole])
        self.assertTrue(source.is_valid)
        actual = unwrap_antimeridian(source)
        self.assertIs(actual, source)
        self.assertEqual(actual.wkb, source.wkb)
        self.assertFalse(actual.covers(Point(172, 0)))
        multi = MultiPolygon([source])
        self.assertIs(unwrap_antimeridian(multi), multi)

    def test_crossing_shell_places_hole_by_containment_regardless_of_start_vertex(self):
        shell = [(179, -3), (-179, -3), (-179, 3), (179, 3), (179, -3)]
        # The hole starts on the other side of the seam from the shell.
        hole = [(-179.5, -1), (179.5, -1), (179.5, 1), (-179.5, 1), (-179.5, -1)]
        actual = unwrap_antimeridian(Polygon(shell, [hole]))
        expected = Polygon([(179, -3), (181, -3), (181, 3), (179, 3)],
                           [[(179.5, -1), (180.5, -1), (180.5, 1), (179.5, 1)]])
        self.assertTrue(actual.is_valid)
        self.assertTrue(actual.equals(expected))
        self.assertFalse(actual.covers(Point(180, 0)))
        # Reversing winding and changing the shell's starting vertex must keep
        # the same physical range, although its longitude copy may differ.
        reverse = unwrap_antimeridian(Polygon(shell[::-1], [hole[::-1]]))
        self.assertTrue(reverse.equals(actual))
        from shapely import affinity
        shifted_start = unwrap_antimeridian(Polygon(shell[1:-1] + shell[:2], [hole]))
        self.assertTrue(affinity.translate(shifted_start, xoff=360).equals(actual))

    def test_unplaceable_hole_fails_instead_of_dropping_or_repairing_it(self):
        shell = [(179, -2), (-179, -2), (-179, 2), (179, 2)]
        hole = [(10, -1), (11, -1), (11, 1), (10, 1)]
        with self.assertRaisesRegex(GeometryCoverageError, "Cannot place unwrapped hole"):
            unwrap_antimeridian(Polygon(shell, [hole]))

    def test_dateline_cell_coverage_preserves_the_hole(self):
        profile = replace(load_spatial_profile(Path(__file__).resolve().parents[1] / "config/spatial_semantics_iucn_richness_v3.toml"),
                          resolution=4, decision_simplification_degrees=0)
        # Both sides are explicitly in the same continuous longitude frame.
        crossing = Polygon([(179, -3), (181, -3), (181, 3), (179, 3)],
                           [[(-179.5, -1), (-179.5, 1), (-180.5, 1), (-180.5, -1)]])
        # Use the unwrapping routine directly for the mixed-frame input, which
        # is not a valid planar source geometry before its rings are aligned.
        wrapped_hole = Polygon([(179, -3), (-179, -3), (-179, 3), (179, 3)],
                               [list(crossing.interiors[0].coords)])
        corrected = unwrap_antimeridian(wrapped_hole)
        expected = Polygon([(179, -3), (181, -3), (181, 3), (179, 3)],
                           [[(179.5, -1), (179.5, 1), (180.5, 1), (180.5, -1)]])
        np.testing.assert_array_equal(exact_intersecting_cells_native(corrected, profile).cells,
                                      exact_intersecting_cells_native(expected, profile).cells)


if __name__ == "__main__":
    unittest.main()
