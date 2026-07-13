"""Centralized coordinate normalization (pipeline/coordinate_normalize.py) — the
ONE place semantic drawing anchors become global CAD coordinates.

The headline is the 158-C top-edge orientation regression: a TOP_EDGE notch must
resolve to y = parent_height - depth .. parent_height (6.25 - 1.88 = 4.37), never
y = 0 .. 1.88 (the bottom edge). Every anchor type is exercised, plus inch→meter
conversion, bounds validation, and the orientation guard.
"""
import math

import pytest

from pipeline.coordinate_normalize import (
    INCH_TO_M, Anchor, Bounds, CoordinateError, Point, anchor_from_open_edge,
    assert_edge_orientation, resolve_notch_anchor, resolve_point_anchor,
    to_meters, validate_bounds,
)

TOL = 1e-9


def _approx(a, b):
    return abs(a - b) <= TOL


# --------------------------------------------------------------------------- #
# 1-4: edge notches resolve to the correct side
# --------------------------------------------------------------------------- #
class TestEdgeNotches:
    def test_top_edge(self):
        # 158-C: plate H=6.25, notch depth=1.88, offset_x=1.56, width=1.62
        b = resolve_notch_anchor(Anchor.TOP_EDGE, offset_x=1.56, width=1.62,
                                 depth=1.88, parent_width=11.0, parent_height=6.25)
        assert _approx(b.x_min, 1.56) and _approx(b.x_max, 3.18)
        assert _approx(b.y_min, 4.37) and _approx(b.y_max, 6.25)

    def test_bottom_edge(self):
        b = resolve_notch_anchor(Anchor.BOTTOM_EDGE, offset_x=1.56, width=1.62,
                                 depth=1.88, parent_width=11.0, parent_height=6.25)
        assert _approx(b.y_min, 0.0) and _approx(b.y_max, 1.88)
        assert _approx(b.x_min, 1.56) and _approx(b.x_max, 3.18)

    def test_left_edge(self):
        b = resolve_notch_anchor(Anchor.LEFT_EDGE, offset_y=2.0, height=1.5,
                                 depth=0.75, parent_width=11.0, parent_height=6.25)
        assert _approx(b.x_min, 0.0) and _approx(b.x_max, 0.75)
        assert _approx(b.y_min, 2.0) and _approx(b.y_max, 3.5)

    def test_right_edge(self):
        b = resolve_notch_anchor(Anchor.RIGHT_EDGE, offset_y=2.0, height=1.5,
                                 depth=0.75, parent_width=11.0, parent_height=6.25)
        assert _approx(b.x_min, 10.25) and _approx(b.x_max, 11.0)
        assert _approx(b.y_min, 2.0) and _approx(b.y_max, 3.5)


# --------------------------------------------------------------------------- #
# 5-7: point anchors (holes, corners, center)
# --------------------------------------------------------------------------- #
class TestPointAnchors:
    def test_upper_right_hole(self):
        p = resolve_point_anchor(Anchor.UPPER_RIGHT, offset_x=0.25, offset_y=0.375,
                                 parent_width=11.0, parent_height=6.25)
        assert _approx(p.x, 10.75) and _approx(p.y, 5.875)

    def test_lower_right_hole(self):
        p = resolve_point_anchor(Anchor.LOWER_RIGHT, offset_x=0.25, offset_y=0.375,
                                 parent_width=11.0, parent_height=6.25)
        assert _approx(p.x, 10.75) and _approx(p.y, 0.375)

    def test_lower_left_and_upper_left(self):
        ll = resolve_point_anchor(Anchor.LOWER_LEFT, offset_x=0.25, offset_y=0.375,
                                  parent_width=11.0, parent_height=6.25)
        ul = resolve_point_anchor(Anchor.UPPER_LEFT, offset_x=0.25, offset_y=0.375,
                                  parent_width=11.0, parent_height=6.25)
        assert _approx(ll.x, 0.25) and _approx(ll.y, 0.375)
        assert _approx(ul.x, 0.25) and _approx(ul.y, 5.875)

    def test_center_relative(self):
        p = resolve_point_anchor(Anchor.CENTER, offset_x=1.0, offset_y=-0.5,
                                 parent_width=10.0, parent_height=6.0)
        assert _approx(p.x, 6.0) and _approx(p.y, 2.5)

    def test_absolute_global_passthrough(self):
        p = resolve_point_anchor(Anchor.ABSOLUTE_GLOBAL, offset_x=3.3, offset_y=2.2,
                                 parent_width=10.0, parent_height=6.0)
        assert _approx(p.x, 3.3) and _approx(p.y, 2.2)


# --------------------------------------------------------------------------- #
# 8: inch → meter conversion (exactly once, at the boundary)
# --------------------------------------------------------------------------- #
class TestUnitConversion:
    def test_inch_to_meter(self):
        assert _approx(INCH_TO_M, 0.0254)
        assert _approx(to_meters(1.0), 0.0254)
        assert _approx(to_meters(6.25), 0.15875)
        assert _approx(to_meters(0.0), 0.0)


# --------------------------------------------------------------------------- #
# 9: bounds validation (in-parent, open-edge overshoot allowed)
# --------------------------------------------------------------------------- #
class TestBoundsValidation:
    def test_valid_top_notch_with_overshoot(self):
        b = Bounds(1.56, 3.18, 4.37, 6.30)  # top overshoots 6.25 by 0.05
        v = validate_bounds(b, parent_width=11.0, parent_height=6.25,
                            overshoot_edge=Anchor.TOP_EDGE)
        assert v == [], v

    def test_out_of_parent_flagged(self):
        b = Bounds(-1.0, 3.18, 4.37, 6.25)  # x_min < 0, left is NOT the open edge
        v = validate_bounds(b, parent_width=11.0, parent_height=6.25,
                            overshoot_edge=Anchor.TOP_EDGE)
        assert any("x_min" in s for s in v)

    def test_degenerate_flagged(self):
        v = validate_bounds(Bounds(1.0, 1.0, 2.0, 2.0), parent_width=11.0, parent_height=6.25)
        assert any("degenerate" in s for s in v)

    def test_non_finite_flagged(self):
        v = validate_bounds(Bounds(0, math.inf, 0, 1), parent_width=11.0, parent_height=6.25)
        assert any("finite" in s for s in v)


