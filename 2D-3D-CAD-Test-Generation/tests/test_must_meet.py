"""Must-meet spec reconciliation (Stage 2.6) + circular-pattern layer tests.

Covers the acceptance path end-to-end without APIs or SolidWorks:
deterministic spec parsing (the exact acceptance text), tier-0 application
with conflict/derivation records, bolt-circle fitting, the pattern-vs-
coordinate routing rule, canonical circular_pattern build-plan schema (with
null refusal), the generated VBA contract, and the CadQuery pre-validation +
constraint evaluation (including the corrupted 5-hole negative case).
"""
import json
import math

import pytest

from pipeline.must_meet import (
    apply_must_meet,
    equal_spacing_deviation,
    evaluate_constraints,
    fit_bolt_circle,
    parse_spec_text_fallback,
)

SPEC = ("there must be 6 holes in this part that can be done using a circular "
        "pattern, there must be an extrude cut from the center that is 3.880 "
        "diameter, there must be a 1.25 diameter extrude cut feature that is in "
        "the bottom and is in the front view that is 2.94 from the center of the "
        "other circular extrude cut. All holes must be through all.")

OD, T, BOLT_R, HOLE_D, BORE_D, CUT_D, CUT_OFF = 7.50, 0.50, 2.880, 0.406, 3.880, 1.25, 2.94
CX = CY = OD / 2.0


def _positions(n=6, r=BOLT_R):
    return [[round(CX + r * math.cos(math.radians(60 * i)), 3),
             round(CY + r * math.sin(math.radians(60 * i)), 3)] for i in range(n)]


def _extraction():
    return {
        "part_number": "A050211E", "part_name": "FLANGE PLATE", "units": "inch",
        "confidence": 0.9,
        "views": [{"view_type": "front", "description": "front"},
                  {"view_type": "side", "description": "side"}],
        "dimensions": [
            {"id": "D001", "type": "diameter", "value": OD, "unit": "inch",
             "applies_to": "outside_diameter", "feature_ref": "F001", "view": "front"},
            {"id": "D002", "type": "linear", "value": T, "unit": "inch",
             "applies_to": "thickness", "feature_ref": "F001", "view": "side"},
            {"id": "D003", "type": "diameter", "value": BORE_D, "unit": "inch",
             "applies_to": "diameter", "feature_ref": "F002", "view": "front"},
            {"id": "D004", "type": "diameter", "value": CUT_D, "unit": "inch",
             "applies_to": "diameter", "feature_ref": "F004", "view": "front"},
        ],
        "hole_callouts": [
            {"id": "H002", "type": "thru", "diameter": BORE_D, "thru": True, "qty": 1,
             "x_position": CX, "y_position": CY, "position_known": True,
             "instance_positions": [[CX, CY]], "feature_ref": "F002"},
            {"id": "H003", "type": "thru", "diameter": HOLE_D, "thru": True, "qty": 6,
             "position_known": True, "instance_positions": _positions(),
             "feature_ref": "F003"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Base round plate",
             "related_dimensions": ["D001", "D002"], "sketch_plane": "front",
             "depth_dimension_id": "D002", "position_known": True,
             "offset_x": CX, "offset_y": CY},
            {"id": "F002", "type": "hole", "description": "Center bore",
             "related_dimensions": ["D003"], "sketch_plane": "front",
             "position_known": True, "offset_x": CX, "offset_y": CY},
            {"id": "F003", "type": "hole", "description": "Bolt holes",
             "related_dimensions": [], "sketch_plane": "front"},
            {"id": "F004", "type": "extrude_cut", "description": "Offset circular cut",
             "related_dimensions": ["D004"], "sketch_plane": "front",
             "position_known": True, "offset_x": CX, "offset_y": CY - CUT_OFF},
        ],
        "build_order": ["F001", "F002", "F003", "F004"],
    }


