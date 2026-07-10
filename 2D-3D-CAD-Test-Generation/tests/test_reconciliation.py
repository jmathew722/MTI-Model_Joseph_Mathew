"""Tests for pipeline.reconciliation — the Stage 10.5 self-correcting loop
(2026-07-10 audit).

Covers: the ground-truth checklist (built ONLY from the raw extraction), the
checklist-vs-disposition diff (justified skips accepted, unresolved exclusions
and instance-count shortfalls named), the capped re-resolution loop's two real
behaviors (nothing wrong -> zero extra passes; genuinely unresolvable -> stops
after one pass rather than looping uselessly, and reports every remaining item
by name), the exact report schema, the "never calls the extractor" cost
discipline, and the build_plan.json/macros splice mechanism.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.build_sequencer import STATE_BUILT, STATE_EXCLUDED, sequence_build_order
from pipeline.macro_generator import generate_macro_package
from pipeline.reconciliation import (
    ChecklistItem,
    UnresolvedItem,
    _splice_recovered_features,
    build_checklist,
    diff_checklist,
    reconcile_part,
)
from pipeline.resolver import resolve_extraction
from pipeline.validator import format_verification_report, run_verification


def bracket_drawing(units="inch") -> dict:
    """Base plate + a 4-hole pattern (qty=4, one callout) + a fillet with NO
    linked dimension at all (genuinely unrecoverable — no radius anywhere on
    the drawing) + a shell (prohibited, always a justified skip)."""
    return {
        "part_number": "RECON-1",
        "units": units,
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": units, "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 2.0, "unit": units, "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 0.5, "unit": units, "applies_to": "height"},
            {"id": "D004", "type": "diameter", "value": 0.25, "unit": units,
             "applies_to": "hole_diameter", "feature_ref": "F002"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "thru", "diameter": 0.25, "qty": 4,
             "pattern": "linear", "pattern_spacing": 1.0, "feature_ref": "F002"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
             "sketch_plane": "Top"},
            {"id": "F002", "type": "hole", "description": "Mounting holes",
             "related_dimensions": ["D004"]},
            {"id": "F003", "type": "fillet", "description": "Corner fillet",
             "related_dimensions": []},
            {"id": "F004", "type": "shell", "description": "Shell body",
             "related_dimensions": ["D003"]},
        ],
        "build_order": ["F001", "F002", "F003", "F004"],
    }


def _resolved(data: dict):
    resolution = resolve_extraction(data)
    model, report = run_verification(resolution.clean_extraction)
    assert model is not None, report
    return resolution, model


# --------------------------------------------------------------------------- #
# build_checklist — ground truth from the RAW extraction only
# --------------------------------------------------------------------------- #
class TestBuildChecklist:
    def test_one_entry_per_feature(self):
        checklist = build_checklist(bracket_drawing())
        assert {c.feature_id for c in checklist} == {"F001", "F002", "F003", "F004"}

    def test_hole_expected_instances_from_qty(self):
        checklist = build_checklist(bracket_drawing())
        f002 = next(c for c in checklist if c.feature_id == "F002")
        assert f002.expected_instances == 4

    def test_non_hole_feature_expects_one_instance(self):
        checklist = build_checklist(bracket_drawing())
        f001 = next(c for c in checklist if c.feature_id == "F001")
        assert f001.expected_instances == 1

    def test_instance_positions_take_precedence_over_qty(self):
        data = bracket_drawing()
        data["hole_callouts"][0]["instance_positions"] = [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]
        data["hole_callouts"][0]["qty"] = 4  # drawing's own qty undercounts vs. explicit positions
        checklist = build_checklist(data)
        f002 = next(c for c in checklist if c.feature_id == "F002")
        assert f002.expected_instances == 5

    def test_never_reads_resolved_or_build_plan(self):
        # build_checklist takes only the raw dict — passing a dict missing the
        # keys resolved_extraction/build_plan would carry proves nothing else
        # is consulted.
        raw = {"features": [{"id": "F001", "type": "extrude_boss", "description": "x"}],
               "hole_callouts": []}
        checklist = build_checklist(raw)
        assert len(checklist) == 1 and checklist[0].expected_instances == 1


# --------------------------------------------------------------------------- #
# diff_checklist — the actual reconciliation logic
# --------------------------------------------------------------------------- #
class TestDiffChecklist:
    def test_fully_built_part_has_no_unresolved_items(self, tmp_path):
        resolution, model = _resolved(bracket_drawing())
        seq = sequence_build_order(model, resolution)
        pkg = generate_macro_package(model, bracket_drawing(),
                                     format_verification_report(model, run_verification(bracket_drawing())[1]),
                                     tmp_path, resolution=resolution)
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
        checklist = build_checklist(bracket_drawing())
        unresolved = diff_checklist(checklist, seq.disposition_table, plan)
        # F003 (fillet, no radius anywhere) is excluded but genuinely unresolvable;
        # it should still surface (not justified — it's not in skipped_prohibited,
        # only F004/shell is), so we expect exactly F003 here.
        assert {u.feature_id for u in unresolved} == {"F003"}

    def test_justified_prohibited_skip_is_not_flagged(self):
        resolution, model = _resolved(bracket_drawing())
        seq = sequence_build_order(model, resolution)
        disp = seq.disposition_table
        plan = {"steps": [], "skipped_prohibited": ["F004"]}
        # F004 is EXCLUDED in disposition terms only if it were — but shell is
        # actually BUILT (manual macro) in the disposition model; simulate the
        # justified-skip path directly: force F004's state to EXCLUDED and
        # confirm skipped_prohibited suppresses the flag.
        for d in disp:
            if d["feature_id"] == "F004":
                d["state"] = STATE_EXCLUDED
        checklist = [c for c in build_checklist(bracket_drawing()) if c.feature_id == "F004"]
        unresolved = diff_checklist(checklist, disp, plan)
        assert unresolved == []

    def test_unjustified_exclusion_is_flagged_with_reason(self):
        checklist = [ChecklistItem("F099", "hole", "test hole", 1)]
        disp = [{"feature_id": "F099", "state": STATE_EXCLUDED,
                 "flags": [{"human_note": "EXCLUDED FROM BUILD — missing diameter."}]}]
        unresolved = diff_checklist(checklist, disp, {"steps": [], "skipped_prohibited": []})
        assert len(unresolved) == 1
        assert unresolved[0].feature_id == "F099"
        assert "missing diameter" in unresolved[0].issue

    def test_instance_count_shortfall_is_flagged(self):
        checklist = [ChecklistItem("F002", "hole", "pattern", expected_instances=6)]
        disp = [{"feature_id": "F002", "state": STATE_BUILT, "flags": []}]
        plan = {"steps": [{"feature_id": "F002", "positions_xy": [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]}]}
        unresolved = diff_checklist(checklist, disp, plan)
        assert len(unresolved) == 1
        assert "6 instance" in unresolved[0].issue and "5" in unresolved[0].issue

    def test_matching_instance_count_is_not_flagged(self):
        checklist = [ChecklistItem("F002", "hole", "pattern", expected_instances=4)]
        disp = [{"feature_id": "F002", "state": STATE_BUILT, "flags": []}]
        plan = {"steps": [{"feature_id": "F002", "positions_xy": [[0, 0], [1, 0], [2, 0], [3, 0]]}]}
        assert diff_checklist(checklist, disp, plan) == []

    def test_missing_disposition_entry_entirely_is_flagged(self):
        # A feature in the raw extraction with NO disposition entry at all
        # (should be structurally impossible per build_sequencer, but the audit
        # explicitly asks that this can never be silently true).
        checklist = [ChecklistItem("F077", "hole", "ghost feature", 1)]
        unresolved = diff_checklist(checklist, [], {"steps": [], "skipped_prohibited": []})
        assert len(unresolved) == 1
        assert "NO disposition entry" in unresolved[0].issue


# --------------------------------------------------------------------------- #
# reconcile_part — the capped, self-correcting loop
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_part(tmp_path):
    """A drawing with NO unresolvable features (drop the ungrounded fillet)."""
    data = bracket_drawing()
    data["features"] = [f for f in data["features"] if f["id"] != "F003"]
    data["build_order"] = [f for f in data["build_order"] if f != "F003"]
    resolution, model = _resolved(data)
    verification_text = format_verification_report(model, run_verification(data)[1])
    part_dir = tmp_path / "RECON-1"
    part_dir.mkdir()
    pkg = generate_macro_package(model, data, verification_text, tmp_path, resolution=resolution)
    # generate_macro_package writes into tmp_path/<safe-name>/ - use THAT as part_dir
    return data, resolution, model, pkg, verification_text


class TestReconcilePartCleanCase:
    def test_no_unresolved_items_costs_zero_passes(self, clean_part):
        data, resolution, model, pkg, verification_text = clean_part
        seq = sequence_build_order(model, resolution)
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
        with patch("pipeline.resolver.resolve_extraction") as mock_resolve:
            result = reconcile_part(
                raw_extraction=data, resolution=resolution, model=model,
                dispositions=seq.disposition_table, build_plan=plan,
                verification_text=verification_text, part_dir=pkg.root, part="RECON-1",
            )
            mock_resolve.assert_not_called()
        assert result.loop_passes_used == 0
        assert result.unresolved == []
        assert result.final_status == "READY"
        assert result.confirmed_built == result.checklist_total


class TestReconcilePartUnresolvableCase:
    def test_genuinely_unresolvable_stops_after_one_pass(self, tmp_path):
        data = bracket_drawing()  # includes F003, the ungrounded fillet
        resolution, model = _resolved(data)
        verification_text = format_verification_report(model, run_verification(data)[1])
        pkg = generate_macro_package(model, data, verification_text, tmp_path, resolution=resolution)
        seq = sequence_build_order(model, resolution)
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))

        result = reconcile_part(
            raw_extraction=data, resolution=resolution, model=model,
            dispositions=seq.disposition_table, build_plan=plan,
            verification_text=verification_text, part_dir=pkg.root, part="RECON-1",
        )
        # Re-running resolve_extraction on the IDENTICAL raw dict with no new
        # signal is deterministic -> the loop must detect zero progress and
        # stop after exactly 1 pass, never burning the full cap of 3.
        assert result.loop_passes_used == 1
        assert result.final_status == "READY_WITH_OPEN_ITEMS"
        assert len(result.unresolved) == 1
        assert result.unresolved[0].feature_id == "F003"
        assert result.unresolved[0].status == "unresolved_after_pass_1"
        assert "no further information" in result.unresolved[0].resolution_attempted.lower()

    def test_never_calls_the_extractor(self, tmp_path):
        data = bracket_drawing()
        resolution, model = _resolved(data)
        verification_text = format_verification_report(model, run_verification(data)[1])
        pkg = generate_macro_package(model, data, verification_text, tmp_path, resolution=resolution)
        seq = sequence_build_order(model, resolution)
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))

        with patch("pipeline.extractor.extract_drawing_data", side_effect=AssertionError(
                "reconciliation must never call the extractor (paid API call)")):
            result = reconcile_part(
                raw_extraction=data, resolution=resolution, model=model,
                dispositions=seq.disposition_table, build_plan=plan,
                verification_text=verification_text, part_dir=pkg.root, part="RECON-1",
                max_passes=3,
            )
        # No exception means extract_drawing_data was never invoked (cost discipline).
        assert result.final_status == "READY_WITH_OPEN_ITEMS"

    def test_report_schema(self, tmp_path):
        data = bracket_drawing()
        resolution, model = _resolved(data)
        verification_text = format_verification_report(model, run_verification(data)[1])
        pkg = generate_macro_package(model, data, verification_text, tmp_path, resolution=resolution)
        seq = sequence_build_order(model, resolution)
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))

        result = reconcile_part(
            raw_extraction=data, resolution=resolution, model=model,
            dispositions=seq.disposition_table, build_plan=plan,
            verification_text=verification_text, part_dir=pkg.root, part="RECON-1",
        )
        path = result.write(pkg.root, "RECON-1")
        report = json.loads(path.read_text(encoding="utf-8"))
        assert set(report.keys()) == {
            "part", "checklist_total", "confirmed_built", "loop_passes_used",
            "unresolved", "splices_applied", "final_status",
        }
        assert report["part"] == "RECON-1"
        assert report["final_status"] in ("READY", "READY_WITH_OPEN_ITEMS")
        for item in report["unresolved"]:
            assert set(item.keys()) == {
                "feature_id", "feature_type", "issue", "resolution_attempted", "status"}


# --------------------------------------------------------------------------- #
# The splice mechanism (tested directly, independent of WHEN it's triggered)
# --------------------------------------------------------------------------- #
class TestSpliceRecoveredFeatures:
    def test_splice_adds_macro_and_patches_build_plan(self, tmp_path):
        data = bracket_drawing()
        data["features"] = [f for f in data["features"] if f["id"] != "F003"]
        data["build_order"] = [f for f in data["build_order"] if f != "F003"]
        resolution, model = _resolved(data)
        verification_text = format_verification_report(model, run_verification(data)[1])
        pkg = generate_macro_package(model, data, verification_text, tmp_path, resolution=resolution)
        part_dir = pkg.root

        plan_path = part_dir / f"{part_dir.name}_build_plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        # Simulate "F002 was previously excluded and is now recovered": remove
        # its step and mark it skipped, matching the state reconcile_part would
        # see BEFORE a successful splice.
        plan["steps"] = [s for s in plan["steps"] if s.get("feature_id") != "F002"]
        plan["skipped_prohibited"] = ["F002"]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        _splice_recovered_features(
            model=model, resolution=resolution, raw_extraction=data,
            verification_text=verification_text, part_dir=part_dir,
            feature_ids=["F002"], pass_num=1,
        )

        patched = json.loads(plan_path.read_text(encoding="utf-8"))
        assert "F002" not in patched["skipped_prohibited"]
        assert any(s.get("feature_id") == "F002" for s in patched["steps"])
        recon_macros = list((part_dir / "macros").glob("RECONCILE_pass1_*.vba"))
        assert recon_macros, "expected a new RECONCILE_pass1_*.vba macro file"
        # Existing macros must be untouched (no renumbering/overwriting).
        assert (part_dir / "macros" / "00_setup.vba").exists()
