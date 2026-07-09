"""Tests for Stage 1.5 — Holistic Overview Analysis (cross-view relational pass).

What this stage must guarantee:
  * the output schema (overview_analysis.json) validates the documented shape;
  * Stage 2.5 consumes it as the tier-2 input: cross-view conflicts become
    flags with ``source='overview_analysis'`` and ``resolved_by_tier='tier2_overview'``;
  * the A050211E failure class — a callout count ("(6) HLS") that per-view
    extraction did not fully capture (5 holes) — surfaces as a CRITICAL flag
    with a populated recommendation, never a silently wrong 5-hole part;
  * every resolution/flag records WHICH tier resolved it (spec / per-view /
    overview) for traceability;
  * token usage logs as its own stage line (``stage_1_5_overview_analysis``);
  * the stage is purely additive: no API key / no analysis -> identical
    behavior to before.
"""
import json

import pytest

from pipeline.overview_analysis import (
    OVERVIEW_ANALYSIS_FILENAME,
    STAGE_TAG,
    OverviewAnalysis,
    analyze_overview,
    save_overview_analysis,
)
from pipeline.resolver import (
    TIER_OVERVIEW,
    TIER_PER_VIEW,
    TIER_SPEC,
    resolve_extraction,
    tier_for_basis,
)
from pipeline.schema import DrawingData
from pipeline.usage_log import record_run


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _a050211e_style_extraction(n_holes: int = 5) -> dict:
    """A flange extraction shaped like A050211E: round plate, center bore, and a
    bolt pattern the per-view pass captured with ``n_holes`` instances."""
    return {
        "part_number": "A050211E",
        "units": "inch",
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "diameter", "value": 8.0, "unit": "inch",
             "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 0.5, "unit": "inch",
             "applies_to": "thickness"},
            {"id": "D003", "type": "diameter", "value": 3.88, "unit": "inch",
             "applies_to": "hole_diameter"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "thru", "diameter": 0.406, "thru": True,
             "qty": n_holes, "pattern": "circular", "bolt_circle_diameter": 5.5,
             "feature_ref": "F003"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base round plate",
             "related_dimensions": ["D001"], "depth_dimension_id": "D002",
             "position_known": True},
            {"id": "F002", "type": "extrude_cut", "description": "center bore",
             "related_dimensions": ["D003"], "parent_feature": "F001",
             "position_known": True},
            {"id": "F003", "type": "hole", "description": "bolt holes",
             "parent_feature": "F001"},
        ],
        "build_order": ["F001", "F002", "F003"],
        "relationships": {},
    }


def _a050211e_overview_analysis() -> dict:
    """The documented overview_analysis.json example (5-vs-6 hole discrepancy)."""
    return {
        "part_number": "A050211E",
        "views_detected": [
            {"view_id": "front", "description": "circular flange face-on with bore and bolt pattern"},
            {"view_id": "side", "description": "profile view showing thickness"},
        ],
        "cross_view_correspondences": [
            {"feature": "center_bore", "seen_in": ["front", "side"],
             "relation": "3.880 DIA circle in front view corresponds to full-height "
                         "vertical hidden lines in side view; confirms through-hole, not blind",
             "confidence": "high"},
        ],
        "overall_shape_summary": "flat circular flange, .50 thick, with a through bore, "
                                 "a 6-hole bolt pattern, and a bottom tab with an "
                                 "additional through-hole",
        "global_notes": [
            {"note": "FINISH ALL OVER", "applies_to": "all_exterior_faces"},
            {"note": "(6) HLS", "applies_to": "bolt_hole_pattern", "resolved_count": 6},
        ],
        "cross_view_conflicts": [
            {"description": "front view shows 5 holes at visible resolution but "
                            "callout states 6 HLS",
             "views_involved": ["front"],
             "severity": "CRITICAL",
             "recommendation": "check for occluded hole behind title block or leader "
                               "line pointing to a 6th location not clearly rendered"},
        ],
        "symmetry": {"type": "none_detected",
                     "notes": "bolt pattern uses X/Y offset dimensioning, not polar - "
                              "verify before assuming rotational symmetry"},
    }


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
class TestSchema:
    def test_documented_example_validates(self):
        data = OverviewAnalysis.model_validate(_a050211e_overview_analysis())
        assert data.part_number == "A050211E"
        assert len(data.views_detected) == 2
        assert data.global_notes[1].resolved_count == 6
        assert data.cross_view_conflicts[0].severity == "CRITICAL"
        assert data.symmetry.type == "none_detected"

    def test_minimal_payload_validates_with_defaults(self):
        data = OverviewAnalysis.model_validate({})
        assert data.views_detected == []
        assert data.symmetry.type == "none_detected"

    def test_round_trips_through_json(self):
        raw = _a050211e_overview_analysis()
        dumped = OverviewAnalysis.model_validate(raw).model_dump(mode="json")
        assert dumped["overall_shape_summary"] == raw["overall_shape_summary"]
        assert dumped["cross_view_conflicts"][0]["recommendation"]

    def test_save_writes_overview_analysis_json(self, tmp_path):
        path = save_overview_analysis(tmp_path / "part", _a050211e_overview_analysis())
        assert path.name == OVERVIEW_ANALYSIS_FILENAME
        assert json.loads(path.read_text(encoding="utf-8"))["part_number"] == "A050211E"


