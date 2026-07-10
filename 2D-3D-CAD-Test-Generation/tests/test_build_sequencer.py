"""Tests for the canonical seven-stage build sequencer + the CadQuery origin
transform (2026-07-10 redesign).

Covers the acceptance criteria that can be verified without a CAD runtime:
  * stage classification (base/additive split, cut vs hole, pattern, edges, thread),
  * deterministic (byte-identical) build order across repeated runs,
  * dependency ordering (pattern after its seed, fillet/chamfer after all cuts),
  * the three-state disposition table (BUILT / BUILT_WITH_DERIVED_VALUE /
    EXCLUDED_INCOMPLETE), and
  * the single-place origin -> workplane-local transform.
"""
from types import SimpleNamespace

import pytest

from pipeline.build_sequencer import (
    STAGE_ADDITIVE,
    STAGE_BASE,
    STAGE_EDGE,
    STAGE_HOLE,
    STAGE_NONGEOMETRIC,
    STAGE_PATTERN,
    STAGE_PROFILE_CUT,
    STATE_BUILT,
    STATE_BUILT_DERIVED,
    STATE_EXCLUDED,
    classify_stage,
    disposition_state,
    sequence_build_order,
)
from pipeline.cq_prevalidate import to_workplane_local
from pipeline.validator import run_verification


def multi_stage_drawing(units="inch") -> dict:
    """A part exercising every stage: two bases (small + large), a profile cut,
    a plain hole, a tapped hole, a pattern on the plain hole, a chamfer, a
    fillet, and a bare cosmetic thread."""
    return {
        "part_number": "SEQ-TEST",
        "units": units,
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": units, "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 2.0, "unit": units, "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 0.5, "unit": units, "applies_to": "height"},
            {"id": "D004", "type": "linear", "value": 1.0, "unit": units, "applies_to": "length"},
            {"id": "D005", "type": "linear", "value": 1.0, "unit": units, "applies_to": "width"},
            {"id": "D006", "type": "linear", "value": 0.4, "unit": units, "applies_to": "length"},
            {"id": "D007", "type": "linear", "value": 0.4, "unit": units, "applies_to": "width"},
            {"id": "D008", "type": "diameter", "value": 0.25, "unit": units,
             "applies_to": "hole_diameter", "feature_ref": "F004"},
            {"id": "D009", "type": "diameter", "value": 0.19, "unit": units,
             "applies_to": "hole_diameter", "feature_ref": "F005"},
            {"id": "D010", "type": "radial", "value": 0.125, "unit": units,
             "applies_to": "fillet_radius", "feature_ref": "F008"},
            {"id": "D011", "type": "linear", "value": 0.06, "unit": units,
             "applies_to": "chamfer", "feature_ref": "F007"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "thru", "diameter": 0.25, "qty": 1, "thru": True,
             "x_position": 3.0, "y_position": 1.0, "position_known": True, "feature_ref": "F004"},
            {"id": "H002", "type": "tapped", "diameter": 0.19, "qty": 1, "thru": True,
             "thread_spec": "10-24 UNC", "x_position": 1.0, "y_position": 1.0,
             "position_known": True, "feature_ref": "F005"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Small boss",
             "related_dimensions": ["D004", "D005"], "depth_dimension_id": "D003"},
            {"id": "F002", "type": "extrude_boss", "description": "Large base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
            {"id": "F003", "type": "extrude_cut", "description": "Notch",
             "related_dimensions": ["D006", "D007"], "offset_x": 0.1, "offset_y": 0.1},
            {"id": "F004", "type": "hole", "description": "Plain thru hole",
             "related_dimensions": ["D008"]},
            {"id": "F005", "type": "thread", "description": "Tapped hole",
             "related_dimensions": ["D009"]},
            {"id": "F006", "type": "pattern", "description": "Hole pattern",
             "parent_feature": "F004", "quantity": 4},
            {"id": "F007", "type": "chamfer", "description": "Edge chamfer",
             "related_dimensions": ["D011"]},
            {"id": "F008", "type": "fillet", "description": "Corner fillet",
             "related_dimensions": ["D010"]},
            {"id": "F009", "type": "thread", "description": "Cosmetic thread note"},
        ],
        "build_order": ["F001", "F002", "F003", "F004", "F005", "F006", "F007", "F008", "F009"],
    }


@pytest.fixture
def model():
    m, _ = run_verification(multi_stage_drawing())
    assert m is not None
    return m


