"""Regression tests for the reliability-hardening pass.

Covers four failure classes from docs/solidworks-macro-error-log.md:
  * E010 — verbose applies_to labels silently miss the canonical envelope/profile.
  * E004/E006 — banned/nonexistent APIs must never ship (static auditor).
  * E008 — a paid extraction must be persisted even when BLOCKED.
  * Phase-4 readiness scoring + optional hard-gate.
"""
import json
import re

import pytest

import main as cli
from pipeline.macro_audit import audit_package, audit_text
from pipeline.macro_generator import MacroGenerationError, generate_macro_package
from pipeline.schema import canonicalize_applies_to, is_envelope_label
from pipeline.validator import compute_readiness, format_verification_report, run_verification


# --------------------------------------------------------------------------- #
# E010 — applies_to canonicalization
# --------------------------------------------------------------------------- #
class TestCanonicalizeAppliesTo:
    @pytest.mark.parametrize("label,expected", [
        ("length", "length"),
        ("width (top view, overall horizontal)", "width"),
        ("width (front view, small feature)", "width"),
        ("thru hole diameter (4 places)", "hole_diameter"),
        ("counterbore depth (lower limit)", "cbore_depth"),
        ("counterbore diameter (upper limit)", "cbore_diameter"),
        ("drill diameter for counterbore holes", "hole_diameter"),
        ("drill depth for counterbore holes", "depth"),
        ("bolt hole pattern horizontal spacing (center-to-center)", "spacing"),
        ("fillet radius", "fillet_radius"),
        ("", ""),
        ("totally unrelated note", ""),
    ])
    def test_canonical_token(self, label, expected):
        assert canonicalize_applies_to(label) == expected

    def test_compound_labels_beat_plain_depth(self):
        # "counterbore depth" contains "depth"; it must NOT canonicalize to depth.
        assert canonicalize_applies_to("counterbore depth") == "cbore_depth"

    def test_envelope_excludes_feature_local_sizes(self):
        assert is_envelope_label("width (top view, overall horizontal)")
        assert not is_envelope_label("width (front view, small feature)")
        assert not is_envelope_label("height of section view feature")
        assert is_envelope_label("length")


def _verbose_bracket() -> dict:
    """A plate whose applies_to labels are verbose, view-qualified free text —
    exactly what the extractor emits in production (would fail before E010 fix)."""
    return {
        "part_number": "VERBOSE-1",
        "units": "inch",
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch",
             "applies_to": "length (top view, overall horizontal)"},
            {"id": "D002", "type": "linear", "value": 2.0, "unit": "inch",
             "applies_to": "width (top view, overall vertical)"},
            {"id": "D003", "type": "linear", "value": 1.0, "unit": "inch",
             "applies_to": "width (front view, small feature)"},  # decoy, not envelope
            {"id": "D004", "type": "depth", "value": 0.5, "unit": "inch",
             "applies_to": "plate thickness"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D004",
             "sketch_plane": "Top"},
        ],
        "build_order": ["F001"],
    }


class TestVerboseLabelsStillBuild:
    def test_base_plate_uses_overall_envelope_not_decoy(self, tmp_path):
        data = _verbose_bracket()
        model, report = run_verification(data)
        assert report.ok, str(report)
        pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
        base = next(p for p in pkg.macros_dir.glob("01_*.vba"))
        text = base.read_text(encoding="utf-8")
        # Rectangle drawn from the 4.0 x 2.0 OVERALL envelope, not the 1.0 decoy.
        assert "CreateCornerRectangle" in text
        assert "4" in text and "2" in text
        assert "1 * UNIT_FACTOR" not in text  # decoy width never used as a side