# --------------------------------------------------------------------------- #
# Stage 2.5 integration — tier-2 flags
# --------------------------------------------------------------------------- #
class TestResolverIntegration:
    def test_a050211e_five_vs_six_surfaces_critical_from_overview(self):
        """The definition-of-done case: extraction captured 5 holes, the sheet
        says (6) HLS — must surface CRITICAL, sourced from the overview stage,
        with the recommendation text populated."""
        res = resolve_extraction(_a050211e_style_extraction(n_holes=5),
                                 overview_analysis=_a050211e_overview_analysis())
        ov_flags = [f for f in res.flags if f.get("source") == "overview_analysis"]
        assert ov_flags, "overview analysis contributed no flags"
        crit = [f for f in ov_flags if f["flag_tier"] == "CRITICAL"]
        assert crit, "5-vs-6 discrepancy did not surface as CRITICAL"
        assert all(f["resolved_by_tier"] == TIER_OVERVIEW for f in ov_flags)
        # The model-reported conflict carries its recommendation text.
        conflict = next(f for f in crit if f["dimension_id"].startswith("OV-0"))
        assert "occluded hole" in conflict["human_note"]
        # The deterministic count cross-check fires too (no callout group of 6).
        count = next(f for f in crit if f["dimension_id"] == "OV-COUNT")
        assert "6" in count["human_note"] and "5" in count["human_note"]
        # Summary reflects the criticals; rebuild confidence is capped.
        assert res.summary.critical_flags >= 2
        assert res.summary.rebuild_confidence <= 0.55

    def test_matching_count_contributes_no_count_flag(self):
        ov = _a050211e_overview_analysis()
        ov["cross_view_conflicts"] = []  # views agree
        res = resolve_extraction(_a050211e_style_extraction(n_holes=6),
                                 overview_analysis=ov)
        assert not [f for f in res.flags if f.get("dimension_id") == "OV-COUNT"]
        assert not [f for f in res.flags if f.get("source") == "overview_analysis"]

    def test_no_overview_analysis_changes_nothing(self):
        base = resolve_extraction(_a050211e_style_extraction())
        with_none = resolve_extraction(_a050211e_style_extraction(), overview_analysis=None)
        assert len(base.flags) == len(with_none.flags)
        assert "overview_analysis" not in with_none.resolved_extraction

    def test_overview_never_changes_tier1_extracted_values(self):
        """Priority order: per-view extraction (tier 1) owns dimension values;
        the overview (tier 2) may only add flags."""
        raw = _a050211e_style_extraction(n_holes=5)
        res = resolve_extraction(raw, overview_analysis=_a050211e_overview_analysis())
        assert res.resolved_extraction["hole_callouts"][0]["qty"] == 5
        for dim in res.resolved_extraction["dimensions"]:
            orig = next(d for d in raw["dimensions"] if d["id"] == dim["id"])
            assert dim["resolved_value"] == orig["value"]

    def test_resolved_extraction_records_overview_section(self):
        res = resolve_extraction(_a050211e_style_extraction(),
                                 overview_analysis=_a050211e_overview_analysis())
        section = res.resolved_extraction["overview_analysis"]
        assert section["overall_shape_summary"].startswith("flat circular flange")
        assert section["n_conflicts"] == 1
        assert section["flags_contributed"]

    def test_clean_extraction_stays_schema_valid(self):
        res = resolve_extraction(_a050211e_style_extraction(),
                                 overview_analysis=_a050211e_overview_analysis())
        assert "overview_analysis" not in res.clean_extraction
        DrawingData.model_validate(res.clean_extraction)  # extra='forbid' passes

    def test_non_critical_conflict_maps_to_medium(self):
        ov = _a050211e_overview_analysis()
        ov["global_notes"] = []
        ov["cross_view_conflicts"] = [{
            "description": "chamfer visible in detail view has no callout in front view",
            "views_involved": ["front", "detail_a"],
            "severity": "MEDIUM",
            "recommendation": "confirm the chamfer size from the detail view scale",
        }]
        res = resolve_extraction(_a050211e_style_extraction(n_holes=5),
                                 overview_analysis=ov)
        ov_flags = [f for f in res.flags if f.get("source") == "overview_analysis"]
        assert len(ov_flags) == 1 and ov_flags[0]["flag_tier"] == "MEDIUM"


