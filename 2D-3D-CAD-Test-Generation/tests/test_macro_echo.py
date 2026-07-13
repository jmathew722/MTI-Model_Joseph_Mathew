"""Stage-7 hardening (2026-07-12): macro echo check, template engine, and the
emission invariants (open-edge overshoot, falsy basis, label/payload agreement).

The headline is the macro echo check — a generation-time round-trip that parses
every emitted geometry literal back out of the VBA and proves it equals the
build-plan value for the SAME feature. The 158-C regression case is here: a
macro whose literals match a DIFFERENT feature's values must fail with a
cross-contamination error.
"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.macro_echo import (
    MacroEchoError, assert_macro_echo, check_macro_echo,
)
from pipeline.macro_template_engine import (
    TemplateFillError, fill, template_names,
)
from pipeline.macro_audit import audit_text
from pipeline.macro_generator import (
    MacroGenerationError, generate_macro_package,
)
from pipeline.slot_cut import EDGE_OVERSHOOT_EPS
from pipeline.resolver import resolve_extraction
from pipeline.validator import format_verification_report, run_verification

COMMIT_FIX = Path(__file__).resolve().parent / "fixtures" / "commit_mode"


def _plate_with_holes() -> dict:
    """A base plate + two distinctly-placed holes of different diameters — enough
    to make cross-contamination detectable (each feature's coords are unique)."""
    return {
        "part_number": "ECHO-1", "units": "inch", "confidence": 0.9,
        "dimensions": [
            {"id": "D1", "type": "linear", "value": 10.0, "unit": "inch", "applies_to": "length"},
            {"id": "D2", "type": "linear", "value": 6.0, "unit": "inch", "applies_to": "width"},
            {"id": "D3", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"},
            {"id": "D4", "type": "diameter", "value": 0.25, "unit": "inch",
             "applies_to": "hole_diameter", "feature_ref": "F002"},
            {"id": "D5", "type": "diameter", "value": 0.5, "unit": "inch",
             "applies_to": "hole_diameter", "feature_ref": "F003"},
        ],
        "hole_callouts": [
            {"id": "H1", "type": "thru", "diameter": 0.25, "qty": 1,
             "x_position": 1.0, "y_position": 1.0, "position_known": True, "feature_ref": "F002"},
            {"id": "H2", "type": "thru", "diameter": 0.5, "qty": 1,
             "x_position": 8.0, "y_position": 5.0, "position_known": True, "feature_ref": "F003"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Base plate",
             "related_dimensions": ["D1", "D2"], "depth_dimension_id": "D3"},
            {"id": "F002", "type": "hole", "description": "Small hole",
             "related_dimensions": ["D4"], "position_known": True, "offset_x": 1.0, "offset_y": 1.0},
            {"id": "F003", "type": "hole", "description": "Large hole",
             "related_dimensions": ["D5"], "position_known": True, "offset_x": 8.0, "offset_y": 5.0},
        ],
        "build_order": ["F001", "F002", "F003"],
    }


def _generate(data: dict, tmp_path: Path):
    model, report = run_verification(data)
    assert report.ok, str(report)
    pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
    return pkg


# --------------------------------------------------------------------------- #
# Echo check passes on clean generation (every part path)
# --------------------------------------------------------------------------- #
class TestEchoPassesClean:
    def test_plate_with_holes_round_trips(self, tmp_path):
        pkg = _generate(_plate_with_holes(), tmp_path)
        report = check_macro_echo(pkg)
        assert report.ok, [str(i) for i in report.issues]
        assert report.checked_literals >= 3  # base rect + 2 hole circles

    def test_158c_slot_and_holes_round_trip(self, tmp_path):
        data = json.loads((COMMIT_FIX / "158-C_extraction.json").read_text())
        res = resolve_extraction(data)
        model, report = run_verification(res.clean_extraction)
        pkg = generate_macro_package(model, data, format_verification_report(model, report),
                                     tmp_path, resolution=res)
        echo = check_macro_echo(pkg)
        assert echo.ok, [str(i) for i in echo.issues]


# --------------------------------------------------------------------------- #
# 158-C regression: a literal matching a DIFFERENT feature is cross-contamination
# --------------------------------------------------------------------------- #
class TestCrossContamination:
    def test_foreign_coordinates_in_a_macro_fail(self, tmp_path):
        pkg = _generate(_plate_with_holes(), tmp_path)
        # Doctor F002's macro so its hole circle carries F003's center (8, 5) —
        # exactly the 158-C "coordinates of another feature in the wrong macro"
        # class the echo check must catch.
        f002 = next(s for s in pkg.steps if s.feature_id == "F002")
        macro = pkg.macros_dir / f002.macro_file
        text = macro.read_text(encoding="utf-8")
        doctored = text.replace("1 * UNIT_FACTOR, 1 * UNIT_FACTOR", "8 * UNIT_FACTOR, 5 * UNIT_FACTOR")
        assert doctored != text, "expected to rewrite F002's hole center"
        macro.write_text(doctored, encoding="utf-8")

        report = check_macro_echo(pkg)
        assert not report.ok
        contam = [i for i in report.issues if i.kind == "cross_contamination"]
        assert contam, [str(i) for i in report.issues]
        assert any(i.feature_id == "F002" and "F003" in (i.detail or "") for i in contam)

    def test_assert_raises_with_named_detail(self, tmp_path):
        pkg = _generate(_plate_with_holes(), tmp_path)
        f003 = next(s for s in pkg.steps if s.feature_id == "F003")
        macro = pkg.macros_dir / f003.macro_file
        text = macro.read_text(encoding="utf-8")
        macro.write_text(text.replace("8 * UNIT_FACTOR, 5 * UNIT_FACTOR",
                                      "1 * UNIT_FACTOR, 1 * UNIT_FACTOR"), encoding="utf-8")
        with pytest.raises(MacroEchoError) as ei:
            assert_macro_echo(pkg)
        assert "F003" in str(ei.value)

    def test_orphan_literal_detected(self, tmp_path):
        pkg = _generate(_plate_with_holes(), tmp_path)
        f002 = next(s for s in pkg.steps if s.feature_id == "F002")
        macro = pkg.macros_dir / f002.macro_file
        text = macro.read_text(encoding="utf-8")
        # A coordinate belonging to NO feature -> orphan (not cross-contamination).
        macro.write_text(text.replace("1 * UNIT_FACTOR, 1 * UNIT_FACTOR",
                                      "3.333 * UNIT_FACTOR, 2.777 * UNIT_FACTOR"), encoding="utf-8")
        report = check_macro_echo(pkg)
        assert any(i.kind == "orphan_literal" for i in report.issues)


# --------------------------------------------------------------------------- #
# Template engine: single-record fill, strict both directions
# --------------------------------------------------------------------------- #
class TestTemplateEngine:
    def test_circle_fill_is_byte_faithful(self):
        out = fill("sketch_circle.vba.tmpl", {"CX": "1.5", "CY": "2.5", "DIA": "0.25"})
        assert "CreateCircleByRadius 1.5 * UNIT_FACTOR, 2.5 * UNIT_FACTOR, 0#, (0.25 / 2#) * UNIT_FACTOR" in out

    def test_missing_placeholder_raises(self):
        with pytest.raises(TemplateFillError) as ei:
            fill("sketch_circle.vba.tmpl", {"CX": "1", "CY": "2"})  # no DIA
        assert "DIA" in str(ei.value)

    def test_extra_key_raises(self):
        # An unused key means the record builder and template disagree — the
        # exact drift class that leaks another feature's field in.
        with pytest.raises(TemplateFillError) as ei:
            fill("sketch_circle.vba.tmpl", {"CX": "1", "CY": "2", "DIA": "3", "FOREIGN": "9"})
        assert "FOREIGN" in str(ei.value)

    def test_unknown_template_raises(self):
        with pytest.raises(TemplateFillError):
            fill("does_not_exist.vba.tmpl", {})

    def test_every_template_passes_static_audit(self):
        # Templates are audited ONCE (banned APIs / structure); generated output
        # inherits the audit.
        names = template_names()
        assert names, "expected template files under macro_templates/"
        for name in names:
            path = Path(__file__).resolve().parent.parent / "pipeline" / "macro_templates" / name
            findings = audit_text(name.replace(".tmpl", ""), path.read_text(encoding="utf-8"))
            errors = [f for f in findings if f.severity == "error"]
            assert not errors, f"{name}: {[f.message for f in errors]}"


# --------------------------------------------------------------------------- #
# Emission invariants (Task 4)
# --------------------------------------------------------------------------- #
class TestOpenEdgeOvershoot:
    def test_158c_notch_overshoots_top_edge(self, tmp_path):
        data = json.loads((COMMIT_FIX / "158-C_extraction.json").read_text())
        res = resolve_extraction(data)
        model, report = run_verification(res.clean_extraction)
        pkg = generate_macro_package(model, data, format_verification_report(model, report),
                                     tmp_path, resolution=res)
        plan = json.loads(pkg.build_plan_json.read_text())
        rect = next(s for s in plan["steps"] if s["type"] == "slot_rect_cut")
        ys = [c[1] for c in rect["sketch"]["corners_drawing_units"]]
        # top edge is 6.25; the open side crosses it (6.30), not terminates at it
        assert max(ys) == pytest.approx(6.25 + EDGE_OVERSHOOT_EPS)

    def test_no_overshoot_refuses_generation(self, tmp_path):
        # A slot whose corners terminate exactly at the depth (no overshoot) is
        # refused by the emission invariant.
        from pipeline import macro_generator as mg

        class _Step:
            feature_type = "slot_rect_cut"
            feature_id = "F002"
            dimensions = {"depth": 1.88}
            slot = {"open_edge": "top",
                    "corners_drawing_units": [[1.56, 4.37], [3.18, 4.37],
                                              [3.18, 6.25], [1.56, 6.25]]}  # span == depth

        class _Pkg:
            steps = [_Step()]

        with pytest.raises(MacroGenerationError) as ei:
            mg._assert_open_edge_overshoot(_Pkg())
        assert "OVERSHOOT" in str(ei.value).upper()


class TestLabelPayloadAgreement:
    def test_foreign_feature_id_in_description_refuses(self):
        from pipeline import macro_generator as mg

        class _Step:
            def __init__(self, fid, desc, parent=""):
                self.feature_id = fid
                self.description = desc
                self.parent_feature_id = parent

        class _Pkg:
            steps = [_Step("F001", "Base plate"),
                     _Step("F002", "Notch that secretly references F001 coords")]

        # F002's description names F001 (a real other feature) -> disagreement.
        with pytest.raises(MacroGenerationError) as ei:
            mg._assert_label_payload_agreement(_Pkg())
        assert "F002" in str(ei.value)

    def test_own_and_parent_ids_are_allowed(self):
        from pipeline import macro_generator as mg

        class _Step:
            def __init__(self, fid, desc, parent=""):
                self.feature_id = fid
                self.description = desc
                self.parent_feature_id = parent

        class _Pkg:
            steps = [_Step("F002", "Slot F002 rectangle"),
                     _Step("F002_fillets", "R0.25 on slot F002", parent="F002")]

        mg._assert_label_payload_agreement(_Pkg())  # no raise: own + parent refs OK


class TestFalsyBasisSweep:
    def test_blank_basis_assumption_reads_as_derived(self):
        # An assumption whose basis went missing must NOT read as directly-
        # extracted (BUILT); it is BUILT_WITH_DERIVED_VALUE with "unspecified_basis".
        from pipeline.build_sequencer import _derivation_source

        class _DR:
            assumption_made = True
            assumption_basis = "   "   # whitespace-only (falsy after strip)

        class _Res:
            dim_resolutions = {"D1": _DR()}
            feature_resolutions = {}

        class _Feat:
            related_dimensions = ["D1"]
            depth_dimension_id = ""
            id = "F1"

        assert _derivation_source(_Feat(), _Res()) == "unspecified_basis"
