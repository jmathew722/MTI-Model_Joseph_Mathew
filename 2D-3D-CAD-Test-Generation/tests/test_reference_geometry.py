"""Tests for Workstream 3 — reference-geometry (datum skeleton) derivation +
macro emission + build_plan integration."""
import json
from pathlib import Path

import pytest

from pipeline.macro_audit import audit_package
from pipeline.macro_generator import generate_macro_package
from pipeline.reference_geometry import (
    derive_reference_geometry,
    positioned_from,
    reference_geometry_macro_body,
)
from pipeline.resolver import resolve_extraction
from pipeline.validator import format_verification_report, run_verification


def _drawing(units="inch", symmetry=False, concentric=False, datums=False):
    d = {
        "part_number": "REFGEO-1", "units": units, "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": units, "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 2.0, "unit": units, "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 0.25, "unit": units, "applies_to": "height"},
            {"id": "D004", "type": "diameter", "value": 0.25, "unit": units,
             "applies_to": "hole_diameter", "feature_ref": "F002"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "thru", "diameter": 0.25, "qty": 4, "pattern": "linear",
             "pattern_spacing": 1.0, "feature_ref": "F002"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
             "sketch_plane": "Front"},
            {"id": "F002", "type": "hole", "description": "4 holes", "related_dimensions": ["D004"]},
        ],
        "build_order": ["F001", "F002"],
    }
    if datums:
        d["geometric_tolerances"] = [{"symbol": "position", "value": 0.01, "datum": "B"}]
    if symmetry:
        d["relationships"] = {"symmetry": [{"plane": "Right", "feature_ids": ["F002"]}]}
    if concentric:
        d.setdefault("relationships", {}).setdefault("concentric_groups", []).append(
            {"feature_ids": ["F002"], "description": "coaxial"})
    return d


def _model(d):
    res = resolve_extraction(d)
    m, _ = run_verification(res.clean_extraction)
    return m


class TestDerivation:
    def test_always_has_base_datum(self):
        refs = derive_reference_geometry(_model(_drawing()))
        ids = {r.id for r in refs}
        assert "REF_DATUM_A" in ids
        a = next(r for r in refs if r.id == "REF_DATUM_A")
        assert a.type == "plane" and a.parent == "Front Plane"

    def test_pattern_gets_ref_point(self):
        refs = derive_reference_geometry(_model(_drawing()))
        assert "REF_PT_F002" in {r.id for r in refs}  # 4-hole pattern origin

    def test_symmetry_gets_sym_plane(self):
        refs = derive_reference_geometry(_model(_drawing(symmetry=True)))
        syms = [r for r in refs if r.id.startswith("REF_SYM_")]
        assert syms and syms[0].type == "plane"

    def test_explicit_datum_b(self):
        refs = derive_reference_geometry(_model(_drawing(datums=True)))
        assert "REF_DATUM_B" in {r.id for r in refs}

    def test_concentric_group_gets_axis(self):
        refs = derive_reference_geometry(_model(_drawing(concentric=True)))
        assert any(r.id.startswith("REF_AXIS_") and r.type == "axis" for r in refs)

    def test_deterministic(self):
        m = _model(_drawing(symmetry=True))
        assert [r.id for r in derive_reference_geometry(m)] == \
               [r.id for r in derive_reference_geometry(m)]


class TestPositionedFrom:
    def test_pattern_positioned_from_ref_point(self):
        m = _model(_drawing())
        f002 = m.feature_by_id("F002")
        assert positioned_from(m, f002) == "REF_PT_F002"

    def test_base_positioned_from_datum_a(self):
        m = _model(_drawing())
        f001 = m.feature_by_id("F001")
        assert positioned_from(m, f001) == "REF_DATUM_A"


class TestMacroBody:
    def test_body_creates_named_planes(self):
        refs = derive_reference_geometry(_model(_drawing(symmetry=True)))
        body = reference_geometry_macro_body(refs)
        assert "InsertRefPlane" in body
        assert 'Name = "REF_DATUM_A"' in body
        assert "swRefPlaneReferenceConstraint_Distance" in body
        assert "LogResult" in body


class TestIntegration:
    def test_package_emits_reference_geometry(self, tmp_path):
        d = _drawing(symmetry=True)
        res = resolve_extraction(d)
        model, rep = run_verification(res.clean_extraction)
        pkg = generate_macro_package(model, d, format_verification_report(model, rep),
                                     tmp_path, resolution=res)
        # macro file present
        assert (pkg.macros_dir / "01a_reference_geometry.vba").is_file()
        # build_plan carries the block + positioned_from
        bp = json.loads(pkg.build_plan_json.read_text())
        assert len(bp["reference_geometry"]) >= 1
        assert any("REF_DATUM_A" == r["id"] for r in bp["reference_geometry"])
        steps = [s for s in bp["steps"] if s.get("feature_id") not in ("-", None)]
        assert all(s.get("positioned_from") for s in steps)
        # audit still passes (new APIs are not banned)
        assert audit_package(pkg.macros_dir).ok

    def test_no_datum_features_still_has_base(self, tmp_path):
        # Even a bare part gets REF_DATUM_A so there's always a landmark.
        d = _drawing()
        res = resolve_extraction(d)
        model, rep = run_verification(res.clean_extraction)
        pkg = generate_macro_package(model, d, format_verification_report(model, rep),
                                     tmp_path, resolution=res)
        assert any(r["id"] == "REF_DATUM_A" for r in pkg.reference_geometry)