class TestTierTraceability:
    def test_tier_for_basis_mapping(self):
        assert tier_for_basis("spec_driven") == TIER_SPEC
        assert tier_for_basis("overview_relationship") == TIER_OVERVIEW
        assert tier_for_basis("arithmetic_chain") == TIER_PER_VIEW
        assert tier_for_basis("explicit_callout") == TIER_PER_VIEW

    def test_every_dimension_records_resolved_by_tier(self):
        res = resolve_extraction(_a050211e_style_extraction())
        for dim in res.resolved_extraction["dimensions"]:
            assert dim["resolved_by_tier"] in (TIER_SPEC, TIER_PER_VIEW, TIER_OVERVIEW)

    def test_spec_driven_resolution_records_tier0(self):
        raw = _a050211e_style_extraction()
        raw["dimensions"].append({
            "id": "D010", "type": "diameter", "value": 0.4, "unit": "inch",
            "applies_to": "hole_diameter", "value_unclear": True,
            "ambiguity_reason": "smudged", "possible_values": [0.4, 0.46],
        })
        res = resolve_extraction(raw, requirements=["all bolt holes must be 0.406 dia"])
        d010 = next(d for d in res.resolved_extraction["dimensions"] if d["id"] == "D010")
        assert d010["resolved_by_tier"] == TIER_SPEC
        assert d010["assumption_basis"] == "spec_driven"

    def test_flags_carry_resolved_by_tier(self):
        raw = _a050211e_style_extraction()
        raw["hole_callouts"][0].pop("bolt_circle_diameter", None)
        res = resolve_extraction(raw, overview_analysis=_a050211e_overview_analysis())
        assert res.flags
        for f in res.flags:
            assert f["resolved_by_tier"] in (TIER_SPEC, TIER_PER_VIEW, TIER_OVERVIEW)


# --------------------------------------------------------------------------- #
# Engineering review
# --------------------------------------------------------------------------- #
class TestEngineeringReview:
    def test_overview_flags_land_in_review_items(self):
        from pipeline.engineering_review import build_review_items

        res = resolve_extraction(_a050211e_style_extraction(n_holes=5),
                                 overview_analysis=_a050211e_overview_analysis())
        items = build_review_items(resolution=res)
        ov_items = [i for i in items if i["source"] == "overview_analysis"]
        assert ov_items
        assert any(i["severity"] == "CRITICAL" for i in ov_items)
        assert items[0]["severity"] == "CRITICAL"  # sorted most urgent first


