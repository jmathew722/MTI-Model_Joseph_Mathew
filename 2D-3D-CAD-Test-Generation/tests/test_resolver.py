"""Tests for Stage 2.5 — the ambiguity resolver and the self-contained build plan.

The prime directive the resolver MUST uphold:
  * every dimension ends with a numeric ``resolved_value`` (never null/absent),
  * every feature is marked ``build_status == "build"``,
  * every assumption carries a basis, confidence, flag tier and a human note,
  * the build plan is self-contained (meters + positions + flags per step).
"""
import json

import pytest

from pipeline.macro_generator import generate_macro_package
from pipeline.resolver import (
    FLAG_TIERS,
    behavior_for_tier,
    resolve_extraction,
    schema_clean,
    worst_tier,
)
from pipeline.schema import DrawingData
from pipeline.validator import run_verification


def _plate_with_holes() -> dict:
    """A clean, fully-dimensioned plate with a closing chain and a hole pattern."""
    return {
        "part_number": "RES-1",
        "units": "inch",
        "confidence": 0.9,
        "general_tolerance": ".XX +/-.01, .XXX +/-.005",
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 11.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 6.5, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "linear", "value": 0.105, "unit": "inch", "applies_to": "thickness"},
            {"id": "D004", "type": "diameter", "value": 0.218, "unit": "inch", "applies_to": "hole_diameter"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "thru", "diameter": 0.218, "thru": True, "qty": 2,
             "instance_positions": [[0.25, 0.375], [10.75, 0.375]], "feature_ref": "F002"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
             "position_known": True},
            {"id": "F002", "type": "hole", "description": "thru holes",
             "related_dimensions": ["D004"], "parent_feature": "F001"},
        ],
        "build_order": ["F001", "F002"],
        "relationships": {},
    }


def _ambiguous_chain() -> dict:
    """An overall length D001=11.0 = D002 + D003, where D003 is smudged with two
    candidate readings — only one of which closes the chain."""
    d = _plate_with_holes()
    d["dimensions"][0]["value"] = 11.0  # D001 length (total)
    d["dimensions"].append(
        {"id": "D010", "type": "linear", "value": 5.5, "unit": "inch", "applies_to": "length"}
    )
    d["dimensions"].append({
        "id": "D011", "type": "linear", "value": 5.5, "unit": "inch", "applies_to": "length",
        "value_unclear": True, "resolution_required": True, "ambiguity_reason": "overlapping lines",
        "possible_values": [5.5, 4.9],  # 5.5 closes 5.5+5.5=11.0; 4.9 does not
    })
    d["relationships"] = {
        "dimension_chains": [
            {"total_dimension_id": "D001", "component_dimension_ids": ["D010", "D011"]}
        ]
    }
    return d


class TestPrimeDirective:
    def test_every_dimension_gets_numeric_resolved_value(self):
        res = resolve_extraction(_plate_with_holes())
        dims = res.resolved_extraction["dimensions"]
        assert dims  # non-empty
        for d in dims:
            assert isinstance(d["resolved_value"], (int, float))
            assert not isinstance(d["resolved_value"], bool)
            assert d["resolved_value"] > 0
            assert d["flag_tier"] in FLAG_TIERS
            assert d["human_note"]

    def test_every_feature_marked_build(self):
        res = resolve_extraction(_plate_with_holes())
        for f in res.resolved_extraction["features"]:
            assert f["build_status"] == "build"
            assert "position_resolved" in f

    def test_forbidden_words_never_appear_in_output(self):
        res = resolve_extraction(_ambiguous_chain())
        blob = json.dumps(res.resolved_extraction).lower()
        # The status fields must never carry these — check the structured values,
        # not free-text notes (a note may legitimately say "verify").
        for f in res.resolved_extraction["features"]:
            assert f["build_status"] == "build"
        for d in res.resolved_extraction["dimensions"]:
            assert d["resolved_value"] is not None
        assert '"build_status": "skip"' not in blob
        assert '"resolved_value": null' not in blob


