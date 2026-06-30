"""Tests for the Option 2 & 3 pipeline additions:

  * hole coordinate-frame inference (center-referenced -> corner frame)
  * circular / bolt-circle hole patterns (positions on the ring)
  * real revolves (profile -> closed sketch + revolve macro)
  * mirror features (macro + validation)
  * feature-scoped fillet/chamfer edge selection strategy

The COM (.sldprt) calls themselves need a live SolidWorks, so these cover the
pure cores and the generated-macro text — which is where correctness is decided.
"""
import math
import tempfile
from pathlib import Path

import pytest

from pipeline.macro_generator import (
    _circular_positions,
    _corner_frame_shift,
    _hole_positions,
    generate_macro_package,
    revolve_sketch_points,
)
from pipeline.schema import DrawingData, Feature, FeatureType, HoleCallout, PatternKind
from pipeline.solidworks_builder import _fillet_edge_strategy
from pipeline.validator import format_verification_report, run_verification


# --------------------------------------------------------------------------- #
# 2b — hole coordinate-frame inference
# --------------------------------------------------------------------------- #
def _plate(length=10.0, width=6.0) -> DrawingData:
    return DrawingData(
        units="inch", confidence=0.9,
        dimensions=[
            {"id": "D1", "type": "linear", "value": length, "unit": "inch", "applies_to": "length"},
            {"id": "D2", "type": "linear", "value": width, "unit": "inch", "applies_to": "width"},
        ],
    )


class TestCornerFrameInference:
    def test_negative_positions_shift_to_corner(self):
        m = _plate(10, 6)
        # Center-referenced (origin at part center): shift by (+5, +3).
        out = _corner_frame_shift(m, [(-2.0, -1.0), (2.0, 1.0)])
        assert out == [(3.0, 2.0), (7.0, 4.0)]

    def test_corner_positions_unchanged(self):
        m = _plate(10, 6)
        pts = [(1.0, 1.0), (9.0, 5.0)]
        assert _corner_frame_shift(m, pts) == pts

    def test_no_envelope_no_shift(self):
        m = DrawingData(units="inch", confidence=0.9)  # no length/width
        pts = [(-2.0, -1.0)]
        assert _corner_frame_shift(m, pts) == pts


# --------------------------------------------------------------------------- #
# 3b — circular / bolt-circle patterns
# --------------------------------------------------------------------------- #
class TestCircularPattern:
    def test_four_holes_on_bolt_circle(self):
        m = _plate(40, 40)
        h = HoleCallout(id="H1", type="thru", diameter=0.25, qty=4,
                        pattern=PatternKind.CIRCULAR, bolt_circle_diameter=10.0,
                        bolt_circle_center=[20.0, 20.0])
        pts = _circular_positions(m, h)
        assert pts is not None and len(pts) == 4
        # Centers lie on the bolt circle (radius 5) about (20, 20).
        for x, y in pts:
            assert math.isclose(math.hypot(x - 20, y - 20), 5.0, abs_tol=1e-6)

    def test_start_angle_first_instance(self):
        m = _plate(40, 40)
        h = HoleCallout(id="H1", type="thru", diameter=0.25, qty=3,
                        pattern=PatternKind.CIRCULAR, bolt_circle_diameter=10.0,
                        bolt_circle_center=[0.0, 0.0], start_angle=90.0)
        pts = _circular_positions(m, h)
        assert math.isclose(pts[0][0], 0.0, abs_tol=1e-6)
        assert math.isclose(pts[0][1], 5.0, abs_tol=1e-6)  # straight up

    def test_non_circular_returns_none(self):
        m = _plate()
        h = HoleCallout(id="H1", type="thru", diameter=0.25, qty=4, pattern=PatternKind.LINEAR)
        assert _circular_positions(m, h) is None

    def test_hole_positions_uses_circular_layout(self):
        m = _plate(40, 40)
        h = HoleCallout(id="H1", type="thru", diameter=0.25, qty=6,
                        pattern=PatternKind.CIRCULAR, bolt_circle_diameter=10.0)
        assert len(_hole_positions(m, h)) == 6

    def test_no_center_no_envelope_stays_non_negative(self):
        # Round part: no bolt_circle_center AND no length/width envelope. Positions
        # must remain non-negative (corner frame) so to_meters() never rejects them.
        m = DrawingData(units="inch", confidence=0.9)  # no envelope dims
        h = HoleCallout(id="H1", type="thru", diameter=0.25, qty=4,
                        pattern=PatternKind.CIRCULAR, bolt_circle_diameter=4.375)
        pts = _circular_positions(m, h)
        assert pts is not None and len(pts) == 4
        assert all(x >= -1e-9 and y >= -1e-9 for x, y in pts), pts


