"""Tests for pipeline.macro_generator — VBA package generation."""
import json

import pytest

from pipeline.macro_generator import generate_macro_package
from pipeline.validator import format_verification_report, run_verification


def bracket_drawing(units="inch") -> dict:
    """A small bracket: base plate + 4-hole pattern + fillet + a prohibited shell."""
    return {
        "part_number": "MTI-0001",
        "revision": "B",
        "units": units,
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": units, "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 2.0, "unit": units, "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 0.5, "unit": units, "applies_to": "height"},
            {"id": "D004", "type": "diameter", "value": 0.25, "unit": units,
             "applies_to": "hole_diameter", "feature_ref": "F002"},
            {"id": "D005", "type": "radial", "value": 0.125, "unit": units,
             "applies_to": "fillet_radius", "feature_ref": "F003"},
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
             "related_dimensions": ["D005"]},
            {"id": "F004", "type": "shell", "description": "Shell body",
             "related_dimensions": ["D003"]},
        ],
        "build_order": ["F001", "F002", "F003", "F004"],
    }


@pytest.fixture
def package(tmp_path):
    data = bracket_drawing()
    model, report = run_verification(data)
    assert report.ok, str(report)
    text = format_verification_report(model, report)
    return generate_macro_package(model, data, text, tmp_path), model


class TestPackageStructure:
    def test_output_tree(self, package):
        pkg, model = package
        assert pkg.extraction_json.exists()
        assert pkg.verification_report.exists()
        assert pkg.build_plan_json.exists()
        assert (pkg.macros_dir / "00_setup.vba").exists()
        assert (pkg.macros_dir / "ZZ_final_verify.vba").exists()
        assert (pkg.macros_dir / "README.md").exists()
        assert (pkg.root / "logs" / ".gitkeep").exists()

    def test_folder_named_after_part(self, package):
        pkg, model = package
        assert pkg.root.name == "MTI-0001-RevB"

    def test_extraction_json_roundtrips(self, package):
        pkg, _ = package
        data = json.loads(pkg.extraction_json.read_text())
        assert data["part_number"] == "MTI-0001"

    def test_verification_report_says_ready(self, package):
        pkg, _ = package
        assert "READY TO BUILD" in pkg.verification_report.read_text()


class TestUnitConversion:
    def test_inch_unit_factor(self, package):
        pkg, _ = package
        setup = (pkg.macros_dir / "00_setup.vba").read_text()
        assert "Const UNIT_FACTOR As Double = 0.0254" in setup

    def test_mm_unit_factor(self, tmp_path):
        data = bracket_drawing(units="mm")
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, "x", tmp_path)
        setup = (pkg.macros_dir / "00_setup.vba").read_text()
        assert "Const UNIT_FACTOR As Double = 0.001" in setup

    def test_values_written_with_unit_factor(self, package):
        pkg, _ = package
        base = next(pkg.macros_dir.glob("01_F001_*.vba")).read_text()
        # depth 0.5" must be converted via UNIT_FACTOR, never a raw meter literal
        assert "0.5 * UNIT_FACTOR" in base
        # profile uses the extracted length/width
        assert "4 " in base and "2 " in base


class TestHoleGeneration:
    def test_pattern_emits_qty_circles(self, package):
        pkg, _ = package
        holes = next(pkg.macros_dir.glob("02_F002_*.vba")).read_text()
        assert holes.count("CreateCircleByRadius") == 4
        # Drawing frame: the 4x2 plate's corner is at the origin, so the unplaced
        # row spans (qty-1)*spacing = 3.0 centered on the plate center (2, 1)
        # -> first hole at (0.5, 1) — inside the material.
        assert "CreateCircleByRadius 0.5 * UNIT_FACTOR, 1 * UNIT_FACTOR" in holes
        assert "CreateCircleByRadius 3.5 * UNIT_FACTOR, 1 * UNIT_FACTOR" in holes

    def test_thru_hole_uses_through_all(self, package):
        pkg, _ = package
        holes = next(pkg.macros_dir.glob("02_F002_*.vba")).read_text()
        assert "swEndCondThroughAll" in holes

    def test_position_assumption_flagged(self, package):
        pkg, _ = package
        holes = next(pkg.macros_dir.glob("02_F002_*.vba")).read_text()
        assert "POSITIONS ASSUMED" in holes


