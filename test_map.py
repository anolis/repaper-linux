#!/usr/bin/env python3
"""
Tests for the monitor mapping matrix.

The matrix maps the whole tablet surface onto one monitor, expressed in
fractions of the whole desktop. Pure arithmetic, so no X session needed.
"""

import unittest

import repaper_map

# The layout this was developed against: three monitors, 5040x1920 desktop.
DESKTOP = (5040, 1920)
DP5 = ('DP-5', 1680, 1050, 1200, 467)
DP0 = ('DP-0', 1200, 1920, 0, 0)
SURFACE_ASPECT = 214.5 / 157.7          # about 1.360


def region(matrix, desktop):
    """Recover the pixel rectangle a matrix maps onto."""
    return (matrix[0] * desktop[0], matrix[4] * desktop[1],
            matrix[2] * desktop[0], matrix[5] * desktop[1])


class StretchTest(unittest.TestCase):
    """Without aspect correction the surface fills the monitor exactly."""

    def test_covers_the_whole_monitor(self):
        matrix = repaper_map.compute_matrix(DP5, DESKTOP, aspect=None)
        width, height, x, y = region(matrix, DESKTOP)
        self.assertAlmostEqual(width, 1680, places=3)
        self.assertAlmostEqual(height, 1050, places=3)

    def test_lands_at_the_monitor_offset(self):
        matrix = repaper_map.compute_matrix(DP5, DESKTOP, aspect=None)
        _, _, x, y = region(matrix, DESKTOP)
        self.assertAlmostEqual(x, 1200, places=3)
        self.assertAlmostEqual(y, 467, places=3)

    def test_has_no_skew_terms(self):
        matrix = repaper_map.compute_matrix(DP5, DESKTOP, aspect=None)
        self.assertEqual([matrix[1], matrix[3], matrix[6], matrix[7]],
                         [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(matrix[8], 1.0)


class AspectTest(unittest.TestCase):
    """With correction, the mapped area keeps the surface's proportions."""

    def test_wide_monitor_is_limited_by_height(self):
        # Monitor 1.600 is wider than the surface 1.360, so height fills and
        # width shrinks to 1050 * 1.360.
        matrix = repaper_map.compute_matrix(DP5, DESKTOP, SURFACE_ASPECT)
        width, height, _, _ = region(matrix, DESKTOP)
        self.assertAlmostEqual(height, 1050, places=3)
        self.assertAlmostEqual(width, 1050 * SURFACE_ASPECT, places=3)
        self.assertLess(width, 1680)

    def test_tall_monitor_is_limited_by_width(self):
        # DP-0 is portrait, far narrower than the surface, so width fills.
        matrix = repaper_map.compute_matrix(DP0, DESKTOP, SURFACE_ASPECT)
        width, height, _, _ = region(matrix, DESKTOP)
        self.assertAlmostEqual(width, 1200, places=3)
        self.assertAlmostEqual(height, 1200 / SURFACE_ASPECT, places=3)
        self.assertLess(height, 1920)

    def test_mapped_region_keeps_the_surface_aspect(self):
        for monitor in (DP5, DP0):
            matrix = repaper_map.compute_matrix(monitor, DESKTOP,
                                                SURFACE_ASPECT)
            width, height, _, _ = region(matrix, DESKTOP)
            self.assertAlmostEqual(width / height, SURFACE_ASPECT, places=6)

    def test_region_is_centred_on_the_monitor(self):
        matrix = repaper_map.compute_matrix(DP5, DESKTOP, SURFACE_ASPECT)
        width, height, x, y = region(matrix, DESKTOP)
        self.assertAlmostEqual(x - 1200, (1680 - width) / 2, places=3)
        self.assertAlmostEqual(y - 467, (1050 - height) / 2, places=3)

    def test_region_stays_inside_the_monitor(self):
        for monitor in (DP5, DP0):
            _, mon_w, mon_h, mon_x, mon_y = monitor
            matrix = repaper_map.compute_matrix(monitor, DESKTOP,
                                                SURFACE_ASPECT)
            width, height, x, y = region(matrix, DESKTOP)
            self.assertGreaterEqual(round(x, 6), mon_x)
            self.assertGreaterEqual(round(y, 6), mon_y)
            self.assertLessEqual(round(x + width, 6), mon_x + mon_w)
            self.assertLessEqual(round(y + height, 6), mon_y + mon_h)

    def test_matching_aspect_fills_the_monitor(self):
        square = ('SQ', 1000, 1000, 0, 0)
        matrix = repaper_map.compute_matrix(square, (1000, 1000), aspect=1.0)
        width, height, _, _ = region(matrix, (1000, 1000))
        self.assertAlmostEqual(width, 1000, places=3)
        self.assertAlmostEqual(height, 1000, places=3)


class IdentityTest(unittest.TestCase):

    def test_reset_matrix_is_the_identity(self):
        self.assertEqual(repaper_map.IDENTITY,
                         [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0])

    def test_full_desktop_mapping_equals_identity(self):
        whole = ('ALL', DESKTOP[0], DESKTOP[1], 0, 0)
        matrix = repaper_map.compute_matrix(whole, DESKTOP, aspect=None)
        for value, expected in zip(matrix, repaper_map.IDENTITY):
            self.assertAlmostEqual(value, expected, places=9)


if __name__ == '__main__':
    unittest.main()
