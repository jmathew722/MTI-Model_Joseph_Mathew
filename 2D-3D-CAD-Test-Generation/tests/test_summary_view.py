"""Tab-3 visual-summary view-model builder (pipeline/summary_view.py).

Runs against frozen copies of REAL golden-part artifacts (158-C, 127-C /
A001271E, M_121-B / A001641E) under tests/fixtures/summary/. Covers the
formatting rules, the merged disposition⊕verification result logic, graceful
degradation when an artifact is missing, and the acceptance criteria from the
task (a human can read the notch, the holes, and each build step's
stage/operation/result WITHOUT opening any JSON).
"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.summary_view import (
    build_summary, fmt_num, _basis_label, _merge_result, _envelope,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "summary"


@pytest.fixture(scope="module")
def c158():
    return build_summary(FIX / "158-C")


@pytest.fixture(scope="module")
def c127():
    return build_summary(FIX / "127-C")


@pytest.fixture(scope="module")
def m121():
    return build_summary(FIX / "M_121-B")


# --------------------------------------------------------------------------- #
# Formatting rules (the one place meters/JSON never escape)
# --------------------------------------------------------------------------- #
class TestFormatting:
    def test_trailing_zeros_trimmed(self):
        assert fmt_num(1.5600000000000001) == "1.56"
        assert fmt_num(6.25) == "6.25"
        assert fmt_num(11.0) == "11"

    def test_leading_zero_dropped_drawing_style(self):
        assert fmt_num(0.105) == ".105"
        assert fmt_num(0.218) == ".218"
        assert fmt_num(-0.5) == "-.5"

    def test_zero_and_nonnumbers(self):
        assert fmt_num(0) == "0"
        assert fmt_num(None) is None
        assert fmt_num("nope") is None
        assert fmt_num(True) is None  # bools are not measurements

    def test_basis_labels(self):
        assert _basis_label("extracted_verbatim") == "Extracted"
        assert _basis_label("explicit_callout") == "Extracted"
        assert _basis_label("") == "Extracted"
        assert _basis_label("arithmetic_chain") == "Derived-chain"
        assert _basis_label("profile_delta") == "Derived-profile"
        assert _basis_label("committed_conservative") == "Committed"
        assert _basis_label("standard_thread_size") == "Standard"
        assert _basis_label("spec_driven") == "Spec"

    def test_envelope_display_uses_drawing_units(self):
        dims = [
            {"applies_to": "length", "value": 11.0},
            {"applies_to": "width", "value": 6.25},
            {"applies_to": "thickness", "value": 0.105},
        ]
        env = _envelope(dims, "inch")
        assert env["display"] == "11 × 6.25 × .105 inch"

    def test_envelope_accepts_height_as_second_axis(self):
        dims = [
            {"applies_to": "length", "value": 8.0},
            {"applies_to": "height", "value": 4.0},
        ]
        env = _envelope(dims, "inch")
        assert env["display"].startswith("8 × 4")

    def test_envelope_falls_back_to_base_values(self):
        env = _envelope([], "inch", {"length": 6.88, "width": 6.88, "thickness": 0.25})
        assert env["display"] == "6.88 × 6.88 × .25 inch"


# --------------------------------------------------------------------------- #
# Merged result logic (disposition ⊕ per-feature verification verdict)
# --------------------------------------------------------------------------- #
class TestMergedResult:
    def test_built_no_verification_is_ok(self):
        assert _merge_result("BUILT", "", False, []) == ("Built", "ok")

    def test_built_verified_when_verdict_ok(self):
        assert _merge_result("BUILT", "OK", True, []) == ("Built ✓ verified", "ok")

    def test_misplaced_verdict_is_warn(self):
        label, kind = _merge_result("BUILT", "MISPLACED", True, [])
        assert kind == "warn" and "misplaced" in label.lower()

    def test_excluded_is_err_regardless(self):
        assert _merge_result("EXCLUDED_INCOMPLETE", "", False, []) == ("Excluded ✗", "err")

    def test_derived_or_flagged_is_warn(self):
        assert _merge_result("BUILT_WITH_DERIVED_VALUE", "", False, [])[1] == "warn"
        crit = [{"flag_tier": "CRITICAL"}]
        assert _merge_result("BUILT", "", False, crit)[1] == "warn"

    def test_phantom_is_neutral(self):
        assert _merge_result("PHANTOM_RECLASSIFIED", "", False, [])[1] == "neutral"


# --------------------------------------------------------------------------- #
# Acceptance (a): 158-C readable without opening any JSON
# --------------------------------------------------------------------------- #
class TestAcceptance158C:
    def test_header_reads_at_a_glance(self, c158):
        h = c158["header"]
        assert c158["ran"] is True
        assert h["envelope"]["display"] == "11 × 6.25 × .105 inch"
        assert h["final_status"] == "READY"

    def test_notch_size_position_basis(self, c158):
        f002 = next(f for f in c158["features"] if f["id"] == "F002")
        assert "1.62" in f002["size"] and "1.88" in f002["size"]
        assert f002["basis"] == "Extracted"

    def test_notch_build_step_placed_at_drawing_position(self, c158):
        # the slot rectangle cut is placed at the dimensioned (1.56, 4.37)
        step = next(s for s in c158["build_steps"]
                    if s["feature_id"] == "F002" and "cut" in s["operation"].lower())
        assert "(1.56, 4.37)" in step["placement"]

    def test_six_diameter_218_holes(self, c158):
        f004 = next(f for f in c158["features"] if f["id"] == "F004")
        assert f004["size"] == "⌀.218"
        assert f004["qty"] == 6

    def test_every_build_step_has_stage_operation_result(self, c158):
        assert c158["build_steps"]
        for s in c158["build_steps"]:
            assert s["stage"] and s["stage"] != ""
            assert s["operation"] and s["operation"] != ""
            assert s["result"] and s["result_kind"] in ("ok", "warn", "err", "neutral")

    def test_feature_verification_absent_degrades_gracefully(self, c158):
        # no *_feature_verification.json exists for this part
        assert c158["header"]["verification_available"] is False
        # ...and the build still reads as built, never crashes or blanks
        assert all(s["result"] for s in c158["build_steps"])


# --------------------------------------------------------------------------- #
# Acceptance (b): a flagged/failed feature is findable by badge in seconds
# --------------------------------------------------------------------------- #
class TestAcceptanceBadges:
    def test_excluded_features_are_red(self, m121):
        excluded = [f for f in m121["features"] if f["status"].startswith("Excluded")]
        assert excluded, "M_121-B has EXCLUDED_INCOMPLETE features"
        assert all(f["status_kind"] == "err" for f in excluded)

    def test_derived_value_feature_is_amber(self, m121):
        derived = [f for f in m121["features"] if f["status"] == "Derived"]
        assert derived and all(f["status_kind"] == "warn" for f in derived)

    def test_final_status_reflects_open_items(self, m121):
        assert m121["header"]["final_status"] == "READY_WITH_OPEN_ITEMS"

    def test_reference_dimension_lands_in_notes_not_geometry(self, m121):
        labels = [n["label"] for n in m121["notes"]]
        assert any("reference" in l.lower() for l in labels)


# --------------------------------------------------------------------------- #
# Assist-queue awareness (a pending question surfaces a "?" affix)
# --------------------------------------------------------------------------- #
class TestAssistAwareness:
    def test_pending_question_flags_feature(self, c127):
        assert c127["header"]["pending_questions"] >= 1
        flagged = [f for f in c127["features"] if f["has_question"]]
        assert flagged, "127-C has a pending assist question on a feature"
        # a question nudges an otherwise-clean feature to amber
        assert all(f["status_kind"] in ("warn", "err") for f in flagged)

    def test_clean_part_has_no_question_affix(self, c158):
        assert c158["header"]["pending_questions"] == 0
        assert all(not f["has_question"] for f in c158["features"])


# --------------------------------------------------------------------------- #
# Graceful degradation with missing / partial artifacts
# --------------------------------------------------------------------------- #
class TestGracefulDegradation:
    def test_empty_dir_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vm = build_summary(Path(td))
        assert vm["ran"] is False
        assert vm["features"] == [] and vm["build_steps"] == []

    def test_nonexistent_dir_never_raises(self):
        vm = build_summary(Path("does/not/exist/anywhere"))
        assert vm["ran"] is False

    def test_resolved_only_still_lists_features(self):
        # only the resolved extraction present — no plan/dispositions
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            src = json.loads((FIX / "158-C" / "158-C_resolved_extraction.json").read_text())
            (out / "P_resolved_extraction.json").write_text(json.dumps(src))
            vm = build_summary(out)
        assert vm["ran"] is True
        assert len(vm["features"]) >= 4
        assert vm["build_steps"] == []  # no build_plan → pending, not a crash

    def test_no_null_or_empty_leaks_to_ui(self, c158, c127, m121):
        # every displayed cell is a non-empty string (dash for absent), never
        # None / "" / "null".
        for vm in (c158, c127, m121):
            for f in vm["features"]:
                for k in ("id", "type_label", "size", "position", "basis", "status"):
                    assert isinstance(f[k], str) and f[k] != "" and f[k] != "null"
            for s in vm["build_steps"]:
                for k in ("operation", "stage", "key_values", "placement", "result"):
                    assert isinstance(s[k], str) and s[k] != "" and s[k] != "null"
