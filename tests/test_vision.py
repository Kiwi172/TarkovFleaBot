"""Vision tests using synthetic images — no screen or game required."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from flea_bot.vision.ocr import parse_number, parse_quantity, preprocess
from flea_bot.vision.template import Match, match_template, match_template_all


def canvas(w=400, h=300, value=30) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def stamp(image: np.ndarray, patch: np.ndarray, x: int, y: int) -> np.ndarray:
    out = image.copy()
    out[y : y + patch.shape[0], x : x + patch.shape[1]] = patch
    return out


def marker(size=40) -> np.ndarray:
    """A distinctive patch — flat colour would match everywhere."""
    p = np.zeros((size, size, 3), dtype=np.uint8)
    p[:, :] = (200, 120, 60)
    cv2.rectangle(p, (5, 5), (size - 6, size - 6), (20, 220, 250), 2)
    cv2.line(p, (0, 0), (size, size), (255, 255, 255), 2)
    return p


class TestTemplateMatching:
    def test_finds_template_at_known_position(self):
        needle = marker()
        haystack = stamp(canvas(), needle, 120, 80)
        match = match_template(haystack, needle, threshold=0.9)
        assert match is not None
        assert (match.x, match.y) == (120, 80)
        assert match.confidence > 0.99

    def test_center_is_middle_of_box(self):
        needle = marker(40)
        haystack = stamp(canvas(), needle, 100, 50)
        match = match_template(haystack, needle, threshold=0.9)
        assert match.center == (120, 70)

    def test_offset_converts_to_absolute_coords(self):
        needle = marker()
        haystack = stamp(canvas(), needle, 10, 20)
        match = match_template(haystack, needle, threshold=0.9, offset=(500, 300))
        assert (match.x, match.y) == (510, 320)

    def test_returns_none_when_absent(self):
        haystack = canvas()
        assert match_template(haystack, marker(), threshold=0.9) is None

    def test_threshold_gates_the_result(self):
        needle = marker()
        # Degrade the on-screen copy so it is a near, not exact, match.
        noisy = cv2.GaussianBlur(needle, (7, 7), 3)
        haystack = stamp(canvas(), noisy, 60, 60)

        assert match_template(haystack, needle, threshold=0.99) is None
        assert match_template(haystack, needle, threshold=0.5) is not None

    def test_oversized_template_returns_none(self):
        big = np.zeros((500, 500, 3), dtype=np.uint8)
        assert match_template(canvas(100, 100), big, threshold=0.5) is None

    def test_find_all_locates_repeated_rows(self):
        needle = marker(30)
        haystack = canvas(400, 400)
        for y in (20, 100, 180, 260):
            haystack = stamp(haystack, needle, 50, y)

        matches = match_template_all(haystack, needle, threshold=0.9)
        assert len(matches) == 4
        assert [m.y for m in matches] == [20, 100, 180, 260], "must be in reading order"

    def test_find_all_suppresses_overlaps(self):
        needle = marker(30)
        haystack = stamp(canvas(200, 200), needle, 50, 50)
        # Without NMS a single stamp produces many adjacent above-threshold hits.
        assert len(match_template_all(haystack, needle, threshold=0.8)) == 1


class TestOCRPreprocessing:
    def test_upscales_and_pads(self, config):
        config.ocr.upscale = 3
        out = preprocess(canvas(50, 20), config)
        # 3x upscale plus 10px border on each side.
        assert out.shape == (20 * 3 + 20, 50 * 3 + 20)

    def test_output_is_binary_single_channel(self, config):
        img = canvas(60, 30, value=40)
        cv2.putText(img, "1234", (2, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        out = preprocess(img, config)
        assert out.ndim == 2
        assert set(np.unique(out)).issubset({0, 255})

    def test_invert_flips_polarity(self, config):
        light_on_dark = canvas(40, 20, value=10)
        config.ocr.invert = True
        inverted = preprocess(light_on_dark, config)
        config.ocr.invert = False
        plain = preprocess(light_on_dark, config)
        assert inverted.mean() != plain.mean()

    def test_accepts_grayscale_input(self, config):
        gray = np.full((20, 40), 30, dtype=np.uint8)
        assert preprocess(gray, config).ndim == 2


class TestNumberParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("12345", 12345),
            ("12 345", 12345),           # Tarkov uses space separators
            ("12,345", 12345),
            ("12.345", 12345),
            ("₽ 45 000", 45000),
            ("45 000 ₽", 45000),
            ("Price: 8 500", 8500),
            ("0", 0),
        ],
    )
    def test_parses_numeric_forms(self, text, expected):
        assert parse_number(text) == expected

    @pytest.mark.parametrize("text", ["", "  ", "abc", "₽", "---"])
    def test_returns_none_without_digits(self, text):
        assert parse_number(text) is None

    @pytest.mark.parametrize(
        "text,expected",
        [("x5", 5), ("5x", 5), ("12 pcs", 12), ("3 pc", 3), ("7", 7), ("", 1), ("abc", 1)],
    )
    def test_quantity_forms(self, text, expected):
        assert parse_quantity(text) == expected


class TestMatchDataclass:
    def test_region_round_trips(self):
        m = Match(10, 20, 30, 40, 0.99)
        assert m.region == (10, 20, 30, 40)
        assert m.center == (25, 40)