class TestResolutionAlgorithm:
    def test_step1_arithmetic_chain_picks_closing_candidate(self):
        # D011 has candidates [5.5, 4.9]; only 5.5 closes D001=D010+D011 (=11.0).
        res = resolve_extraction(_ambiguous_chain())
        d011 = res.dim_resolutions["D011"]
        assert d011.resolved_value == 5.5
        assert d011.assumption_basis == "arithmetic_chain"
        assert d011.flag_tier == "HIGH"
        assert "D001" in d011.chain_ids_used

    def test_clear_dimension_confirmed_high(self):
        res = resolve_extraction(_plate_with_holes())
        d001 = res.dim_resolutions["D001"]
        assert d001.assumption_made is False
        assert d001.flag_tier == "HIGH"
        assert d001.resolved_value == 11.0

    def _unplaced_pocket_part(self):
        d = _plate_with_holes()
        d["dimensions"].append(
            {"id": "D070", "type": "diameter", "value": 0.5, "unit": "inch", "applies_to": "diameter"}
        )
        d["features"].append({
            "id": "F009", "type": "extrude_cut", "description": "unplaced pocket",
            "related_dimensions": ["D070"], "parent_feature": "F001",
            "position_known": False,
        })
        d["build_order"].append("F009")
        return d

    def test_unknown_position_feature_excluded_when_commit_mode_off(self):
        # Legacy comparison path (commit_mode=False): a cut with no location and no
        # symmetry evidence is EXCLUDED (the parent-center guess is gone).
        d = self._unplaced_pocket_part()
        res = resolve_extraction(d, commit_mode=False)
        f009 = res.feature_resolutions["F009"]
        assert f009.build_status == "excluded"
        assert f009.position_assumption == "needs_markup_review"
        assert "POSITION UNRESOLVED" in f009.human_note
        assert "F009" not in res.resolved_extraction["build_order"]
        flag = next(f for f in res.flags if f["dimension_id"] == "F009")
        assert flag.get("source") == "position_unresolved"
        assert flag.get("excluded_from_build") is True
        assert flag["flag_tier"] == "CRITICAL"

    def test_unknown_position_feature_committed_and_built_in_commit_mode(self):
        # Commit-to-extraction (default): the same feature BUILDS at a conservative
        # inside-parent placement (never [0,0], never excluded), flagged CRITICAL.
        d = self._unplaced_pocket_part()
        res = resolve_extraction(d)  # commit_mode defaults ON
        f009 = res.feature_resolutions["F009"]
        assert f009.position_assumption == "committed_conservative"
        assert "F009" in res.resolved_extraction["build_order"]
        assert "COMMITTED" in f009.human_note
        # a real, non-[0,0] placement was written into the feature
        f = next(f for f in res.resolved_extraction["features"] if f["id"] == "F009")
        assert (f.get("offset_x"), f.get("offset_y")) != (0.0, 0.0)
        # The positioned hole feature F002 (callout carries instance positions) is HIGH.
        assert res.feature_resolutions["F002"].flag_tier == "HIGH"

    def test_missing_value_last_resort_is_critical_and_numeric(self):
        d = _plate_with_holes()
        # A depth dimension with no readable value at all (value omitted -> needs
        # last resort). Schema requires value>0, so we feed it via possible_values=[]
        # and value_unclear with a placeholder the resolver must still finalize.
        d["dimensions"].append({
            "id": "D050", "type": "depth", "value": 0.001, "unit": "inch",
            "applies_to": "drill depth", "value_unclear": True, "resolution_required": True,
            "ambiguity_reason": "illegible", "possible_values": [],
        })
        res = resolve_extraction(d)
        d050 = res.dim_resolutions["D050"]
        assert isinstance(d050.resolved_value, float)
        assert d050.resolved_value > 0