# --------------------------------------------------------------------------- #
# 3a — revolve profile transform
# --------------------------------------------------------------------------- #
class TestRevolveProfile:
    def test_closes_back_to_axis(self):
        closed, (xmn, xmx) = revolve_sketch_points([[0, 5], [10, 5], [10, 8], [25, 8]])
        assert (xmn, xmx) == (0.0, 25.0)
        # Two points were added to drop to the axis and return along it.
        assert (25.0, 0.0) in closed and (0.0, 0.0) in closed
        assert closed[0] == (0.0, 5.0)

    def test_profile_already_on_axis_not_duplicated(self):
        # Endpoints already at radial 0 -> no extra axis points added.
        closed, _ = revolve_sketch_points([[0, 0], [0, 5], [10, 5], [10, 0]])
        assert closed == [(0.0, 0.0), (0.0, 5.0), (10.0, 5.0), (10.0, 0.0)]

    def test_too_few_points_raises(self):
        from pipeline.macro_generator import MacroGenerationError

        with pytest.raises(MacroGenerationError):
            revolve_sketch_points([[0, 5]])


# --------------------------------------------------------------------------- #
# Macro generation for the new feature types (also exercises the static audit,
# which raises during generation on bad VBA).
# --------------------------------------------------------------------------- #
def _gen(data: dict, tmp_path) -> Path:
    model, report = run_verification(data)
    assert report.ok, str(report)
    pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
    return pkg.macros_dir


class TestRevolveMacro:
    def test_real_revolve_macro(self, tmp_path):
        data = {
            "part_number": "SHAFT-1", "units": "inch", "confidence": 0.9,
            "dimensions": [{"id": "D1", "type": "diameter", "value": 1.0, "unit": "inch", "applies_to": "diameter"}],
            "features": [{"id": "F1", "type": "revolve", "description": "Turned shaft",
                          "sketch_plane": "front",
                          "revolve_profile": [[0, 0.5], [2, 0.5], [2, 0.75], [4, 0.75]]}],
            "build_order": ["F1"],
        }
        macros = _gen(data, tmp_path)
        rev = next(macros.glob("*F1*")).read_text()
        assert "FeatureRevolve2" in rev
        assert "CreateCenterLine" in rev
        assert rev.count("CreateLine ") >= 4  # closed profile segments

    def test_revolve_without_profile_falls_back_to_skeleton(self, tmp_path):
        data = {
            "part_number": "SHAFT-2", "units": "inch", "confidence": 0.9,
            "dimensions": [{"id": "D1", "type": "diameter", "value": 1.0, "unit": "inch", "applies_to": "diameter"},
                           {"id": "D2", "type": "depth", "value": 2.0, "unit": "inch", "applies_to": "depth"}],
            "features": [{"id": "F1", "type": "revolve", "description": "Hub",
                          "related_dimensions": ["D1", "D2"]}],
            "build_order": ["F1"],
        }
        macros = _gen(data, tmp_path)
        rev = next(macros.glob("*F1*")).read_text()
        assert "TODO: VERIFY API CALL" in rev  # manual skeleton


class TestMirrorMacro:
    def _mirror_data(self) -> dict:
        return {
            "part_number": "MIR-1", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D1", "type": "linear", "value": 4, "unit": "inch", "applies_to": "length"},
                {"id": "D2", "type": "linear", "value": 2, "unit": "inch", "applies_to": "width"},
                {"id": "D3", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"},
                {"id": "D4", "type": "diameter", "value": 0.25, "unit": "inch",
                 "applies_to": "hole_diameter", "feature_ref": "F2"}],
            "hole_callouts": [{"id": "H1", "type": "thru", "diameter": 0.25, "qty": 1,
                               "feature_ref": "F2", "position_known": True, "x_position": 1, "y_position": 1}],
            "features": [
                {"id": "F1", "type": "extrude_boss", "description": "Base",
                 "related_dimensions": ["D1", "D2"], "depth_dimension_id": "D3", "sketch_plane": "top"},
                {"id": "F2", "type": "hole", "description": "Hole", "related_dimensions": ["D4"]},
                {"id": "F3", "type": "mirror", "description": "Mirror hole",
                 "parent_feature": "F2", "mirror_plane": "right"}],
            "build_order": ["F1", "F2", "F3"],
        }

    def test_mirror_macro_emits_insert_mirror(self, tmp_path):
        macros = _gen(self._mirror_data(), tmp_path)
        mir = next(macros.glob("*F3*")).read_text()
        assert "InsertMirrorFeature2" in mir
        assert "F2_" in mir  # references the seed feature by name


