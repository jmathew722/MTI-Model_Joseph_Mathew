"""Per-instance hole placement with datum chaining (2026-07-12) — A001271E.

Evidence part: a 17.50 x 11.250 x 1.00 plate with 4 corner c'bore holes (.500)
and 4 deliberately-asymmetric inner thru holes (.531). Its previous build
collapsed the inner group into overlapping/misplaced instances and emitted two
competing F001 base macros. All defects are visible in build_plan.json pre-COM.
"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.macro_generator import (
    MacroGenerationError,
    generate_macro_package,
    is_verified_pattern,
    _assert_no_overlapping_holes,
    BuildStep,
)
from pipeline.resolver import resolve_extraction
from pipeline.schema import DrawingData, HoleCallout
from pipeline.validator import format_verification_report, run_verification

FIX = Path(__file__).resolve().parent / "fixtures" / "commit_mode"

# The 8 holes' exact origin-frame coordinates, frozen from the drawing chains.
EXPECTED = {
    "F002": [0.844, 10.25], "F003": [16.656, 10.25],
    "F004": [0.844, 4.0],   "F005": [16.656, 4.0],
    "F006": [6.781, 7.25],  "F007": [7.281, 7.25],
    "F008": [6.781, 4.75],  "F009": [13.781, 4.75],
}


def _build(raw, out=None):
    res = resolve_extraction(raw)
    model, rep = run_verification(res.clean_extraction)
    out = out or Path(tempfile.mkdtemp())
    pkg = generate_macro_package(model, raw, format_verification_report(model, rep),
                                 out, resolution=res)
    plan = json.loads(pkg.build_plan_json.read_text())
    return res, model, pkg, plan


@pytest.fixture(scope="module")
def a271():
    return json.loads((FIX / "A001271E_extraction.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Acceptance (a) — all 8 holes at their individual coordinates, zero overlaps
# --------------------------------------------------------------------------- #
class TestIndividualPlacement:
    def test_all_eight_holes_at_expected_coordinates(self, a271):
        _res, _model, _pkg, plan = _build(a271)
        got = {}
        for s in plan["steps"]:
            if s.get("type") == "hole":
                assert len(s["positions_xy"]) == 1, (s["feature_id"], s["positions_xy"])
                got[s["feature_id"]] = [round(v, 3) for v in s["positions_xy"][0]]
        assert got == EXPECTED

    def test_no_overlapping_instances(self, a271):
        _res, _model, _pkg, plan = _build(a271)
        pts = [tuple(round(v, 3) for v in s["positions_xy"][0])
               for s in plan["steps"] if s.get("type") == "hole"]
        assert len(pts) == 8 and len(set(pts)) == 8


# --------------------------------------------------------------------------- #
# Acceptance (b) — placement: individual + per-instance datum chains
# --------------------------------------------------------------------------- #
class TestClassificationAndChains:
    def test_both_groups_classified_individual(self, a271):
        res, _m, _p, plan = _build(a271)
        for s in plan["steps"]:
            if s.get("type") == "hole":
                assert s.get("placement") == "individual", s["feature_id"]
                assert s.get("pattern_evidence") == "none->individual"

    def test_every_hole_has_position_basis(self, a271):
        _res, _m, _p, plan = _build(a271)
        for s in plan["steps"]:
            if s.get("type") == "hole":
                assert s.get("position_basis"), s["feature_id"]
                for b in s["position_basis"]:
                    assert {"anchor", "dim", "value"} <= set(b)

    def test_inner_holes_anchor_to_hole_centers_with_datum_points(self, a271):
        # The inner group's chains run hole-to-hole (.500 / 7.000 dims) -> datum pts.
        _res, _m, pkg, plan = _build(a271)
        f007 = next(s for s in plan["steps"] if s.get("feature_id") == "F007")
        anchors = {b["anchor"] for b in f007["position_basis"]}
        assert "hole_center" in anchors
        assert f007.get("datum_points") == ["REF_PT_F007"]
        # Acceptance (c): the datum point is a real reference point in the macro
        # package, built in 01a (before the Stage-4 holes that reference it).
        refgeo = (pkg.macros_dir / "01a_reference_geometry.vba")
        assert refgeo.is_file()
        assert "REF_PT_F007" in refgeo.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Pattern-vs-individual classifier
# --------------------------------------------------------------------------- #
class TestPatternClassifier:
    def _callout(self, **kw):
        base = dict(id="H1", type="thru", diameter=0.25, qty=4, feature_ref="F002")
        base.update(kw)
        return HoleCallout(**base)

    def test_no_evidence_is_individual(self):
        ok, ev = is_verified_pattern(None, self._callout(qty=4))
        assert not ok
        ok, ev = is_verified_pattern(None, self._callout(qty=4, pattern="none"))
        assert not ok and ev == "none->individual"

    def test_bolt_circle_is_pattern(self):
        ok, ev = is_verified_pattern(None, self._callout(pattern="circular", bolt_circle_diameter=3.0))
        assert ok and "bolt_circle" in ev

    def test_uniform_pitch_is_pattern(self):
        ok, ev = is_verified_pattern(None, self._callout(pattern="linear", pattern_spacing=1.5, qty=5))
        assert ok and "uniform_pitch" in ev


# --------------------------------------------------------------------------- #
# Acceptance (d) — macro-package dedup (the double-F001 defect)
# --------------------------------------------------------------------------- #
class TestDedup:
    def test_single_base_macro(self, a271):
        _res, _m, pkg, _plan = _build(a271)
        base = list(pkg.macros_dir.glob("01_F001*.vba"))
        assert len(base) == 1, [p.name for p in base]

    def test_rerun_with_changed_description_leaves_one_base_macro(self, a271):
        out = Path(tempfile.mkdtemp())
        _build(a271, out)                                  # first run
        raw2 = json.loads(json.dumps(a271))
        for f in raw2["features"]:
            if f["id"] == "F001":
                f["description"] = "Rectangular plate renamed"
        _res, _m, pkg, _plan = _build(raw2, out)           # re-run, same dir
        base = list(pkg.macros_dir.glob("01_F001*.vba"))
        assert len(base) == 1, [p.name for p in base]      # stale one was cleared

    def test_duplicate_feature_in_build_order_refused(self, a271):
        raw = json.loads(json.dumps(a271))
        res = resolve_extraction(raw)
        model, rep = run_verification(res.clean_extraction)
        model.build_order = list(model.build_order) + [model.build_order[-1]]  # inject dup
        with pytest.raises(MacroGenerationError, match="DUPLICATE FEATURE"):
            generate_macro_package(model, raw, format_verification_report(model, rep),
                                   Path(tempfile.mkdtemp()), resolution=res)


# --------------------------------------------------------------------------- #
# Acceptance (e) — duplicate-position invariant crashes a synthetic violation
# --------------------------------------------------------------------------- #
class TestDuplicatePositionInvariant:
    def test_overlapping_holes_refused(self, a271):
        model = DrawingData.model_validate(a271)
        # two .531 instances at (almost) the same coordinate
        steps = [
            BuildStep(4, "a.vba", "F006", "hole", "h", "generated",
                      positions_xy=[[6.781, 7.25]]),
            BuildStep(5, "b.vba", "F007", "hole", "h", "generated",
                      positions_xy=[[6.7815, 7.2503]]),
        ]
        with pytest.raises(MacroGenerationError, match="OVERLAPPING HOLES"):
            _assert_no_overlapping_holes(model, steps)

    def test_distinct_holes_pass(self, a271):
        model = DrawingData.model_validate(a271)
        steps = [
            BuildStep(4, "a.vba", "F006", "hole", "h", "generated", positions_xy=[[6.781, 7.25]]),
            BuildStep(5, "b.vba", "F007", "hole", "h", "generated", positions_xy=[[7.281, 7.25]]),
        ]
        _assert_no_overlapping_holes(model, steps)  # must not raise
