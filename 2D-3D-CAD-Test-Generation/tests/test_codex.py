"""Tests for the Codex integration (Stage A validation + Stage B macro writing).

Everything runs in the deterministic OFFLINE STUB (no Codex CLI, no network), so
these pass on Mac/CI. They cover:
  * codex_client health/mode + robust JSON extraction;
  * Stage A verdicts — APPROVED, forced REJECTED, and hole-count >= HIGH;
  * Stage B overall-shape check — pass and fail (missing feature coverage);
  * the two halt-and-report paths through process_drawing_data:
      - a forced REJECTED validation halts BEFORE macro writing;
      - a forced CadQuery pre-validation failure halts BEFORE the SolidWorks build.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """Force the offline stub for every test; clear force hooks between tests."""
    monkeypatch.setenv("MTI_CODEX_STUB", "1")
    for k in ("MTI_CODEX_FORCE_VERDICT", "MTI_FORCE_PREVAL_FAIL", "MTI_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── codex_client ──────────────────────────────────────────────────────────────
def test_client_health_and_mode():
    from pipeline import codex_client
    h = codex_client.health().as_dict()
    assert h["mode"] == "stub"
    assert h["model"] == codex_client.CODEX_MODEL
    assert codex_client.active() is True          # stub forced → stages run
    # never raises
    assert isinstance(codex_client.is_installed(), bool)


def test_extract_json_strips_fences():
    from pipeline import codex_client
    assert codex_client.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert codex_client.extract_json('noise {"b": [1,2]} trailing') == {"b": [1, 2]}
    with pytest.raises(codex_client.CodexError):
        codex_client.extract_json("no json here")


# ── Stage A · validation ──────────────────────────────────────────────────────
def test_validation_approved_when_counts_agree():
    from pipeline import codex_validation
    resolved = {"part_number": "X1", "units": "in",
                "hole_callouts": [{"id": "H1", "qty": 6}], "dimensions": []}
    overview = {"global_notes": [{"note": "(6) HLS", "resolved_count": 6}]}
    v = codex_validation.validate_extraction(resolved, overview_analysis=overview)
    assert v["overall_status"] == "APPROVED"
    assert v["engine"] == "stub"


def test_validation_hole_count_mismatch_is_high_and_notes():
    from pipeline import codex_validation
    resolved = {"hole_callouts": [{"id": "H1", "qty": 5}], "dimensions": []}
    overview = {"global_notes": [{"note": "(6) HLS", "resolved_count": 6}]}   # A050211E class
    v = codex_validation.validate_extraction(resolved, overview_analysis=overview)
    disc = [d for d in v["discrepancies"] if "hole" in d["field"].lower()]
    assert disc and disc[0]["severity"] in ("HIGH", "CRITICAL")
    assert v["overall_status"] == "APPROVED_WITH_NOTES"


def test_validation_forced_rejected(monkeypatch):
    from pipeline import codex_validation
    monkeypatch.setenv("MTI_CODEX_FORCE_VERDICT", "REJECTED")
    v = codex_validation.validate_extraction({"hole_callouts": [], "dimensions": []})
    assert v["overall_status"] == "REJECTED"
    assert codex_validation.build_hints(v)  # non-empty re-run hints


def test_validation_writes_report_and_lessons(tmp_path):
    from pipeline import codex_validation
    resolved = {"hole_callouts": [{"id": "H1", "qty": 5}],
                "dimensions": [], "units": "in"}
    overview = {"global_notes": [{"note": "(6) HLS", "resolved_count": 6}]}
    codex_validation.validate_extraction(resolved, overview_analysis=overview,
                                         output_dir=tmp_path, drawing_id="D1")
    assert (tmp_path / "codex_validation.json").is_file()
    assert (tmp_path / "lessons_learned.jsonl").is_file()
    rec = (tmp_path / "lessons_learned.jsonl").read_text(encoding="utf-8")
    assert "codex_validation" in rec and "hole_count" in rec


# ── Stage B · overall shape check ─────────────────────────────────────────────
def test_shape_check_pass():
    from pipeline import codex_macros
    plan = {"steps": [{"feature_id": "F1", "type": "extrude_boss"}]}
    preval = {"solid": {"bbox_mm": [11 * 25.4, 6.5 * 25.4, 0.105 * 25.4]},
              "measured_holes_in": []}
    resolved = {"dimensions": [{"dimension_type": "linear", "value": 11.0},
                               {"dimension_type": "linear", "value": 6.5}]}
    manifest = {"feature_coverage": {"F1": "BUILT"}}
    res = codex_macros.overall_shape_check(plan, preval, resolved, None, manifest=manifest)
    assert res["passed"] is True


def test_shape_check_fails_on_missing_feature_coverage():
    from pipeline import codex_macros
    plan = {"steps": [{"feature_id": "F1"}, {"feature_id": "F2"}]}
    preval = {"solid": {"bbox_mm": [100, 50, 3]}, "measured_holes_in": []}
    resolved = {"dimensions": []}
    manifest = {"feature_coverage": {"F1": "BUILT"}}   # F2 missing → hard fail
    res = codex_macros.overall_shape_check(plan, preval, resolved, None, manifest=manifest)
    assert res["passed"] is False
    cov = [c for c in res["checks"] if c["check"] == "feature_coverage"][0]
    assert "F2" in cov["missing"]


# ── Integration · halt-and-report paths through process_drawing_data ──────────
def _load_sample():
    p = ROOT / "extraction_115C.json"
    if not p.is_file():
        pytest.skip("sample extraction not present")
    return json.loads(p.read_text(encoding="utf-8"))


def test_pipeline_halts_before_macros_on_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MTI_CODEX_FORCE_VERDICT", "REJECTED")
    from pipeline.batch import process_drawing_data
    row = process_drawing_data(_load_sample(), "codextest", tmp_path, sw_app=None)
    assert row.status == "BLOCKED"
    assert row.n_macros == 0
    # the verdict report was written and there are no .vba macros for this part
    part_dir = next(d for d in tmp_path.iterdir() if d.is_dir())
    assert (part_dir / "codex_validation.json").is_file()
    verdict = json.loads((part_dir / "codex_validation.json").read_text(encoding="utf-8"))
    assert verdict["overall_status"] == "REJECTED"
    assert not list((part_dir / "macros").glob("*.vba")) if (part_dir / "macros").exists() else True


def test_pipeline_halts_before_solidworks_on_cadquery_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MTI_CODEX_FORCE_VERDICT", "APPROVED")   # pass Stage A
    monkeypatch.setenv("MTI_FORCE_PREVAL_FAIL", "1")            # force CadQuery failure
    from pipeline.batch import process_drawing_data
    process_drawing_data(_load_sample(), "codextest", tmp_path, sw_app=None)
    out = capsys.readouterr().out
    assert "HALTING before the SolidWorks build" in out
    part_dir = next(d for d in tmp_path.iterdir() if d.is_dir())
    assert (part_dir / "codex_shape_check.json").is_file()
    report = json.loads((part_dir / "prevalidation_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