# --------------------------------------------------------------------------- #
# Token ledger — distinct stage line item
# --------------------------------------------------------------------------- #
class TestUsageLedger:
    def test_stage_tag_is_a_distinct_line_item(self, tmp_path):
        usage = {"input_tokens": 2000, "output_tokens": 900, "calls": 1}
        record_run(tmp_path, "A050211E", "claude-sonnet-5", usage)  # per-view extraction
        record_run(tmp_path, "A050211E", "claude-sonnet-5", usage, stage=STAGE_TAG)
        rows = [json.loads(l) for l in
                (tmp_path / "token_usage_log.jsonl").read_text().splitlines() if l.strip()]
        assert [r["stage"] for r in rows] == ["extraction", STAGE_TAG]
        assert STAGE_TAG in (tmp_path / "token_usage_log.txt").read_text()

    def test_legacy_rows_without_stage_still_render(self, tmp_path):
        legacy = {"timestamp": "2026-01-01 00:00:00", "part": "OLD", "model": "claude-sonnet-5",
                  "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                  "cache_write_tokens": 0, "api_calls": 1, "cache_hit": False,
                  "cost_usd": 0.0, "model_priced": True}
        (tmp_path / "token_usage_log.jsonl").write_text(json.dumps(legacy) + "\n")
        record_run(tmp_path, "NEW", "claude-sonnet-5", {"input_tokens": 1, "calls": 1})
        text = (tmp_path / "token_usage_log.txt").read_text()
        assert "OLD" in text and "NEW" in text


# --------------------------------------------------------------------------- #
# Graceful degradation — the stage can only ADD signal
# --------------------------------------------------------------------------- #
class TestGracefulDegradation:
    def test_no_api_key_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert analyze_overview("aGVsbG8=", cache_dir=tmp_path) is None

    def test_cache_hit_needs_no_key(self, monkeypatch, tmp_path):
        """A previously cached analysis is served even with no key (free re-runs)."""
        from pipeline.extractor import DEFAULT_MODEL
        from pipeline.overview_analysis import _cache_key

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("EXTRACTION_MODEL", raising=False)
        # Key on the SAME model analyze_overview will resolve (the default), so
        # the pre-seeded cache hits regardless of what the default model is.
        key = _cache_key("aGVsbG8=", DEFAULT_MODEL)
        (tmp_path / f"{key}.json").write_text(
            json.dumps(_a050211e_overview_analysis()), encoding="utf-8")
        usage: dict = {}
        out = analyze_overview("aGVsbG8=", cache_dir=tmp_path, usage_out=usage)
        assert out is not None and out["part_number"] == "A050211E"
        assert usage.get("cache_hits") == 1

    def test_process_drawing_data_persists_overview_json(self, tmp_path):
        """batch.process_drawing_data writes overview_analysis.json into the
        part folder and feeds the analysis into resolution."""
        from pipeline.batch import process_drawing_data

        row = process_drawing_data(
            _a050211e_style_extraction(n_holes=5), "A050211E", tmp_path,
            overview_analysis=_a050211e_overview_analysis(),
            skip_overview_check=True, skip_requirements_check=True,
        )
        part_dir = tmp_path / "A050211E"
        saved = json.loads((part_dir / OVERVIEW_ANALYSIS_FILENAME).read_text(encoding="utf-8"))
        assert saved["global_notes"][1]["resolved_count"] == 6
        resolved = json.loads(
            (part_dir / "A050211E_resolved_extraction.json").read_text(encoding="utf-8"))
        assert resolved["overview_analysis"]["n_conflicts"] == 1
        # Build plan carries the overview findings for the UI's Engineering Flags.
        plan = json.loads((part_dir / "A050211E_build_plan.json").read_text(encoding="utf-8"))
        review = plan.get("engineering_review", [])
        assert any(i.get("source") == "overview_analysis" and i.get("severity") == "CRITICAL"
                   for i in review)
        assert row.status in ("READY", "NOT READY")