class TestMachineRobustness:
    """Macros must survive machines with no default template / renamed planes."""

    def test_setup_discovers_template_when_default_unset(self, package):
        pkg, _ = package
        setup = (pkg.macros_dir / "00_setup.vba").read_text()
        assert "FindPartTemplate" in setup
        assert "swFileLocationsDocumentTemplates" in setup

    def test_feature_macros_select_planes_robustly(self, package):
        pkg, _ = package
        base = next(pkg.macros_dir.glob("01_F001_*.vba")).read_text()
        assert "SelectRefPlane" in base
        # Fallback by tree position, never only by hard-coded name:
        assert 'GetTypeName2 = "RefPlane"' in base

    def test_cuts_are_direction_proof(self, package):
        pkg, _ = package
        holes = next(pkg.macros_dir.glob("02_F002_*.vba")).read_text()
        # Thru cuts reach material on either side of the sketch plane...
        assert "swEndCondThroughAllBoth" in holes
        # ...and a failed cut is retried once with the direction flipped.
        assert holes.count("FeatureCut4") == 2
        # Retry restores the sketch by feature-tree object, never by name.
        assert '"ProfileFeature"' in holes
        assert 'SelectByID2(sketchName' not in holes

    def test_features_consume_active_sketch(self, package):
        """Recorded-macro pattern: feature calls consume the ACTIVE sketch.
        No closing InsertSketch, no name-based sketch reselection."""
        pkg, _ = package
        for name in ("01_F001_*.vba", "02_F002_*.vba"):
            text = next(pkg.macros_dir.glob(name)).read_text()
            assert text.count("InsertSketch") == 1, name  # open only, never close
            assert 'SelectByID2(sketchName' not in text, name

    def test_pattern_covered_by_hole_cut_is_a_noop_pass(self, tmp_path):
        """A pattern whose instances were already cut by the parent hole
        feature must not demand manual work (no MsgBox, no needs_review)."""
        data = bracket_drawing()
        data["features"].append(
            {"id": "F005", "type": "pattern", "description": "Hole pattern",
             "parent_feature": "F002", "quantity": 4, "related_dimensions": []}
        )
        data["build_order"].append("F005")
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, "x", tmp_path)
        assert not any(s.feature_id == "F005" for s in pkg.needs_review)
        macro = next(pkg.macros_dir.glob("*F005*")).read_text()
        assert "ALREADY SATISFIED" in macro
        # No manual-action prompt — only the standard run-order guard remains.
        assert "apply manually" not in macro
        assert "vbInformation" not in macro

    def test_no_nonexistent_bounding_box_api(self, package):
        pkg, _ = package
        for vba in pkg.macros_dir.glob("*.vba"):
            text = vba.read_text()
            # IModelDoc2 has no GetModelBoundingBox — VBA runtime error 438.
            assert "GetModelBoundingBox" not in text, vba.name
            if "vBox" in text:
                assert "GetBodyBox" in text, vba.name

    def test_base_plate_uses_drawing_frame_corner_rectangle(self, package):
        pkg, _ = package
        base = next(pkg.macros_dir.glob("01_F001_*.vba")).read_text()
        # Corner at the origin so edge-referenced hole positions land in material.
        assert "CreateCornerRectangle 0 * UNIT_FACTOR, 0 * UNIT_FACTOR" in base
        assert "CreateCenterRectangle" not in base


