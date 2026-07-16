"""Overview-analysis macro validation (pipeline/overview_macro_validate.py).

The validator cross-checks the generated macro package against the Stage 1.5
overview words: note counts vs drilled instances, cross-view feature coverage,
through-vs-blind agreement, conflict carryover, symmetry advisory. Advisory by
default; strict mode raises. All tests run on the frozen golden drawing (no
API, no SolidWorks).
"""
import json
from types import SimpleNamespace

import pytest

from pipeline.macro_generator import BuildStep, generate_macro_package
from pipeline.overview_macro_validate import (
    OverviewMacroValidationError,
    assert_overview_macro_validation,
    run_overview_macro_validation,
    validate_macros_against_overview,
)
from pipeline.validator import format_verification_report, run_verification
from tests.test_golden_macros import _golden_drawing


def _pkg(tmp_path):
    data = _golden_drawing()
    model, report = run_verification(data)
    assert report.ok, str(report)
    pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
    return model, pkg


def _overview(**overrides):
    base = {
        "part_number": "GOLDEN-1",
        "views_detected": [{"view_id": "front", "description": ""},
                           {"view_id": "side", "description": ""}],
        "cross_view_correspondences": [],
        "overall_shape_summary": "flat plate with four mounting holes",
        "global_notes": [],
        "cross_view_conflicts": [],
        "symmetry": {"type": "none_detected", "notes": ""},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Note counts vs drilled instances
# --------------------------------------------------------------------------- #
def test_note_count_pass_when_instances_match(tmp_path):
    model, pkg = _pkg(tmp_path)  # golden part drills exactly 4 holes
    ov = _overview(global_notes=[{"note": "(4) HLS", "applies_to": "mounting holes",
                                  "resolved_count": 4}])
    report = validate_macros_against_overview(model, pkg, ov)
    assert report.planned_hole_instances == 4
    entries = [e for e in report.entries if e.check == "note_count"]
    assert len(entries) == 1 and entries[0].status == "PASS"
    assert report.ok


def test_note_count_fail_when_instances_missing(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(global_notes=[{"note": "(6) HLS", "applies_to": "holes",
                                  "resolved_count": 6}])
    report = validate_macros_against_overview(model, pkg, ov)
    fails = [e for e in report.entries if e.check == "note_count" and e.status == "FAIL"]
    assert len(fails) == 1
    assert fails[0].severity == "CRITICAL"
    assert "2 instance(s) missing" in fails[0].detail
    assert not report.ok
    # FAIL findings become engineering-review items with the standard shape.
    items = report.review_items()
    assert items and items[0]["severity"] == "CRITICAL"
    assert items[0]["source"] == "overview_macro_validation"
    assert set(items[0]) >= {"what", "decision", "why", "affects", "id"}


def test_note_count_warn_when_note_governs_subset(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(global_notes=[{"note": "(2) TAPPED HLS", "applies_to": "",
                                  "resolved_count": 2}])
    report = validate_macros_against_overview(model, pkg, ov)
    entries = [e for e in report.entries if e.check == "note_count"]
    assert entries[0].status == "WARN"
    assert report.ok  # WARN never fails the report


def test_non_hole_and_inspection_notes_are_ignored(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(global_notes=[
        {"note": "3 VIEWS", "applies_to": "sheet layout", "resolved_count": 3},
        {"note": "10.00 IN.", "applies_to": "inspection", "resolved_count": 10},
        {"note": "FINISH ALL OVER", "applies_to": "all surfaces", "resolved_count": None},
    ])
    report = validate_macros_against_overview(model, pkg, ov)
    assert not [e for e in report.entries if e.check == "note_count"]


# --------------------------------------------------------------------------- #
# Correspondence coverage + through/blind agreement
# --------------------------------------------------------------------------- #
def test_correspondence_matches_and_confirms_through(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(cross_view_correspondences=[{
        "feature": "mounting_holes", "seen_in": ["front", "side"],
        "relation": "full-height hidden lines in the side view confirm THROUGH holes",
        "confidence": "high",
    }])
    report = validate_macros_against_overview(model, pkg, ov)
    cov = [e for e in report.entries if e.check == "correspondence_coverage"]
    assert cov and cov[0].status == "PASS"
    assert "F002" in cov[0].matched_feature_ids
    # Golden holes are thru → depth_type through_all → agreement PASS.
    tb = [e for e in report.entries if e.check == "through_blind"]
    assert tb and tb[0].status == "PASS"
    assert report.ok


def test_unmatched_high_confidence_correspondence_fails(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(cross_view_correspondences=[{
        "feature": "bottom_tab", "seen_in": ["front"],
        "relation": "tab profile at the bottom edge", "confidence": "high",
    }])
    report = validate_macros_against_overview(model, pkg, ov)
    fails = [e for e in report.entries if e.check == "correspondence_coverage"]
    assert fails[0].status == "FAIL" and fails[0].severity == "HIGH"
    assert not report.ok


def test_unmatched_medium_confidence_is_warn_only(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(cross_view_correspondences=[{
        "feature": "bottom_tab", "seen_in": ["front"],
        "relation": "", "confidence": "medium",
    }])
    report = validate_macros_against_overview(model, pkg, ov)
    entries = [e for e in report.entries if e.check == "correspondence_coverage"]
    assert entries[0].status == "WARN"
    assert report.ok


def test_sheet_furniture_correspondences_are_skipped(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(cross_view_correspondences=[{
        "feature": "title_block", "seen_in": ["title_block"],
        "relation": "", "confidence": "high",
    }])
    report = validate_macros_against_overview(model, pkg, ov)
    assert not [e for e in report.entries if e.check == "correspondence_coverage"]


def test_through_blind_contradiction_is_critical():
    """A fabricated blind hole against overview words confirming THROUGH."""
    steps = [BuildStep(3, "03_F009_Center_bore.vba", "F009", "hole",
                       "Center bore", "generated",
                       dimensions={"diameter": 0.5, "depth": 0.25},
                       positions_xy=[[1.0, 1.0]], depth_type="blind")]
    pkg = SimpleNamespace(steps=steps)
    ov = _overview(cross_view_correspondences=[{
        "feature": "center_bore", "seen_in": ["front", "side"],
        "relation": "hidden lines run the FULL height — a THROUGH bore, not blind",
        "confidence": "high",
    }])
    report = validate_macros_against_overview(None, pkg, ov)
    tb = [e for e in report.entries if e.check == "through_blind"]
    assert tb and tb[0].status == "FAIL" and tb[0].severity == "CRITICAL"
    assert tb[0].matched_feature_ids == ["F009"]
    assert not report.ok


def test_relation_mentioning_both_words_is_skipped():
    steps = [BuildStep(3, "03_F009_Center_bore.vba", "F009", "hole",
                       "Center bore", "generated",
                       dimensions={"diameter": 0.5}, positions_xy=[[1.0, 1.0]],
                       depth_type="blind")]
    pkg = SimpleNamespace(steps=steps)
    ov = _overview(cross_view_correspondences=[{
        "feature": "center_bore", "seen_in": ["front"],
        "relation": "unclear whether through or blind", "confidence": "medium",
    }])
    report = validate_macros_against_overview(None, pkg, ov)
    assert not [e for e in report.entries if e.check == "through_blind"]


# --------------------------------------------------------------------------- #
# Conflict carryover + symmetry advisory
# --------------------------------------------------------------------------- #
def test_critical_conflicts_carry_over_as_warn(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(cross_view_conflicts=[
        {"description": "5 visible holes vs '(6) HLS' callout",
         "views_involved": ["front"], "severity": "CRITICAL",
         "recommendation": "check for an occluded hole behind the title block"},
        {"description": "minor line weight difference", "views_involved": [],
         "severity": "LOW", "recommendation": ""},
    ])
    report = validate_macros_against_overview(model, pkg, ov)
    carried = [e for e in report.entries if e.check == "conflict_carryover"]
    assert len(carried) == 1  # LOW conflicts are not re-surfaced
    assert carried[0].status == "WARN" and carried[0].severity == "CRITICAL"
    assert report.ok  # carryover warns, never fails


def test_rotational_symmetry_without_pattern_is_advisory(tmp_path):
    model, pkg = _pkg(tmp_path)  # 4 baked-circle holes, no pattern step
    ov = _overview(symmetry={"type": "rotational", "notes": "4x symmetry implied"})
    report = validate_macros_against_overview(model, pkg, ov)
    adv = [e for e in report.entries if e.check == "symmetry_advisory"]
    assert adv and adv[0].status == "WARN" and adv[0].severity == "LOW"
    assert report.ok


# --------------------------------------------------------------------------- #
# Persistence + wiring + strict mode
# --------------------------------------------------------------------------- #
def test_run_returns_none_without_overview_file(tmp_path):
    model, pkg = _pkg(tmp_path)
    assert run_overview_macro_validation(model, pkg) is None


def test_run_writes_report_file(tmp_path):
    model, pkg = _pkg(tmp_path)
    (pkg.root / "overview_analysis.json").write_text(
        json.dumps(_overview(global_notes=[{"note": "(4) HLS", "applies_to": "holes",
                                            "resolved_count": 4}])),
        encoding="utf-8")
    report = run_overview_macro_validation(model, pkg)
    assert report is not None and report.ok
    out = pkg.root / "GOLDEN-1-RevA_macro_overview_validation.json"
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["planned_hole_instances"] == 4
    assert payload["counts"]["FAIL"] == 0


def test_generate_macro_package_folds_findings_into_plan(tmp_path):
    """End-to-end wiring: an overview_analysis.json present at generation time
    lands in build_plan.json (summary) and the engineering review (FAILs)."""
    data = _golden_drawing()
    model, report = run_verification(data)
    part_dir = tmp_path / "GOLDEN-1-RevA"
    part_dir.mkdir(parents=True)
    (part_dir / "overview_analysis.json").write_text(
        json.dumps(_overview(global_notes=[{"note": "(6) HLS", "applies_to": "holes",
                                            "resolved_count": 6}])),
        encoding="utf-8")
    pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
    plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
    assert plan["overview_macro_validation"]["ok"] is False
    review_sources = {it["source"] for it in plan["engineering_review"]}
    assert "overview_macro_validation" in review_sources
    # The standalone report is also on disk.
    assert (part_dir / "GOLDEN-1-RevA_macro_overview_validation.json").is_file()


def test_strict_mode_raises_on_fail(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview(global_notes=[{"note": "(9) HLS", "applies_to": "holes",
                                  "resolved_count": 9}])
    with pytest.raises(OverviewMacroValidationError, match="9 hole feature"):
        assert_overview_macro_validation(model, pkg, ov)


def test_strict_mode_passes_clean(tmp_path):
    model, pkg = _pkg(tmp_path)
    ov = _overview()
    report = assert_overview_macro_validation(model, pkg, ov)
    assert report.ok