# ── Part 1: parsing ─────────────────────────────────────────────────────────
def test_fallback_parser_acceptance_text():
    cons = parse_spec_text_fallback(SPEC)
    assert [c["id"] for c in cons] == ["MM-001", "MM-002", "MM-003", "MM-004"]
    assert cons[0]["type"] == "circular_pattern" and cons[0]["hole_count_total"] == 6
    assert cons[1]["type"] == "cut_extrude" and cons[1]["diameter_in"] == pytest.approx(3.880)
    assert cons[1]["position"] == "center"
    assert cons[2]["diameter_in"] == pytest.approx(1.25)
    pos = cons[2]["position"]
    assert pos["offset_in"] == pytest.approx(2.94)
    assert pos["reference"] == "center_of_MM-002"
    assert pos["direction"] == "down" and pos["view"] == "front"
    assert cons[3]["type"] == "global_modifier"
    assert cons[3]["end_condition"] == "through_all"
    assert "all_holes" in cons[3]["applies_to"]


def test_bolt_circle_fit_and_spacing():
    fit = fit_bolt_circle(_positions())
    assert fit["radius"] == pytest.approx(BOLT_R, abs=0.001)
    assert fit["max_disagreement"] <= 0.005
    spacing = equal_spacing_deviation(_positions(), fit["center"])
    assert spacing["worst_arc_deviation"] <= 0.005


# ── Tier-0 application ──────────────────────────────────────────────────────
def test_apply_sets_pattern_and_derives_bolt_circle():
    cons = parse_spec_text_fallback(SPEC)
    app = apply_must_meet(_extraction(), cons, part="A050211E")
    h = next(h for h in app.extraction["hole_callouts"] if h["id"] == "H003")
    assert h["pattern"] == "circular"
    assert h["bolt_circle_diameter"] == pytest.approx(2 * BOLT_R, abs=0.01)
    assert any(c["field"] == "pattern" and c["resolution"] == "spec_override"
               for c in app.conflicts)
    derived = [d for d in app.derived if d["value_name"] == "bolt_circle_radius_in"]
    assert derived and derived[0]["value"] == pytest.approx(BOLT_R, abs=0.001)
    assert "derivation" in derived[0]


def test_apply_count_disagreement_flags_critical_and_keeps_drawing():
    cons = parse_spec_text_fallback(SPEC.replace("6 holes", "5 holes"))
    app = apply_must_meet(_extraction(), cons, part="A050211E")
    h = next(h for h in app.extraction["hole_callouts"] if h["id"] == "H003")
    assert h["qty"] == 6  # drawing keeps its 6 dimensioned positions
    assert any(c["resolution"] == "spec_vs_drawing_disagreement" for c in app.conflicts)
    assert any(f["severity"] == "CRITICAL" for f in app.flags)


def test_global_through_all_enforced():
    ext = _extraction()
    ext["hole_callouts"][1]["thru"] = False
    cons = parse_spec_text_fallback(SPEC)
    app = apply_must_meet(ext, cons, part="X")
    assert all(h["thru"] for h in app.extraction["hole_callouts"])
    assert any(c["field"] == "thru" for c in app.conflicts)


# ── Constraint evaluation (shared by prevalidation + post-build) ────────────
def _holes(n=6):
    return ([{"x": p[0], "y": p[1], "diameter": HOLE_D, "through": True}
             for p in _positions(n)]
            + [{"x": CX, "y": CY, "diameter": BORE_D, "through": True},
               {"x": CX, "y": CY - CUT_OFF, "diameter": CUT_D, "through": True}])


def test_evaluate_constraints_all_pass():
    results = evaluate_constraints(_holes(), parse_spec_text_fallback(SPEC))
    assert [r["status"] for r in results] == ["PASS"] * 4


def test_evaluate_constraints_five_holes_fails_mm001_with_measured():
    results = evaluate_constraints(_holes(5), parse_spec_text_fallback(SPEC))
    r = next(r for r in results if r["id"] == "MM-001")
    assert r["status"] == "FAIL" and r["required"] == 6 and r["measured"] == 5


# ── Circular-pattern build plan + VBA contract ──────────────────────────────
def _package(tmp_path, spec=SPEC):
    from pipeline.macro_generator import generate_macro_package
    from pipeline.resolver import resolve_extraction
    from pipeline.validator import format_verification_report, run_verification

    ext = _extraction()
    cons = parse_spec_text_fallback(spec)
    app = apply_must_meet(ext, cons, part="A050211E")
    res = resolve_extraction(app.extraction, requirements=[spec])
    model, report = run_verification(res.clean_extraction)
    pkg = generate_macro_package(model, ext, format_verification_report(model, report),
                                 tmp_path, resolution=res)
    return pkg, cons