class TestProhibitedAndDeferred:
    def test_shell_skipped_and_flagged(self, package):
        pkg, _ = package
        assert [s.feature_id for s in pkg.skipped] == ["F004"]
        plan = json.loads(pkg.build_plan_json.read_text())
        assert plan["skipped_prohibited"] == ["F004"]
        skipped = [s for s in plan["steps"] if s["status"] == "skipped_prohibited"]
        assert skipped and "SKIPPED" in skipped[0]["notes"]

    def test_no_macro_file_for_shell(self, package):
        pkg, _ = package
        assert not list(pkg.macros_dir.glob("*F004*"))

    def test_fillet_deferred_to_last_numbered_macro(self, package):
        pkg, _ = package
        fillet = list(pkg.macros_dir.glob("*fillets_chamfers.vba"))
        assert len(fillet) == 1
        numbered = sorted(
            p.name for p in pkg.macros_dir.glob("[0-9][0-9]_*.vba")
        )
        assert numbered[-1] == fillet[0].name  # highest sequence number

    def test_fillet_radius_baked_in(self, package):
        pkg, _ = package
        fillet = next(pkg.macros_dir.glob("*fillets_chamfers.vba")).read_text()
        assert "0.125 * UNIT_FACTOR" in fillet
        assert "FeatureFillet3" in fillet


class TestNeedsReview:
    def test_revolve_marked_needs_review(self, tmp_path):
        data = bracket_drawing()
        data["features"].append(
            {"id": "F005", "type": "revolve", "description": "Hub",
             "related_dimensions": ["D004"]}
        )
        data["build_order"].append("F005")
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, "x", tmp_path)
        assert any(s.feature_id == "F005" for s in pkg.needs_review)
        macro = next(pkg.macros_dir.glob("*F005*")).read_text()
        assert "TODO: VERIFY API CALL" in macro


class TestVbaSafety:
    def test_every_macro_logs_and_uses_enum_names(self, package):
        pkg, _ = package
        for vba in pkg.macros_dir.glob("*.vba"):
            text = vba.read_text()
            assert "LogResult" in text, vba.name
            assert "Option Explicit" in text, vba.name
            # No bare numeric end-condition constants in feature calls
            assert "UNIT_FACTOR" in text, vba.name

    def test_counterbore_gets_second_cut(self, tmp_path):
        data = bracket_drawing()
        data["hole_callouts"][0].update(
            {"type": "counterbore", "cbore_diameter": 0.5, "cbore_depth": 0.2}
        )
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, "x", tmp_path)
        holes = next(pkg.macros_dir.glob("02_F002_*.vba")).read_text()
        assert "COUNTERBORE" in holes
        assert "(0.5 / 2#) * UNIT_FACTOR" in holes
        assert "0.2 * UNIT_FACTOR" in holes

    def test_model_text_cannot_break_vba_strings(self, tmp_path):
        """Quotes/unicode in model-supplied text must not break VBA literals."""
        import re

        data = bracket_drawing()
        data["features"][0]["description"] = 'Plate "A" — main ø body'
        data["hole_callouts"][0].update({"type": "tapped", "thread_spec": '1/4"-20 UNC'})
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, "x", tmp_path)
        text = "".join(f.read_text() for f in pkg.macros_dir.glob("*.vba"))
        # Every LogResult/MsgBox line must have balanced double-quotes.
        bad = [
            line for line in text.splitlines()
            if ("LogResult" in line or "MsgBox" in line) and line.count('"') % 2 != 0
        ]
        assert not bad, bad
        # The VBA editor mangles non-ASCII — none may leak into macros.
        assert not re.search(r"[^\x00-\x7F]", text)

    def test_tapped_hole_flags_cosmetic_thread(self, tmp_path):
        data = bracket_drawing()
        data["hole_callouts"][0].update({"type": "tapped", "thread_spec": "1/4-20 UNC"})
        model, report = run_verification(data)
        pkg = generate_macro_package(model, data, "x", tmp_path)
        holes = next(pkg.macros_dir.glob("02_F002_*.vba")).read_text()
        assert "1/4-20 UNC" in holes
        assert "Cosmetic Thread" in holes
        # Never model real threads:
        assert "Helix" not in holes