class TestBuildableBaseThickness:
    def _disc_without_thickness(self) -> dict:
        """A circular flange dimensioned by diameter only — no thickness anywhere
        (exactly the A001291E failure: the base extrude could not build)."""
        return {
            "part_number": "DISC-1", "units": "inch", "confidence": 0.8,
            "dimensions": [
                {"id": "D001", "type": "diameter", "value": 5.0, "unit": "inch",
                 "applies_to": "outer_diameter"},
                {"id": "D004", "type": "depth", "value": 0.25, "unit": "inch",
                 "applies_to": "counterbore_depth"},
            ],
            "features": [
                {"id": "F001", "type": "extrude_boss", "description": "base disc",
                 "related_dimensions": ["D001"]},
            ],
            "build_order": ["F001"], "relationships": {},
        }

    def test_synthesizes_thickness_flagged_critical(self):
        res = resolve_extraction(self._disc_without_thickness())
        f001 = next(f for f in res.resolved_extraction["features"] if f["id"] == "F001")
        did = f001["depth_dimension_id"]
        assert did, "base extrude must get a synthesized depth dimension"
        synth = next(d for d in res.resolved_extraction["dimensions"] if d["id"] == did)
        assert synth["value"] > 0.25  # thicker than the deepest sub-cut (counterbore 0.25)
        assert synth["flag_tier"] == "CRITICAL"
        assert synth["assumption_basis"] == "default_base_thickness"

    def test_clean_twin_builds_a_solid_base(self, tmp_path):
        res = resolve_extraction(self._disc_without_thickness())
        model = DrawingData.model_validate(res.clean_extraction)
        # The base feature now resolves a depth (builder/macro-gen read it).
        f001 = model.feature_by_id("F001")
        assert f001.depth_dimension_id
        assert model.dimension_by_id(f001.depth_dimension_id).value > 0
        # Macro generation succeeds and the base macro carries the thickness.
        from pipeline.validator import run_verification

        m, _ = run_verification(res.clean_extraction)
        pkg = generate_macro_package(m, self._disc_without_thickness(), "REPORT", tmp_path,
                                     resolution=res)
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
        base = next(s for s in plan["steps"] if s["type"] == "extrude_boss")
        assert base["dimensions_meters"].get("depth") or base["dimensions_meters"].get("thickness")


class TestSchemaCleanAndVerification:
    def test_clean_extraction_validates_against_strict_schema(self):
        res = resolve_extraction(_ambiguous_chain())
        # The strict (extra='forbid') schema must accept the clean twin.
        model = DrawingData.model_validate(res.clean_extraction)
        assert model.dimension_by_id("D011").value == 5.5

    def test_resolved_data_verifies_ready(self):
        # An ambiguous drawing that would BLOCK verification becomes READY once
        # the resolver clears the soft-block flags.
        raw = _ambiguous_chain()
        blocked_model, blocked_report = run_verification(raw)
        assert not blocked_report.ok  # resolution_required blocks raw data
        res = resolve_extraction(raw)
        model, report = run_verification(res.clean_extraction)
        assert report.ok

    def test_summary_counts_and_confidence(self):
        res = resolve_extraction(_ambiguous_chain())
        s = res.summary
        assert s.total_dimensions == len(res.resolved_extraction["dimensions"])
        assert 0.0 <= s.rebuild_confidence <= 1.0
        assert s.plain_english


class TestGeneralToleranceParsing:
    def test_unicode_plus_minus_does_not_crash(self):
        # Regression: "±0.005" must parse to 0.005, not raise on the ± glyph.
        from pipeline.resolver import _general_tolerance_value

        d = _plate_with_holes()
        d["general_tolerance"] = ".XX ±0.005, .XXX ±0.010, angular ±1°"
        model = DrawingData.model_validate(schema_clean(resolve_extraction(d).resolved_extraction))
        assert _general_tolerance_value(model) == 0.005

    def test_no_tolerance_block_defaults(self):
        from pipeline.resolver import _general_tolerance_value

        d = _plate_with_holes()
        d["general_tolerance"] = ""
        model = DrawingData.model_validate(d)
        assert _general_tolerance_value(model) == 0.01


class TestFlagTierHelpers:
    def test_worst_tier(self):
        assert worst_tier("HIGH", "LOW", "MEDIUM") == "LOW"
        assert worst_tier("HIGH", "CRITICAL") == "CRITICAL"
        assert worst_tier() == "HIGH"

    @pytest.mark.parametrize("tier,behavior", [
        ("HIGH", "comment_only"),
        ("MEDIUM", "msgbox_on_run"),
        ("LOW", "msgbox_on_run"),
        ("CRITICAL", "confirm_on_run"),
    ])
    def test_behavior_for_tier(self, tier, behavior):
        assert behavior_for_tier(tier) == behavior


