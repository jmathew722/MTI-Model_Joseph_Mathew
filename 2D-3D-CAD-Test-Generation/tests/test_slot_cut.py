"""Tests for the canonical slot / U-notch decomposition rule (2026-07-11).

A U-shaped cutout / open notch / slot is NEVER a single arc sketch — it is two
ordered features: a MANDATORY rectangle cut (must_complete, carries position +
size) immediately followed by DEFERRABLE corner fillets. Worked example: drawing
A001581E — a notch 1.56 from the left edge, 1.62 wide x 1.88 deep from the top
edge, through-all, then R.25 fillets on the two interior bottom corners.
"""
import json
from pathlib import Path

import pytest

from pipeline.macro_audit import audit_package
from pipeline.macro_generator import generate_macro_package
from pipeline.reconciliation import build_checklist, slot_checks
from pipeline.resolver import resolve_extraction
from pipeline.slot_cut import (
    corner_array,
    expected_corner_count,
    interior_corners,
    normalize_legacy_slots,
    validate_slot,
)
from pipeline.schema import DrawingData
from pipeline.validator import format_verification_report, run_verification


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _a001581e(width=1.62, depth=1.88, radius=0.25, anchor=1.56,
              slot_kind="open_notch", open_edge="top",
              anchor_semantics="edge_to_near_edge"):
    """The A001581E golden slot part: a 11.0 x 6.25 x .105 plate with one
    top-edge U-notch."""
    return {
        "part_number": "A001581E", "units": "inch", "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 11.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 6.25, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 0.105, "unit": "inch", "applies_to": "height"},
            {"id": "D004", "type": "linear", "value": anchor, "unit": "inch", "applies_to": "position"},
            {"id": "D005", "type": "linear", "value": width, "unit": "inch", "applies_to": "width"},
            {"id": "D006", "type": "linear", "value": depth, "unit": "inch", "applies_to": "depth"},
            {"id": "D012", "type": "radial", "value": radius, "unit": "inch", "applies_to": "fillet_radius"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
            {"id": "F002", "type": "extrude_cut", "description": "U-notch cutout",
             "related_dimensions": []},
        ],
        "slot_cuts": [
            {"id": "F002", "slot_kind": slot_kind, "open_edge": open_edge,
             "anchor_edge": "left", "anchor_offset": anchor, "anchor_dimension_id": "D004",
             "anchor_semantics": anchor_semantics,
             "width": width, "width_dimension_id": "D005",
             "depth": depth, "depth_dimension_id": "D006",
             "corner_radius": radius, "corner_radius_dimension_id": "D012",
             "thru": True, "thru_basis": "single_view_default"},
        ],
        "build_order": ["F001", "F002"],
    }


def _build_pkg(raw, tmp_path):
    res = resolve_extraction(raw)
    model, rep = run_verification(res.clean_extraction)
    pkg = generate_macro_package(model, raw, format_verification_report(model, rep),
                                 tmp_path, resolution=res)
    plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
    return res, model, pkg, plan