def test_circular_pattern_trio_and_canonical_schema(tmp_path):
    from pipeline.macro_generator import CIRCULAR_PATTERN_REQUIRED

    pkg, _ = _package(tmp_path)
    types = [s.feature_type for s in pkg.steps]
    i_hole = types.index("hole", types.index("hole") + 1)  # seed (2nd hole step)
    assert types[i_hole + 1] == "reference_axis"
    assert types[i_hole + 2] == "circular_pattern"

    plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
    cp = next(s for s in plan["steps"] if s["type"] == "circular_pattern")
    spec = cp["circular_pattern"]
    for field in CIRCULAR_PATTERN_REQUIRED:
        assert spec.get(field) is not None, f"null canonical field {field}"
    assert spec["total_instances"] == 6  # INCLUDES the seed
    assert spec["seed_feature_name"] == "F003_SeedHoleCut"
    assert spec["pattern_axis"]["axis_name"] == "PatternAxis1"
    assert spec["bolt_circle_radius_in"] == pytest.approx(BOLT_R, abs=0.001)


def test_generated_vba_contract(tmp_path):
    pkg, _ = _package(tmp_path)
    pattern_vba = next(pkg.macros_dir / s.macro_file for s in pkg.steps
                       if s.feature_type == "circular_pattern")
    text = pattern_vba.read_text(encoding="utf-8")
    # Mark=1 axis / Mark=4 seed selection contract, version-pinned call, and
    # Nothing-checks live in the single shared helper.
    assert "CreateCircularPatternSafe" in text
    assert "False, 1, Nothing, 0" in text     # axis Mark=1
    assert "True, 4, Nothing, 0" in text      # seed Mark=4 (append)
    assert "FeatureCircularPattern5" in text
    assert "FeatureCircularPattern4" in text  # version fallback
    assert "If swFeat Is Nothing Then" in text
    assert "WriteMacroResult" in text
    assert "SendMsgToUser2" in text
    axis_vba = next(pkg.macros_dir / s.macro_file for s in pkg.steps
                    if s.feature_type == "reference_axis")
    atext = axis_vba.read_text(encoding="utf-8")
    assert "InsertAxis2" in atext and "PatternAxis1" in atext
    seed_vba = next(pkg.macros_dir / s.macro_file for s in pkg.steps
                    if "SeedHoleCut" in s.macro_file)
    stext = seed_vba.read_text(encoding="utf-8")
    assert "F003_SeedHoleCut" in stext
    assert "-> radius" in stext and " m" in stext  # inch->meter audit comment


def test_canonical_schema_refuses_nulls():
    from pipeline.macro_generator import MacroGenerationError, canonical_circular_pattern
    from pipeline.schema import DrawingData

    ext = _extraction()
    model = DrawingData.model_validate(ext)
    h = model.hole_callout_by_id("H003")  # no bolt_circle_diameter set
    feat = model.feature_by_id("F003")
    with pytest.raises(MacroGenerationError, match="refusing to emit"):
        canonical_circular_pattern(model, feat, h, "PatternAxis1", "test")


# ── CadQuery pre-validation (skipped when cadquery is unavailable) ──────────
cadquery = pytest.importorskip("cadquery")


def test_prevalidation_passes_and_writes_stl(tmp_path):
    from pipeline.cq_prevalidate import run_prevalidation

    pkg, cons = _package(tmp_path)
    report = run_prevalidation(pkg.build_plan_json, cons, pkg.root)
    assert report["ok"], report
    assert (pkg.root / "prevalidation.stl").is_file()
    assert [r["status"] for r in report["constraints"]] == ["PASS"] * 4
    assert report["solid"]["valid_watertight"]


def test_prevalidation_corrupted_spec_fails_mm001(tmp_path):
    from pipeline.cq_prevalidate import run_prevalidation

    pkg, cons = _package(tmp_path, spec=SPEC.replace("6 holes", "5 holes"))
    report = run_prevalidation(pkg.build_plan_json, cons, pkg.root)
    assert not report["ok"]
    assert any(f.startswith("MM-001 FAILED") for f in report["failed_constraints"])
    r = next(r for r in report["constraints"] if r["id"] == "MM-001")
    assert r["required"] == 5 and r["measured"] == 6
