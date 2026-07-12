"""Phantom-feature reconciliation + commit-mode type coverage (2026-07-12,
Task 3) — 158-C F004.

F004 is a "pattern" feature (parent_feature=F003, quantity=6) synthesized
alongside F003, the real hole feature that already builds all 6 instances from
its own callout (H001, qty=6, "6-HLS"). The drawing's hole accounting is fully
satisfied by F003 — F004 corresponds to nothing new. It was EXCLUDED and left
the part READY_WITH_OPEN_ITEMS permanently. Fix: callout-arithmetic
reconciliation reclassifies it as a duplicate BEFORE the completeness gate ever
considers excluding it, and the commit-mode ladder is extended so no feature
TYPE (fillet/chamfer/pattern included) can reach EXCLUDED_INCOMPLETE any more.
"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.build_sequencer import STATE_PHANTOM_RECLASSIFIED, sequence_build_order
from pipeline.macro_generator import generate_macro_package
from pipeline.reconciliation import build_checklist, diff_checklist, reconcile_part
from pipeline.resolver import _pattern_covered_by_parent, resolve_extraction
from pipeline.schema import DrawingData
from pipeline.validator import format_verification_report, run_verification

FIX = Path(__file__).resolve().parent / "fixtures" / "commit_mode"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _build(raw):
    res = resolve_extraction(raw)
    model, rep = run_verification(res.clean_extraction)
    tmp = Path(tempfile.mkdtemp())
    pkg = generate_macro_package(model, raw, format_verification_report(model, rep),
                                 tmp, resolution=res)
    disp = json.loads((pkg.root / f"{pkg.root.name}_build_dispositions.json").read_text())
    plan = json.loads(pkg.build_plan_json.read_text())
    return res, model, pkg, disp, plan


@pytest.fixture(scope="module")
def c158():
    return _load("158-C_extraction.json")


# --------------------------------------------------------------------------- #
# The 158-C F004 evidence case
# --------------------------------------------------------------------------- #
class TestF004PhantomDuplicate:
    def test_f004_reclassified_not_excluded(self, c158):
        res, _model, _pkg, disp, _plan = _build(c158)
        f004 = next(d for d in disp if d["feature_id"] == "F004")
        assert f004["state"] == STATE_PHANTOM_RECLASSIFIED
        assert f004["state"] != "EXCLUDED_INCOMPLETE"

    def test_f004_flag_names_f003_as_the_owner(self, c158):
        res, _m, _p, _d, _plan = _build(c158)
        flag = next(f for f in res.flags if f.get("source") == "phantom_duplicate")
        assert flag["duplicate_of"] == "F003"
        assert flag["feature_id"] == "F004"
        assert flag["flag_tier"] == "LOW"  # informational, never gates

    def test_pattern_covered_by_parent_detects_it(self, c158):
        model = DrawingData.model_validate(c158)
        f004 = next(f for f in c158["features"] if f["id"] == "F004")
        covered = _pattern_covered_by_parent(f004, c158, model)
        assert covered == ("F003", 6)

    def test_no_terminal_open_item_end_to_end(self, c158):
        res, model, pkg, disp, plan = _build(c158)
        rr = reconcile_part(raw_extraction=c158, resolution=res, model=model,
                            dispositions=disp, build_plan=plan,
                            verification_text="", part_dir=pkg.root, part="158-C")
        assert rr.final_status == "READY"
        assert rr.unresolved == []
        assert rr.checklist_total == 5
        assert rr.confirmed_built == 4
        assert len(rr.phantom_reclassified) == 1
        assert rr.phantom_reclassified[0]["feature_id"] == "F004"
        assert rr.phantom_reclassified[0]["duplicate_of"] == "F003"
        # explicit accounting: built + phantom-reclassified == checklist total
        assert rr.confirmed_built + len(rr.phantom_reclassified) == rr.checklist_total


# --------------------------------------------------------------------------- #
# diff_checklist represents phantom reclassification explicitly (never a miss)
# --------------------------------------------------------------------------- #
class TestDiffChecklistPhantomAccounting:
    def test_phantom_state_is_not_unresolved(self):
        checklist = build_checklist({"features": [
            {"id": "F004", "type": "pattern", "description": "dup"}]})
        disp = [{"feature_id": "F004", "state": STATE_PHANTOM_RECLASSIFIED,
                "flags": [{"human_note": "duplicate of F003", "duplicate_of": "F003"}]}]
        phantom_out = []
        unresolved = diff_checklist(checklist, disp, {}, phantom_out=phantom_out)
        assert unresolved == []
        assert len(phantom_out) == 1
        assert phantom_out[0]["duplicate_of"] == "F003"

    def test_phantom_accounting_optional(self):
        # phantom_out is optional — omitting it must not error, item is still
        # excluded from unresolved.
        checklist = build_checklist({"features": [
            {"id": "F004", "type": "pattern", "description": "dup"}]})
        disp = [{"feature_id": "F004", "state": STATE_PHANTOM_RECLASSIFIED, "flags": []}]
        assert diff_checklist(checklist, disp, {}) == []


# --------------------------------------------------------------------------- #
# Defense-in-depth: a BOM/balloon/applied-item note synthesized as a feature
# anyway (extraction slip-up) is caught by description text, not just parent-linkage
# --------------------------------------------------------------------------- #
class TestMetadataOnlyBackstop:
    def test_weatherstrip_note_reclassified_not_excluded(self):
        raw = {
            "part_number": "P2", "units": "inch", "confidence": 0.9,
            "dimensions": [{"id": "D1", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
                          {"id": "D2", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
                          {"id": "D3", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"}],
            "features": [
                {"id": "F001", "type": "extrude_boss", "description": "plate",
                 "related_dimensions": ["D1", "D2"], "depth_dimension_id": "D3"},
                {"id": "F002", "type": "pattern", "description":
                 "Weatherstrip, sponge rubber, applied per BOM item 2", "related_dimensions": []},
            ],
            "build_order": ["F001", "F002"],
        }
        res = resolve_extraction(raw)
        assert "F002" not in res.resolved_extraction["build_order"]  # removed — nothing to draw
        flag = next(f for f in res.flags if f.get("source") == "phantom_duplicate"
                    and f["feature_id"] == "F002")
        assert flag["flag_tier"] == "LOW"
        excluded = [f for f in res.flags if f.get("feature_id") == "F002"
                    and f.get("excluded_from_build")]
        assert not excluded


# --------------------------------------------------------------------------- #
# Non-duplicate pattern: no covering parent -> falls to the commit ladder
# --------------------------------------------------------------------------- #
class TestNonDuplicatePatternCommits:
    def test_pattern_with_no_parent_commits_not_excluded(self):
        raw = {
            "part_number": "P", "units": "inch", "confidence": 0.9,
            "dimensions": [{"id": "D1", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
                          {"id": "D2", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
                          {"id": "D3", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"}],
            "features": [
                {"id": "F001", "type": "extrude_boss", "description": "plate",
                 "related_dimensions": ["D1", "D2"], "depth_dimension_id": "D3"},
                {"id": "F002", "type": "pattern", "description": "orphan pattern",
                 "related_dimensions": [], "quantity": 3},
            ],
            "build_order": ["F001", "F002"],
        }
        res = resolve_extraction(raw)
        assert "F002" in res.resolved_extraction["build_order"]
        fx = next(f for f in res.resolved_extraction["features"] if f["id"] == "F002")
        committed = [d for d in res.resolved_extraction["dimensions"]
                    if d["id"] in fx["related_dimensions"]
                    and d.get("assumption_basis") == "committed_conservative"]
        assert committed


# --------------------------------------------------------------------------- #
# Commit-mode coverage sweep: parametrized, one fixture per feature type
# --------------------------------------------------------------------------- #
def _plate(extra_feature, extra_dims=None):
    return {
        "part_number": "SWEEP", "units": "inch", "confidence": 0.9,
        "dimensions": [
            {"id": "D1", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D2", "type": "linear", "value": 3.0, "unit": "inch", "applies_to": "width"},
            {"id": "D3", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"},
        ] + (extra_dims or []),
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "plate",
             "related_dimensions": ["D1", "D2"], "depth_dimension_id": "D3"},
            extra_feature,
        ],
        "build_order": ["F001", "FX"],
    }


TYPE_SWEEP_CASES = [
    ({"id": "FX", "type": "hole", "description": "hole", "parent_feature": "F001",
      "related_dimensions": [], "position_known": True}, "hole"),
    ({"id": "FX", "type": "extrude_cut", "description": "cut", "parent_feature": "F001",
      "related_dimensions": [], "position_known": True}, "extrude_cut"),
    ({"id": "FX", "type": "fillet", "description": "fillet", "parent_feature": "F001",
      "related_dimensions": []}, "fillet"),
    ({"id": "FX", "type": "chamfer", "description": "chamfer", "parent_feature": "F001",
      "related_dimensions": []}, "chamfer"),
    ({"id": "FX", "type": "pattern", "description": "pattern", "parent_feature": "F001",
      "related_dimensions": [], "quantity": 3}, "pattern"),
    ({"id": "FX", "type": "linear_pattern", "description": "linear pattern", "parent_feature": "F001",
      "related_dimensions": [], "quantity": 4}, "linear_pattern"),
    ({"id": "FX", "type": "circular_pattern", "description": "circular pattern", "parent_feature": "F001",
      "related_dimensions": [], "quantity": 4}, "circular_pattern"),
]


@pytest.mark.parametrize("feat,label", TYPE_SWEEP_CASES, ids=[c[1] for c in TYPE_SWEEP_CASES])
def test_commit_mode_never_excludes_any_feature_type(feat, label):
    raw = _plate(feat)
    res = resolve_extraction(raw)
    assert "FX" in res.resolved_extraction["build_order"], (
        f"{label}: commit-mode must never leave a feature EXCLUDED_INCOMPLETE")
    excluded = [f for f in res.flags if f.get("dimension_id") == "FX" and f.get("excluded_from_build")]
    assert not excluded, f"{label}: unexpected exclusion flag {excluded}"


@pytest.mark.parametrize("feat,label", TYPE_SWEEP_CASES, ids=[c[1] for c in TYPE_SWEEP_CASES])
def test_commit_mode_sweep_disposition_never_excluded(feat, label):
    raw = _plate(feat)
    res = resolve_extraction(raw)
    model, rep = run_verification(res.clean_extraction)
    seq = sequence_build_order(model, res)
    fx_disp = next(d for d in seq.disposition_table if d["feature_id"] == "FX")
    assert fx_disp["state"] != "EXCLUDED_INCOMPLETE", f"{label}: {fx_disp['state']}"