# --------------------------------------------------------------------------- #
# Golden: A001581E emits exactly two linked ordered steps
# --------------------------------------------------------------------------- #
class TestGoldenA001581E:
    def test_two_linked_steps_correct_order(self, tmp_path):
        _, _, _, plan = _build_pkg(_a001581e(), tmp_path)
        steps = plan["steps"]
        rect_i = next(i for i, s in enumerate(steps) if s["type"] == "slot_rect_cut")
        fil_i = next(i for i, s in enumerate(steps) if s["type"] == "slot_corner_fillet")
        # fillet ALWAYS immediately follows its rectangle — nothing between them
        assert fil_i == rect_i + 1
        assert steps[rect_i]["feature_id"] == "F002"
        assert steps[fil_i]["feature_id"].startswith("F002")

    def test_rectangle_is_must_complete(self, tmp_path):
        _, _, _, plan = _build_pkg(_a001581e(), tmp_path)
        rect = next(s for s in plan["steps"] if s["type"] == "slot_rect_cut")
        assert rect["must_complete"] is True

    def test_fillet_is_deferrable(self, tmp_path):
        _, _, _, plan = _build_pkg(_a001581e(), tmp_path)
        fil = next(s for s in plan["steps"] if s["type"] == "slot_corner_fillet")
        assert fil["defer_on_failure"] is True
        assert fil["corner_count_expected"] == 2

    def test_exact_corner_array(self, tmp_path):
        _, _, _, plan = _build_pkg(_a001581e(), tmp_path)
        rect = next(s for s in plan["steps"] if s["type"] == "slot_rect_cut")
        corners = rect["sketch"]["corners_drawing_units"]
        assert corners == [[1.56, 4.37], [3.18, 4.37], [3.18, 6.25], [1.56, 6.25]]

    def test_fillet_targets_two_interior_bottom_corners(self, tmp_path):
        _, _, _, plan = _build_pkg(_a001581e(), tmp_path)
        fil = next(s for s in plan["steps"] if s["type"] == "slot_corner_fillet")
        assert fil["positions_xy"] == [[1.56, 4.37], [3.18, 4.37]]

    def test_macros_pass_audit(self, tmp_path):
        _, _, pkg, _ = _build_pkg(_a001581e(), tmp_path)
        names = [p.name for p in sorted(pkg.macros_dir.glob("*slot*.vba"))]
        assert any("slot_rect_cut" in n for n in names)
        assert any("slot_corner_fillet" in n for n in names)
        ar = audit_package(pkg.macros_dir)
        assert ar.ok, [e.message for e in ar.errors]


# --------------------------------------------------------------------------- #
# Resolver validation: violations go to the clarification gate, NEVER clamped
# --------------------------------------------------------------------------- #
class TestSlotValidation:
    def test_radius_violation_gates_not_clamped(self):
        raw = _a001581e(radius=0.9)  # 2R = 1.8 > width 1.62
        res = resolve_extraction(raw)
        # radius is preserved unchanged (never silently clamped)
        assert res.clean_extraction["slot_cuts"][0]["corner_radius"] == 0.9
        crit = [f for f in res.flags
                if f.get("source") == "slot_cut" and f.get("flag_tier") == "CRITICAL"]
        assert crit and any("gate_question" in f for f in crit)

    def test_misfit_gates(self):
        raw = _a001581e(anchor=10.5, width=1.62)  # 10.5 + 1.62 > 11.0
        res = resolve_extraction(raw)
        crit = [f for f in res.flags
                if f.get("source") == "slot_cut" and f.get("flag_tier") == "CRITICAL"]
        assert crit

    def test_clean_slot_single_view_flag(self):
        res = resolve_extraction(_a001581e())
        med = [f for f in res.flags
               if f.get("source") == "slot_cut" and f.get("flag_tier") == "MEDIUM"]
        assert med  # single-view through-all assumption is flagged


# --------------------------------------------------------------------------- #
# Corner geometry: open notch = 2 interior, closed slot / obround = 4
# --------------------------------------------------------------------------- #
class TestCornerCounts:
    def _model(self, **kw):
        m = DrawingData.model_validate(_a001581e(**kw))
        return m, m.slot_cuts[0]

    def test_open_notch_two_interior(self):
        m, slot = self._model()
        corners = corner_array(slot, m)
        assert len(corners) == 4
        assert len(interior_corners(slot, corners)) == 2
        assert expected_corner_count(slot) == 2

    def test_closed_slot_four_corners(self):
        m, slot = self._model(slot_kind="closed_slot", open_edge="")
        corners = corner_array(slot, m)
        assert len(interior_corners(slot, corners)) == 4
        assert expected_corner_count(slot) == 4

    def test_obround_four_corners(self):
        m, slot = self._model(slot_kind="obround", open_edge="")
        assert expected_corner_count(slot) == 4

    def test_edge_to_centerline_offsets_inboard(self):
        m, slot = self._model(anchor_semantics="edge_to_centerline")
        corners = corner_array(slot, m)
        # near edge is anchor - width/2 = 1.56 - 0.81 = 0.75
        assert min(c[0] for c in corners) == pytest.approx(0.75)


