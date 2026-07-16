"""Tests for the Codex macro-writing integration (macro writing only — there is
no independent Codex validation/OCR stage).

Everything runs in the deterministic OFFLINE STUB (no Codex CLI, no network), so
these pass on Mac/CI. They cover:
  * codex_client health/mode + robust JSON extraction;
  * the overall-shape check — pass and fail (missing feature coverage);
  * Codex macro writing through the pipeline produces a manifest + shape check;
  * a forced CadQuery pre-validation failure halts BEFORE the SolidWorks build.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """Force the offline stub for every test; clear hooks between tests."""
    monkeypatch.setenv("MTI_CODEX_STUB", "1")
    for k in ("MTI_FORCE_PREVAL_FAIL", "MTI_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── codex_client ──────────────────────────────────────────────────────────────
def test_client_health_and_mode():
    from pipeline import codex_client
    h = codex_client.health().as_dict()
    assert h["mode"] == "stub"
    assert h["model"] == codex_client.CODEX_MODEL
    assert codex_client.active() is True          # stub forced → macro stage runs
    assert isinstance(codex_client.is_installed(), bool)


def test_extract_json_strips_fences():
    from pipeline import codex_client
    assert codex_client.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert codex_client.extract_json('noise {"b": [1,2]} trailing') == {"b": [1, 2]}
    with pytest.raises(codex_client.CodexError):
        codex_client.extract_json("no json here")


# ── overall shape check ───────────────────────────────────────────────────────
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


# ── Integration through process_drawing_data ─────────────────────────────────
def _load_sample():
    p = ROOT / "extraction_115C.json"
    if not p.is_file():
        pytest.skip("sample extraction not present")
    return json.loads(p.read_text(encoding="utf-8"))


def test_pipeline_writes_codex_macro_manifest(tmp_path):
    from pipeline.batch import process_drawing_data
    row = process_drawing_data(_load_sample(), "codextest", tmp_path, sw_app=None)
    assert row.status in ("READY", "NOT READY")
    part_dir = next(d for d in tmp_path.iterdir() if d.is_dir())
    manifest = part_dir / "macros" / "codex_manifest.json"
    assert manifest.is_file()
    mf = json.loads(manifest.read_text(encoding="utf-8"))
    assert mf["engine"] == "fallback"           # offline stub keeps deterministic macros
    assert (part_dir / "codex_shape_check.json").is_file()
    # no independent validation artifact is produced anymore
    assert not (part_dir / "codex_validation.json").exists()


def test_pipeline_halts_before_solidworks_on_cadquery_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MTI_FORCE_PREVAL_FAIL", "1")            # force CadQuery failure
    from pipeline.batch import process_drawing_data
    process_drawing_data(_load_sample(), "codextest", tmp_path, sw_app=None)
    out = capsys.readouterr().out
    assert "HALTING before the SolidWorks build" in out
    part_dir = next(d for d in tmp_path.iterdir() if d.is_dir())
    assert (part_dir / "codex_shape_check.json").is_file()
    report = json.loads((part_dir / "prevalidation_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
