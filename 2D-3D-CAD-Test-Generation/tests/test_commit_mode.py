"""Commit-to-extraction mode (2026-07-11) — build every extracted feature.

Acceptance (headless; no live SolidWorks — both bugs are visible in
build_plan.json before COM runs):
  * Bug-1 invariant crashes on a synthetic dropped-position violation.
  * Y-flip: a top-edge notch and a bottom-edge notch land at opposite Y ends.
  * profile-delta: M_121-B's two step cuts build fully-dimensioned.
  * commit-mode: zero terminal excluded/review states and zero [0,0] placeholder
    positions across the evidence fixtures.
  * goldens: 158-C notch (1.62 x 1.88, top edge, x=1.56, built) and M_121-B
    (2 extrude_cuts -> 2 built).
"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.macro_generator import (
    MacroGenerationError,
    generate_macro_package,
    _assert_no_dropped_positions,
)
from pipeline.resolver import resolve_extraction
from pipeline.schema import DrawingData
from pipeline.slot_cut import EDGE_OVERSHOOT_EPS, corner_array
from pipeline.validator import format_verification_report, run_verification

FIX = Path(__file__).resolve().parent / "fixtures" / "commit_mode"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _build(raw):
    res = resolve_extraction(raw)                       # commit_mode default ON
    model, rep = run_verification(res.clean_extraction)
    tmp = Path(tempfile.mkdtemp())
    pkg = generate_macro_package(model, raw, format_verification_report(model, rep),
                                 tmp, resolution=res)
    disp = json.loads((pkg.root / f"{pkg.root.name}_build_dispositions.json").read_text())
    plan = json.loads(pkg.build_plan_json.read_text())
    return res, model, pkg, disp, plan


# --------------------------------------------------------------------------- #
# Bug 1 — invariant makes the dropped-position failure state unrepresentable
# --------------------------------------------------------------------------- #
class TestBug1Invariant:
    def _slot_model(self):
        raw = _load("158-C_extraction.json")
        res = resolve_extraction(raw)
        model, _ = run_verification(res.clean_extraction)
        return model

    def test_invariant_crashes_on_synthetic_violation(self):
        # F002 (the notch) HAS an extracted position (slot anchor D002=1.56). A
        # disposition claiming its position is unresolved is a dropped-position
        # bug — the invariant must refuse.
        model = self._slot_model()
        bad = [{"feature_id": "F002", "state": "BUILT_WITH_DERIVED_VALUE",
                "derivation_source": "position:needs_markup_review", "position_xy": [0.0, 0.0]}]
        with pytest.raises(MacroGenerationError, match="INVARIANT VIOLATION"):
            _assert_no_dropped_positions(model, bad)

    def test_invariant_passes_when_position_consumed(self):
        # The real, fixed pipeline: F002's position is consumed, so no violation.
        model = self._slot_model()
        good = [{"feature_id": "F002", "state": "BUILT",
                 "derivation_source": "", "position_xy": [1.56, 4.37]}]
        _assert_no_dropped_positions(model, good)  # must not raise

    def test_full_build_of_158c_does_not_trip_invariant(self):
        _res, _model, _pkg, disp, _plan = _build(_load("158-C_extraction.json"))
        f002 = next(d for d in disp if d["feature_id"] == "F002")
        assert "needs_markup_review" not in str(f002.get("derivation_source", ""))
        assert f002["position_xy"] != [0.0, 0.0]


# --------------------------------------------------------------------------- #
# Bug 2 — Y-axis / edge inversion (top vs bottom anchoring)
# --------------------------------------------------------------------------- #
class TestYFlip:
    def _notch_part(self, open_edge):
        return {
            "part_number": f"YFLIP-{open_edge}", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D001", "type": "linear", "value": 10.0, "unit": "inch", "applies_to": "length"},
                {"id": "D002", "type": "linear", "value": 6.0, "unit": "inch", "applies_to": "width"},
                {"id": "D003", "type": "depth", "value": 0.25, "unit": "inch", "applies_to": "height"},
                {"id": "D004", "type": "linear", "value": 2.0, "unit": "inch", "applies_to": "slot_offset"},
                {"id": "D005", "type": "linear", "value": 1.5, "unit": "inch", "applies_to": "slot_width"},
                {"id": "D006", "type": "linear", "value": 1.0, "unit": "inch", "applies_to": "slot_depth"},
            ],
            "features": [
                {"id": "F001", "type": "extrude_boss", "description": "plate",
                 "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
                {"id": "F002", "type": "extrude_cut", "description": f"{open_edge} notch",
                 "related_dimensions": []},
            ],
            "slot_cuts": [{"id": "F002", "slot_kind": "open_notch", "open_edge": open_edge,
                           "anchor_edge": "left", "anchor_offset": 2.0, "width": 1.5, "depth": 1.0,
                           "corner_radius": 0.0, "thru": True, "thru_basis": "single_view_default"}],
            "build_order": ["F001", "F002"],
        }

    def test_top_and_bottom_notch_land_at_opposite_y_ends(self):
        height = 6.0
        top = DrawingData.model_validate(self._notch_part("top"))
        bot = DrawingData.model_validate(self._notch_part("bottom"))
        top_corners = corner_array(top.slot_cuts[0], top)
        bot_corners = corner_array(bot.slot_cuts[0], bot)
        top_ys = [c[1] for c in top_corners]
        bot_ys = [c[1] for c in bot_corners]
        # The OPEN side overshoots its edge by EDGE_OVERSHOOT_EPS so the cut
        # breaks the edge cleanly: the top notch opens PAST the top (height+eps),
        # the bottom notch opens PAST y=0 (-eps). Closed ends stay exact.
        assert max(top_ys) == pytest.approx(height + EDGE_OVERSHOOT_EPS)
        assert min(top_ys) == pytest.approx(height - 1.0)
        assert min(bot_ys) == pytest.approx(-EDGE_OVERSHOOT_EPS)
        assert max(bot_ys) == pytest.approx(1.0)
        # opposite ends: the top notch's whole span is above the bottom notch's
        assert min(top_ys) > max(bot_ys)


# --------------------------------------------------------------------------- #
# Golden: 158-C notch, top edge, 1.62 x 1.88, x = 1.56, built
# --------------------------------------------------------------------------- #
class TestGolden158C:
    def test_notch_built_top_edge_correct_geometry(self):
        _res, _model, _pkg, disp, plan = _build(_load("158-C_extraction.json"))
        f002 = next(d for d in disp if d["feature_id"] == "F002")
        assert f002["state"] in ("BUILT", "BUILT_WITH_DERIVED_VALUE")
        rect = next(s for s in plan["steps"] if s.get("type") == "slot_rect_cut"
                    and "F002" in str(s.get("feature_id")))
        # open top side overshoots the 6.25 edge by EDGE_OVERSHOOT_EPS -> 6.30
        assert rect["sketch"]["corners_drawing_units"] == [
            [1.56, 4.37], [3.18, 4.37], [3.18, 6.3], [1.56, 6.3]]

    def test_no_terminal_excluded_or_review_states(self):
        _res, _model, _pkg, disp, _plan = _build(_load("158-C_extraction.json"))
        assert all(d["state"] != "EXCLUDED_INCOMPLETE" for d in disp)
        assert all("needs_markup_review" not in str(d.get("derivation_source", "")) for d in disp)


# --------------------------------------------------------------------------- #
# Golden: M_121-B — both step cuts build (profile-delta), F005 inherits sibling dia
# --------------------------------------------------------------------------- #
class TestGoldenM121B:
    def test_both_step_cuts_built(self):
        _res, _model, _pkg, disp, _plan = _build(_load("M_121-B_extraction.json"))
        cuts = [d for d in disp if d.get("type") == "extrude_cut"]
        assert len(cuts) == 2
        for c in cuts:
            # the missing size came from the outer profile chain -> derived + built
            assert c["state"] == "BUILT_WITH_DERIVED_VALUE", c

    def test_profile_delta_dimensions_present(self):
        res, _model, _pkg, _disp, _plan = _build(_load("M_121-B_extraction.json"))
        pd = [d for d in res.resolved_extraction["dimensions"]
              if d.get("assumption_basis") == "profile_delta"]
        assert pd, "expected profile_delta-derived dimensions for the step cuts"

    def test_sibling_hole_diameter_inherited(self):
        res, _model, _pkg, disp, _plan = _build(_load("M_121-B_extraction.json"))
        # F005 (2nd of the '(2) HOLES' .422) inherits its sibling's diameter.
        f005 = next(d for d in disp if d["feature_id"] == "F005")
        assert f005["state"] in ("BUILT", "BUILT_WITH_DERIVED_VALUE")
        assert f005["state"] != "EXCLUDED_INCOMPLETE"


# --------------------------------------------------------------------------- #
# Cross-fixture acceptance: no exclusions, no [0,0] placeholders
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fixture", ["158-C_extraction.json", "M_121-B_extraction.json"])
class TestCommitAcceptance:
    def test_zero_excluded_or_review_features(self, fixture):
        _res, _model, _pkg, disp, _plan = _build(_load(fixture))
        assert all(d["state"] != "EXCLUDED_INCOMPLETE" for d in disp), fixture
        assert all("needs_markup_review" not in str(d.get("derivation_source", "")) for d in disp)

    def test_no_placeholder_zero_positions_on_locatable_features(self, fixture):
        _res, _model, _pkg, disp, plan = _build(_load(fixture))
        # A locatable cut/hole/slot step must never sit at a [0,0] placeholder.
        for s in plan["steps"]:
            t = s.get("type", "")
            if t in ("slot_rect_cut", "extrude_cut", "hole") and s.get("positions_xy"):
                assert s["positions_xy"] != [[0.0, 0.0]], (fixture, t, s.get("feature_id"))


# --------------------------------------------------------------------------- #
# commit_mode=False preserves the legacy exclude/review behavior (comparison)
# --------------------------------------------------------------------------- #
class TestLegacyModeStillExcludes:
    def test_m121b_excludes_step_cuts_when_commit_off(self):
        raw = _load("M_121-B_extraction.json")
        res = resolve_extraction(raw, commit_mode=False)
        excluded = {f["dimension_id"] for f in res.flags if f.get("excluded_from_build")}
        # at least the under-dimensioned step cuts are excluded in legacy mode
        assert {"F002", "F003"} & excluded