# --------------------------------------------------------------------------- #
# E004/E006 — static auditor
# --------------------------------------------------------------------------- #
class TestMacroAuditor:
    def test_flags_nonexistent_bounding_box_api(self):
        findings = audit_text("01_x.vba", "Option Explicit\nSub main()\n"
                              "x = swModel.GetModelBoundingBox()\nEnd Sub\n")
        assert any(f.rule_id == "E004" and f.severity == "error" for f in findings)

    def test_flags_unbalanced_sub(self):
        findings = audit_text("01_x.vba", "Option Explicit\nSub main()\n'no end\n")
        assert any("Unbalanced Sub" in f.message and f.severity == "error" for f in findings)

    def test_clean_macro_has_no_errors(self):
        text = ("Option Explicit\nSub main()\n  LogResult \"PASS\", \"01\", \"ok\"\nEnd Sub\n")
        findings = audit_text("01_x.vba", text)
        assert not [f for f in findings if f.severity == "error"]

    def test_real_package_passes_audit(self, tmp_path):
        data = _verbose_bracket()
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
        audit = audit_package(pkg.macros_dir)
        assert audit.ok, [f.message for f in audit.errors]
        assert (pkg.root / "VERBOSE-1_audit_report.json").exists()

    def test_generator_raises_when_audit_finds_error(self, tmp_path, monkeypatch):
        # Simulate a generator regression that emits a banned API.
        import pipeline.macro_generator as mg
        from pipeline.macro_audit import AuditReport, Finding

        def fake_audit(_dir):
            return AuditReport(findings=[Finding("error", "E004", "01_x.vba", "boom")])

        monkeypatch.setattr(mg, "audit_package", fake_audit)
        data = _verbose_bracket()
        model, report = run_verification(data)
        with pytest.raises(MacroGenerationError, match="static self-validation"):
            generate_macro_package(model, data, "report", tmp_path)


# --------------------------------------------------------------------------- #
# RUN_ALL.vba — one-click, in-order build
# --------------------------------------------------------------------------- #
class TestRunAllMacro:
    @pytest.fixture
    def run_all(self, tmp_path):
        data = _verbose_bracket()
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
        return (pkg.macros_dir / "RUN_ALL.vba").read_text(encoding="utf-8"), pkg

    def test_exists_and_passes_audit(self, run_all):
        text, pkg = run_all
        assert text
        assert audit_package(pkg.macros_dir).ok

    def test_shared_scaffolding_defined_once(self, run_all):
        text, _ = run_all
        assert text.count("Option Explicit") == 1
        assert len(re.findall(r"Const UNIT_FACTOR", text)) == 1
        # Helpers appear exactly once (not duplicated per step).
        assert text.count("Function VerifySolidBody(") == 1
        assert text.count("Function SelectRefPlane(") == 1
        assert text.count("Function FindPartTemplate(") == 1

    def test_main_runs_steps_in_order(self, run_all):
        text, _ = run_all
        main = re.search(r"^Sub main\(\).*?^End Sub", text, re.S | re.M).group(0)
        calls = [ln.strip() for ln in main.splitlines()
                 if ln.strip().startswith("Step")]
        assert calls[0] == "Step00_Setup"
        assert calls[-1] == "StepZZ_FinalVerify"
        assert any(c.startswith("Step01_") for c in calls)

    def test_blocks_balanced(self, run_all):
        text, _ = run_all
        assert len(re.findall(r"^\s*Sub\b", text, re.M)) == len(re.findall(r"^\s*End Sub\b", text, re.M))
        assert len(re.findall(r"^\s*Function\b", text, re.M)) == len(re.findall(r"^\s*End Function\b", text, re.M))


# --------------------------------------------------------------------------- #
# Phase-4 readiness
# --------------------------------------------------------------------------- #
class TestReadiness:
    def test_scores_present_and_bounded(self):
        model, report = run_verification(_verbose_bracket())
        r = compute_readiness(model, report)
        for k in ("geometry_completeness", "dimension_completeness", "consistency",
                  "feature_confidence", "macro_readiness"):
            assert 0.0 <= r[k] <= 1.0
        assert "Macro readiness" in format_verification_report(model, report)

    def test_env_threshold_blocks(self, monkeypatch):
        monkeypatch.setenv("MACRO_READINESS_THRESHOLD", "0.99")
        model, report = run_verification(_verbose_bracket())
        assert not report.ok
        assert any("readiness" in e.lower() for e in report.errors)

    def test_bad_threshold_is_ignored_not_crashing(self, monkeypatch):
        monkeypatch.setenv("MACRO_READINESS_THRESHOLD", "not-a-number")
        model, report = run_verification(_verbose_bracket())
        assert report.ok  # invalid threshold must not block a clean drawing


# --------------------------------------------------------------------------- #
# E008 — extraction always persisted
# --------------------------------------------------------------------------- #
class TestExtractionPersistence:
    def test_save_extraction_writes_named_file(self, tmp_path):
        path = cli._save_extraction(tmp_path, "117-C-RevB", {"part_number": "117-C"})
        assert path.exists()
        assert path.name == "117-C-RevB_extraction.json"
        assert json.loads(path.read_text(encoding="utf-8"))["part_number"] == "117-C"

    def test_save_extraction_handles_blank_name(self, tmp_path):
        path = cli._save_extraction(tmp_path, "", {"a": 1})
        assert path.exists()
        assert path.parent.name == "part"
