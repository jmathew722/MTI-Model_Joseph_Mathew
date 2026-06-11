"""Tests for utils.unit_converter — the most safety-critical module."""
import math

import pytest

from utils.unit_converter import assert_meters, to_meters, to_radians


class TestToMeters:
    def test_mm_to_meters(self):
        assert to_meters(25.4, "mm") == pytest.approx(0.0254)

    def test_inch_to_meters(self):
        assert to_meters(1.0, "inch") == pytest.approx(0.0254)
        assert to_meters(1.0, "in") == pytest.approx(0.0254)

    def test_cm_to_meters(self):
        assert to_meters(10.0, "cm") == pytest.approx(0.1)

    def test_meters_identity(self):
        assert to_meters(2.0, "m") == pytest.approx(2.0)

    def test_feet_to_meters(self):
        assert to_meters(1.0, "ft") == pytest.approx(0.3048)

    def test_case_and_whitespace_insensitive(self):
        assert to_meters(1.0, "  MM ") == pytest.approx(0.001)
        assert to_meters(1.0, "Inches") == pytest.approx(0.0254)

    def test_zero_is_allowed(self):
        # Zero length is degenerate but not an error at the conversion layer.
        assert to_meters(0.0, "mm") == 0.0

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            to_meters(-5.0, "mm")

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError):
            to_meters(1.0, "furlong")

    def test_non_finite_raises(self):
        with pytest.raises(ValueError):
            to_meters(float("inf"), "mm")
        with pytest.raises(ValueError):
            to_meters(float("nan"), "mm")

    def test_non_number_raises(self):
        with pytest.raises(TypeError):
            to_meters("25.4", "mm")  # string value, not a number

    def test_bool_rejected(self):
        with pytest.raises(TypeError):
            to_meters(True, "mm")


class TestToRadians:
    def test_180_degrees(self):
        assert to_radians(180.0) == pytest.approx(math.pi)

    def test_90_degrees(self):
        assert to_radians(90.0) == pytest.approx(math.pi / 2)

    def test_zero(self):
        assert to_radians(0.0) == 0.0

    def test_non_finite_raises(self):
        with pytest.raises(ValueError):
            to_radians(float("nan"))


class TestAssertMeters:
    def test_passes_plausible_value(self):
        assert assert_meters(0.0254, "x") == pytest.approx(0.0254)

    def test_rejects_unconverted_mm_value(self):
        # 100 (mm meant as meters) is implausibly large → caught.
        with pytest.raises(ValueError):
            assert_meters(100.0, "length")

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            assert_meters(-0.01, "x")