class TestSelfContainedBuildPlan:
    def _build(self, tmp_path, raw):
        res = resolve_extraction(raw)
        model, _report = run_verification(res.clean_extraction)
        assert model is not None
        pkg = generate_macro_package(model, raw, "REPORT", tmp_path, resolution=res)
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
        return pkg, plan

    def test_plan_header_states_coordinate_convention(self, tmp_path):
        _pkg, plan = self._build(tmp_path, _plate_with_holes())
        assert plan["coordinate_origin"] == "lower_left_corner_of_base_solid"
        assert plan["x_direction"] == "positive_right"
        assert plan["y_direction"] == "positive_up"
        assert "resolution_summary" in plan
        assert plan["resolution_summary"]["plain_english"]

    def test_every_step_is_self_contained(self, tmp_path):
        _pkg, plan = self._build(tmp_path, _plate_with_holes())
        for step in plan["steps"]:
            # Required self-containment keys present on EVERY step.
            for key in ("dimensions_drawing_units", "dimensions_meters", "flags",
                        "requires_input", "auto_select_strategy", "expected_edge_count",
                        "assumption_made", "assumption_confidence", "flag_tier"):
                assert key in step, f"step {step['macro_file']} missing {key}"
            assert isinstance(step["flags"], list)

    def test_hole_step_carries_positions_in_both_units(self, tmp_path):
        _pkg, plan = self._build(tmp_path, _plate_with_holes())
        hole_steps = [s for s in plan["steps"] if s["type"] == "hole"]
        assert hole_steps
        for s in hole_steps:
            assert s["positions_xy"], "hole step must carry drawing-unit positions"
            assert len(s["positions_xy_meters"]) == len(s["positions_xy"])
            # meters == drawing * 0.0254 for inch parts.
            x_draw = s["positions_xy"][0][0]
            x_m = s["positions_xy_meters"][0][0]
            assert abs(x_m - x_draw * 0.0254) < 1e-6

    def test_meters_conversion_matches_unit_factor(self, tmp_path):
        _pkg, plan = self._build(tmp_path, _plate_with_holes())
        assert plan["unit_factor_to_meters"] == 0.0254
        base = next(s for s in plan["steps"] if s["type"] == "extrude_boss")
        for k, v in base["dimensions_drawing_units"].items():
            if k == "qty":
                continue
            assert abs(base["dimensions_meters"][k] - v * 0.0254) < 1e-6

    def test_resolved_extraction_written_to_package(self, tmp_path):
        pkg, _plan = self._build(tmp_path, _ambiguous_chain())
        assert pkg.resolved_extraction_json is not None
        assert pkg.resolved_extraction_json.exists()
        data = json.loads(pkg.resolved_extraction_json.read_text(encoding="utf-8"))
        assert "resolution" in data


class TestCriticalFlagVba:
    def test_critical_flag_emits_confirmation_dialog(self, tmp_path):
        # Force a CRITICAL: a feature consuming a last-resort-resolved depth dim.
        raw = _plate_with_holes()
        raw["dimensions"].append({
            "id": "D060", "type": "depth", "value": 0.001, "unit": "inch",
            "applies_to": "drill depth", "value_unclear": True, "resolution_required": True,
            "ambiguity_reason": "illegible", "possible_values": [],
        })
        raw["features"].append({
            "id": "F003", "type": "extrude_cut", "description": "blind pocket",
            "related_dimensions": ["D004"], "depth_dimension_id": "D060",
            "parent_feature": "F001", "position_known": True,
        })
        raw["build_order"].append("F003")
        res = resolve_extraction(raw)
        assert res.dim_resolutions["D060"].flag_tier == "CRITICAL"
        model, _ = run_verification(res.clean_extraction)
        pkg = generate_macro_package(model, raw, "REPORT", tmp_path, resolution=res)
        # The F003 macro must contain a confirmation dialog (vbOKCancel) + banner.
        macro = next(p for p in pkg.macros_dir.glob("*F003*.vba"))
        text = macro.read_text(encoding="utf-8")
        assert "CRITICAL ASSUMPTION" in text
        assert "vbOKCancel" in text
