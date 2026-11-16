"""Tests for the design tokens (fyrn/theme.py). No Qt needed:
the tokens and the contrast maths are plain data."""

import re
import unittest
from pathlib import Path

from fyrn.theme import (DARK, HIGH_CONTRAST, build_stylesheet,
                        contrast_ratio, fit_badges)

_HERE = Path(__file__).resolve().parent
_PACKAGE = _HERE.parent / "fyrn"

# Every text/background pair the app actually shows, per theme.
# The dark theme's pairs are listed for the record (they are the
# look Fyrn has always had); only the high-contrast pairs carry
# the 7:1 bar.
_HIGH_CONTRAST_PAIRS = [
    ("text on bg", "text", "bg"),
    ("text on raised", "text", "bg_raised"),
    ("text on deep", "text", "bg_deep"),
    ("text on button", "text", "bg_button"),
    ("muted on bg", "text_muted", "bg"),
    ("muted on raised", "text_muted", "bg_raised"),
    ("dim on bg", "text_dim", "bg"),
    ("dim on deep", "text_dim", "bg_deep"),
    ("disabled on button", "text_disabled", "bg_button"),
    ("accent on bg", "accent", "bg"),
    ("accent on button", "accent", "bg_button"),
    ("selection text on selection bg", "selection_text",
     "selection_bg"),
    ("badge text on fit_ok", "badge_text", "fit_ok"),
    ("badge text on fit_tight", "badge_text", "fit_tight"),
    ("badge text on fit_big", "badge_text", "fit_big"),
    ("badge text on fit_unknown", "badge_text", "fit_unknown"),
]


class TokenTests(unittest.TestCase):
    def test_both_themes_have_the_same_keys(self):
        self.assertEqual(set(DARK), set(HIGH_CONTRAST))

    def test_all_values_are_hex_colours(self):
        pattern = re.compile(r"^#[0-9a-f]{6}$")
        for theme in (DARK, HIGH_CONTRAST):
            for key, value in theme.items():
                self.assertRegex(value, pattern, key)

    def test_focus_ring_differs_from_accent(self):
        # Focus must never read as selection: the two colours
        # are different in both themes.
        for theme in (DARK, HIGH_CONTRAST):
            self.assertNotEqual(theme["focus_ring"],
                                theme["accent"])

    def test_stylesheet_has_no_fixed_point_size(self):
        # No font-size anywhere: the application font (and the
        # user's system text size) shows through.
        for theme in (DARK, HIGH_CONTRAST):
            sheet = build_stylesheet(theme)
            self.assertNotIn("font-size", sheet)
            self.assertNotIn("pt;", sheet)

    def test_stylesheet_uses_the_tokens(self):
        dark = build_stylesheet(DARK)
        self.assertIn(DARK["accent"], dark)
        self.assertIn(DARK["bg"], dark)
        high = build_stylesheet(HIGH_CONTRAST)
        self.assertIn(HIGH_CONTRAST["accent"], high)
        self.assertNotEqual(dark, high)

    def test_fit_badges_follow_the_theme(self):
        dark = fit_badges(DARK)
        high = fit_badges(HIGH_CONTRAST)
        self.assertEqual(dark["fits"][0], "Fits")
        self.assertEqual(dark["fits"][1], DARK["fit_ok"])
        self.assertEqual(high["fits"][1], HIGH_CONTRAST["fit_ok"])
        self.assertNotEqual(dark["fits"][1], high["fits"][1])


class ContrastTests(unittest.TestCase):
    def test_contrast_ratio_known_values(self):
        self.assertAlmostEqual(
            contrast_ratio("#ffffff", "#000000"), 21.0, places=1)
        self.assertAlmostEqual(
            contrast_ratio("#000000", "#000000"), 1.0, places=2)

    def test_high_contrast_pairs_meet_seven_to_one(self):
        for name, fg_key, bg_key in _HIGH_CONTRAST_PAIRS:
            ratio = contrast_ratio(HIGH_CONTRAST[fg_key],
                                   HIGH_CONTRAST[bg_key])
            self.assertGreaterEqual(
                ratio, 7.0,
                "%s is %.2f:1, below the 7:1 bar" % (name, ratio))


class TokenCentralizationTests(unittest.TestCase):
    def test_no_hex_colour_outside_theme(self):
        # The task's own check: a hex value appears in exactly
        # one place in the package (theme.py). Every other
        # module reads tokens, never literals.
        pattern = re.compile(r"#[0-9a-fA-F]{6}\b")
        offenders = []
        for path in sorted(_PACKAGE.glob("*.py")):
            if path.name == "theme.py":
                continue
            for number, line in enumerate(
                    path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append("%s:%d: %s" % (
                        path.name, number, line.strip()))
        self.assertEqual(offenders, [])

    def test_no_fixed_point_size_in_the_package(self):
        pattern = re.compile(r"font-size|font-size: ?\d+pt|\b\d+pt\b")
        offenders = []
        for path in sorted(_PACKAGE.glob("*.py")):
            for number, line in enumerate(
                    path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append("%s:%d: %s" % (
                        path.name, number, line.strip()))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