# --------------------------------------------------------------------------- #
# Legacy conversion: extrude_cut + child fillet -> one slot_cut
# --------------------------------------------------------------------------- #
class TestLegacyNormalization:
    def _legacy(self):
        return {
            "part_number": "LEG-1", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D001", "type": "linear", "value": 11.0, "unit": "inch", "applies_to": "length"},
                {"id": "D002", "type": "linear", "value": 6.25, "unit": "inch", "applies_to": "width"},
                {"id": "D003", "type": "depth", "value": 0.105, "unit": "inch", "applies_to": "height"},
                {"id": "D005", "type": "linear", "value": 1.62, "unit": "inch", "applies_to": "width"},
                {"id": "D006", "type": "linear", "value": 1.88, "unit": "inch", "applies_to": "depth"},
                {"id": "D012", "type": "radial", "value": 0.25, "unit": "inch", "applies_to": "fillet_radius"},
            ],
            "features": [
                {"id": "F001", "type": "extrude_boss", "description": "plate",
                 "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
                {"id": "F002", "type": "extrude_cut", "description": "U-notch slot cutout",
                 "related_dimensions": ["D005", "D006"]},
                {"id": "F003", "type": "fillet", "description": "notch corner fillet",
                 "parent_feature": "F002", "related_dimensions": ["D012"]},
            ],
            "build_order": ["F001", "F002", "F003"],
        }

    def test_pair_becomes_slot_cut(self):
        resolved = self._legacy()
        flags = []
        n = normalize_legacy_slots(resolved, flags.append)
        assert n == 1
        assert resolved["slot_cuts"][0]["id"] == "F002"
        assert resolved["slot_cuts"][0]["width"] == 1.62
        assert resolved["slot_cuts"][0]["depth"] == 1.88
        assert resolved["slot_cuts"][0]["corner_radius"] == 0.25
        # loose fillet feature dropped from features + build_order
        assert not any(f["id"] == "F003" for f in resolved["features"])
        assert "F003" not in resolved["build_order"]
        # extrude_cut feature kept
        assert any(f["id"] == "F002" for f in resolved["features"])
        assert flags and flags[0]["flag_tier"] == "MEDIUM"

    def test_legacy_via_full_resolve_emits_slot(self, tmp_path):
        _, _, _, plan = _build_pkg(self._legacy(), tmp_path)
        assert any(s["type"] == "slot_rect_cut" for s in plan["steps"])


# --------------------------------------------------------------------------- #
# Reconciliation: a missing rectangle is CRITICAL
# --------------------------------------------------------------------------- #
class TestReconciliation:
    def test_missing_rectangle_is_critical(self):
        raw = _a001581e()
        build_plan = {"steps": [], "skipped_prohibited": []}  # rectangle NOT built
        issues = slot_checks(raw, build_plan)
        assert issues and "CRITICAL" in issues[0].issue

    def test_present_rectangle_passes(self, tmp_path):
        raw = _a001581e()
        _, _, _, plan = _build_pkg(raw, tmp_path)
        assert slot_checks(raw, plan) == []

    def test_mispositioned_rectangle_flagged(self, tmp_path):
        raw = _a001581e()
        _, _, _, plan = _build_pkg(raw, tmp_path)
        # corrupt the rectangle position in the plan
        for s in plan["steps"]:
            if s["type"] == "slot_rect_cut":
                s["sketch"]["corners_drawing_units"] = [[5.0, 4.37], [6.62, 4.37],
                                                        [6.62, 6.25], [5.0, 6.25]]
        issues = slot_checks(raw, plan)
        assert issues and "mis-positioned" in issues[0].issue


# --------------------------------------------------------------------------- #
# Regression: a part with NO slot is byte-identical to before
# --------------------------------------------------------------------------- #
class TestNoSlotRegression:
    def _plain(self):
        return {
            "part_number": "PLAIN-1", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
                {"id": "D002", "type": "linear", "value": 2.0, "unit": "inch", "applies_to": "width"},
                {"id": "D003", "type": "depth", "value": 0.25, "unit": "inch", "applies_to": "height"},
            ],
            "features": [
                {"id": "F001", "type": "extrude_boss", "description": "plate",
                 "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
            ],
            "build_order": ["F001"],
        }

    def test_no_slot_steps_emitted(self, tmp_path):
        _, _, _, plan = _build_pkg(self._plain(), tmp_path)
        assert not any(s["type"] in ("slot_rect_cut", "slot_corner_fillet")
                       for s in plan["steps"])

    def test_slot_checks_noop_without_slots(self):
        assert slot_checks(self._plain(), {"steps": []}) == []