class TestClassification:
    def test_stages(self, model):
        by_id = {f.id: classify_stage(model, f) for f in model.features}
        # Both extrude_boss classify as BASE at the type level; the largest is
        # promoted to Stage 1 and the other demoted in sequence_build_order.
        assert by_id["F001"] == STAGE_BASE
        assert by_id["F002"] == STAGE_BASE
        assert by_id["F003"] == STAGE_PROFILE_CUT
        assert by_id["F004"] == STAGE_HOLE
        assert by_id["F005"] == STAGE_HOLE          # tapped hole (has callout)
        assert by_id["F006"] == STAGE_PATTERN
        assert by_id["F007"] == STAGE_EDGE
        assert by_id["F008"] == STAGE_EDGE
        assert by_id["F009"] == STAGE_NONGEOMETRIC  # bare cosmetic thread

    def test_largest_base_wins(self, model):
        res = sequence_build_order(model)
        # F002 (4x2) is larger than F001 (1x1) -> Stage 1 base, first in order.
        assert res.build_order[0] == "F002"
        disp = {d.feature_id: d for d in res.dispositions}
        assert disp["F002"].stage == STAGE_BASE
        assert disp["F001"].stage == STAGE_ADDITIVE


class TestOrdering:
    def test_full_stage_order(self, model):
        order = sequence_build_order(model).build_order
        assert order == ["F002", "F001", "F003", "F004", "F005", "F006", "F007", "F008", "F009"]

    def test_pattern_after_seed(self, model):
        order = sequence_build_order(model).build_order
        assert order.index("F006") > order.index("F004")

    def test_edges_after_all_cuts_and_holes(self, model):
        order = sequence_build_order(model).build_order
        last_cut = max(order.index(f) for f in ("F003", "F004", "F005"))
        assert order.index("F007") > last_cut  # chamfer after cuts/holes
        assert order.index("F008") > last_cut  # fillet after cuts/holes

    def test_chamfer_before_fillet(self, model):
        order = sequence_build_order(model).build_order
        assert order.index("F007") < order.index("F008")

    def test_plain_hole_before_tapped(self, model):
        order = sequence_build_order(model).build_order
        assert order.index("F004") < order.index("F005")

    def test_deterministic_byte_identical(self, model):
        a = sequence_build_order(model).build_order
        b = sequence_build_order(model).build_order
        assert a == b
        # A fresh model from the same dict must produce the same order.
        m2, _ = run_verification(multi_stage_drawing())
        assert sequence_build_order(m2).build_order == a


class TestDispositionTable:
    def test_all_features_present(self, model):
        res = sequence_build_order(model)
        assert {d.feature_id for d in res.dispositions} == {f.id for f in model.features}

    def test_built_state_default(self, model):
        res = sequence_build_order(model)  # no resolution -> everything in order is BUILT
        disp = {d.feature_id: d for d in res.dispositions}
        assert disp["F002"].state == STATE_BUILT
        assert disp["F004"].state == STATE_BUILT

    def test_excluded_when_not_in_build_order(self):
        data = multi_stage_drawing()
        data["build_order"] = [f for f in data["build_order"] if f != "F004"]
        m, _ = run_verification(data)
        res = sequence_build_order(m)
        disp = {d.feature_id: d for d in res.dispositions}
        assert disp["F004"].state == STATE_EXCLUDED
        assert "F004" not in res.build_order

    def test_derived_value_state(self, model):
        # A stub resolution marking F003's length as constraint-graph derived.
        dr = SimpleNamespace(assumption_made=True, assumption_basis="constraint_graph")
        resolution = SimpleNamespace(
            dim_resolutions={"D006": dr}, feature_resolutions={}, flags=[])
        f003 = model.feature_by_id("F003")
        assert disposition_state(f003, resolution, in_build_order=True) == STATE_BUILT_DERIVED
        # An explicitly-read value stays BUILT.
        dr_read = SimpleNamespace(assumption_made=True, assumption_basis="explicit_callout")
        resolution2 = SimpleNamespace(
            dim_resolutions={"D006": dr_read}, feature_resolutions={}, flags=[])
        assert disposition_state(f003, resolution2, in_build_order=True) == STATE_BUILT

    def test_disposition_json_serializable(self, model):
        import json
        res = sequence_build_order(model)
        json.dumps(res.disposition_table)  # must not raise
        assert res.human_lines  # backward-compat free-text lines exist


class TestHardFailure:
    def test_no_base_is_hard_failure(self):
        data = multi_stage_drawing()
        # Drop both bases from the build order -> no closed outer profile.
        data["build_order"] = ["F003", "F004", "F005"]
        m, _ = run_verification(data)
        res = sequence_build_order(m)
        assert res.hard_failures
        assert "base" in res.hard_failures[0].lower()


class TestOriginTransform:
    def test_scale_inch_to_mm(self):
        assert to_workplane_local(1.0, 2.0, 25.4) == pytest.approx((25.4, 50.8))

    def test_mm_identity(self):
        assert to_workplane_local(3.0, 4.0, 1.0) == pytest.approx((3.0, 4.0))

    def test_origin_maps_to_origin(self):
        # The part's lower-left datum maps to the workplane-local origin.
        assert to_workplane_local(0.0, 0.0, 25.4) == pytest.approx((0.0, 0.0))