class TestCircularHoleMacro:
    def test_bolt_circle_emits_individual_circles(self, tmp_path):
        data = {
            "part_number": "BC-1", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D1", "type": "diameter", "value": 6, "unit": "inch", "applies_to": "diameter"},
                {"id": "D3", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "thickness"},
                {"id": "D4", "type": "diameter", "value": 0.25, "unit": "inch",
                 "applies_to": "hole_diameter", "feature_ref": "F2"}],
            "hole_callouts": [{"id": "H1", "type": "thru", "diameter": 0.25, "qty": 6,
                               "pattern": "circular", "bolt_circle_diameter": 4,
                               "bolt_circle_center": [3, 3], "feature_ref": "F2"}],
            "features": [
                {"id": "F1", "type": "extrude_boss", "description": "Disc",
                 "related_dimensions": ["D1"], "depth_dimension_id": "D3", "sketch_plane": "front"},
                {"id": "F2", "type": "hole", "description": "Bolt holes", "related_dimensions": ["D4"]}],
            "build_order": ["F1", "F2"],
        }
        macros = _gen(data, tmp_path)
        hole = next(macros.glob("*F2*")).read_text()
        assert hole.count("CreateCircleByRadius") == 6


# --------------------------------------------------------------------------- #
# Validation of the new feature types
# --------------------------------------------------------------------------- #
class TestValidation:
    def test_revolve_with_profile_passes_definability(self):
        data = {
            "part_number": "R", "units": "inch", "confidence": 0.9,
            "dimensions": [{"id": "D1", "type": "diameter", "value": 1.0, "unit": "inch", "applies_to": "diameter"}],
            "features": [{"id": "F1", "type": "revolve", "description": "shaft",
                          "revolve_profile": [[0, 0.5], [3, 0.5]]}],  # no related dims
            "build_order": ["F1"],
        }
        _model, report = run_verification(data)
        assert report.ok, str(report)

    def test_mirror_without_parent_warns_not_blocks(self):
        data = {
            "part_number": "M", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D1", "type": "linear", "value": 4, "unit": "inch", "applies_to": "length"},
                {"id": "D2", "type": "linear", "value": 2, "unit": "inch", "applies_to": "width"},
                {"id": "D3", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"}],
            "features": [
                {"id": "F1", "type": "extrude_boss", "description": "Base",
                 "related_dimensions": ["D1", "D2"], "depth_dimension_id": "D3"},
                {"id": "F2", "type": "mirror", "description": "bad mirror"}],  # no parent_feature
            "build_order": ["F1", "F2"],
        }
        _model, report = run_verification(data)
        assert report.ok  # advisory only
        assert any("Mirror feature F2" in w for w in report.warnings)


# --------------------------------------------------------------------------- #
# 2a — fillet edge-selection strategy (pure decision)
# --------------------------------------------------------------------------- #
class TestFilletEdgeStrategy:
    def test_scopes_to_parent_when_built(self):
        f = Feature(id="F9", type=FeatureType.FILLET, description="x", parent_feature="F1")
        assert _fillet_edge_strategy(f, {"F1": object()}) == ("feature", "F1")

    def test_all_edges_when_parent_absent(self):
        f = Feature(id="F9", type=FeatureType.FILLET, description="x", parent_feature="F1")
        assert _fillet_edge_strategy(f, {}) == ("all", "")

    def test_no_parent_uses_all_edges(self):
        f = Feature(id="F9", type=FeatureType.FILLET, description="x")
        assert _fillet_edge_strategy(f, {"F1": object()}) == ("all", "")

    def test_env_override_forces_all(self, monkeypatch):
        monkeypatch.setenv("FILLET_EDGE_MODE", "all")
        f = Feature(id="F9", type=FeatureType.FILLET, description="x", parent_feature="F1")
        assert _fillet_edge_strategy(f, {"F1": object()}) == ("all", "")