# --------------------------------------------------------------------------- #
# 10: the 158-C orientation regression guard
# --------------------------------------------------------------------------- #
class TestOrientationGuard:
    def test_correct_top_notch_passes(self):
        b = resolve_notch_anchor(Anchor.TOP_EDGE, offset_x=1.56, width=1.62,
                                 depth=1.88, parent_width=11.0, parent_height=6.25)
        assert_edge_orientation(Anchor.TOP_EDGE, b, parent_height=6.25,
                                parent_width=11.0, depth=1.88)  # no raise

    def test_top_notch_at_bottom_is_rejected(self):
        # the exact bug: a TOP_EDGE notch resolved to y=0..1.88
        bad = Bounds(1.56, 3.18, 0.0, 1.88)
        with pytest.raises(CoordinateError) as ei:
            assert_edge_orientation(Anchor.TOP_EDGE, bad, parent_height=6.25,
                                    parent_width=11.0, depth=1.88)
        assert "BOTTOM" in str(ei.value) and "158-C" in str(ei.value)

    def test_bottom_notch_at_top_is_rejected(self):
        bad = Bounds(1.56, 3.18, 4.37, 6.25)  # bottom feature at the top
        with pytest.raises(CoordinateError):
            assert_edge_orientation(Anchor.BOTTOM_EDGE, bad, parent_height=6.25,
                                    parent_width=11.0, depth=1.88)


# --------------------------------------------------------------------------- #
# open_edge -> anchor mapping + error handling
# --------------------------------------------------------------------------- #
class TestMappingAndErrors:
    def test_open_edge_mapping(self):
        assert anchor_from_open_edge("top") == Anchor.TOP_EDGE
        assert anchor_from_open_edge("BOTTOM") == Anchor.BOTTOM_EDGE
        assert anchor_from_open_edge("") is None
        assert anchor_from_open_edge("diagonal") is None

    def test_edge_resolver_rejects_point_anchor(self):
        with pytest.raises(CoordinateError):
            resolve_notch_anchor(Anchor.CENTER, width=1, depth=1)

    def test_point_resolver_rejects_edge_anchor(self):
        with pytest.raises(CoordinateError):
            resolve_point_anchor(Anchor.TOP_EDGE)

    def test_unknown_anchor_string(self):
        with pytest.raises(CoordinateError):
            resolve_point_anchor("SOMEWHERE")


# --------------------------------------------------------------------------- #
# End-to-end 158-C: the generator emits the notch at the TOP, never the bottom
# --------------------------------------------------------------------------- #
class TestEndToEnd158C:
    def _plan(self, tmp_path):
        import json
        from pathlib import Path
        from pipeline.resolver import resolve_extraction
        from pipeline.validator import format_verification_report, run_verification
        from pipeline.macro_generator import generate_macro_package

        fix = Path(__file__).resolve().parent / "fixtures" / "commit_mode" / "158-C_extraction.json"
        data = json.loads(fix.read_text())
        res = resolve_extraction(data)
        model, rep = run_verification(res.clean_extraction)
        pkg = generate_macro_package(model, data, format_verification_report(model, rep),
                                     tmp_path, resolution=res)
        return json.loads(pkg.build_plan_json.read_text()), pkg

    def test_f002_notch_resolves_to_top_edge(self, tmp_path):
        plan, _pkg = self._plan(tmp_path)
        rect = next(s for s in plan["steps"] if s["type"] == "slot_rect_cut")
        ys = [c[1] for c in rect["sketch"]["corners_drawing_units"]]
        xs = [c[0] for c in rect["sketch"]["corners_drawing_units"]]
        # closed bottom edge at 6.25 - 1.88 = 4.37; NEVER the bottom (0..1.88)
        assert min(ys) == pytest.approx(4.37)
        assert min(xs) == pytest.approx(1.56) and max(xs) == pytest.approx(3.18)
        assert not (min(ys) == pytest.approx(0.0) and max(ys) == pytest.approx(1.88))

    def test_generator_refuses_a_bottom_placed_top_notch(self, tmp_path):
        # Directly exercise the generation-time orientation guard.
        from pipeline import macro_generator as mg
        from pipeline.schema import DrawingData

        model = DrawingData.model_validate({
            "part_number": "T", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D1", "type": "linear", "value": 11.0, "unit": "inch", "applies_to": "length"},
                {"id": "D2", "type": "linear", "value": 6.25, "unit": "inch", "applies_to": "width"},
            ],
            "features": [{"id": "F001", "type": "extrude_boss", "description": "plate",
                          "related_dimensions": ["D1", "D2"]}],
        })

        class _Step:
            feature_type = "slot_rect_cut"
            feature_id = "F002"
            dimensions = {"depth": 1.88}
            slot = {"open_edge": "top",
                    "corners_drawing_units": [[1.56, 0.0], [3.18, 0.0],
                                              [3.18, 1.88], [1.56, 1.88]]}  # BOTTOM placement

        class _Pkg:
            steps = [_Step()]

        with pytest.raises(mg.MacroGenerationError) as ei:
            mg._assert_notch_orientation(model, _Pkg())
        assert "ORIENTATION" in str(ei.value).upper()
