"""Tests for pipeline.hole_resolution — the vector/vision consensus layer."""
import copy

from pipeline.hole_resolution import resolve_holes
from pipeline.macro_generator import generate_macro_package
from pipeline.schema import DrawingData
from pipeline.validator import format_verification_report, run_verification
from pipeline.vector_extract.geometry import DocGeometry, OutlineBox, VCircle


def plate_model(**hole_overrides) -> DrawingData:
    """A 17.5 x 14 inch plate with a 4x ⌀0.531 hole callout, positions unknown."""
    hole = {
        "id": "H001", "type": "thru", "diameter": 0.531, "qty": 4,
        "feature_ref": "F002",
    }
    hole.update(hole_overrides)
    return DrawingData.model_validate({
        "part_number": "VEC-01",
        "units": "inch",
        "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 17.5, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 14.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 1.0, "unit": "inch", "applies_to": "height"},
            {"id": "D004", "type": "diameter", "value": hole["diameter"], "unit": "inch",
             "applies_to": "hole_diameter", "feature_ref": "F002"},
        ],
        "hole_callouts": [hole],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003",
             "sketch_plane": "Top"},
            {"id": "F002", "type": "hole", "description": "Mounting holes",
             "related_dimensions": ["D004"]},
        ],
        "build_order": ["F001", "F002"],
    })


def plate_geometry(scale=10.0, source="pdf_vector", qty=4, radius=None,
                   origin=(50.0, 40.0)) -> DocGeometry:
    """Vector geometry for the plate at ``scale`` native units per inch:
    outline 175x140 native units, hole circles at the four 2x2 pattern corners
    (2.0, 2.0) .. (15.5, 12.0) inches."""
    x0, y0 = origin
    r = radius if radius is not None else (0.531 / 2.0) * scale
    kind = "dxf" if source == "dxf_entity" else "pdf_vector"
    geom = DocGeometry(source_kind=kind)
    geom.outlines.append(OutlineBox(x0, y0, x0 + 17.5 * scale, y0 + 14.0 * scale, meta="outline"))
    coords_inch = [(2.0, 2.0), (15.5, 2.0), (2.0, 12.0), (15.5, 12.0)][:qty]
    for (hx, hy) in coords_inch:
        geom.circles.append(VCircle(x0 + hx * scale, y0 + hy * scale, r, source))
    return geom


EXPECTED = [[2.0, 2.0], [2.0, 12.0], [15.5, 2.0], [15.5, 12.0]]  # sorted by (x, y)


class TestVectorExact:
    def test_positions_written_into_schema(self):
        model = plate_model()
        rep = resolve_holes(model, plate_geometry())
        h = model.hole_callouts[0]
        assert h.position_known is True
        assert h.position_source == "pdf_vector"
        assert h.position_confidence > 0.9
        assert h.instance_positions == EXPECTED
        assert rep.holes[0].outcome == "vector_exact"

    def test_positions_are_edge_referenced_drawing_units(self):
        model = plate_model()
        resolve_holes(model, plate_geometry(scale=3.7, origin=(123.4, 567.8)))
        # Whatever the native scale/origin, output must be inches from lower-left.
        assert model.hole_callouts[0].instance_positions == EXPECTED

    def test_bit_exact_across_runs(self):
        """Regression: vector positions carry no stochastic variance."""
        runs = []
        for _ in range(3):
            m = plate_model()
            resolve_holes(m, plate_geometry())
            runs.append(copy.deepcopy(m.hole_callouts[0].instance_positions))
        assert runs[0] == runs[1] == runs[2] == EXPECTED

    def test_dxf_source_tagged(self):
        model = plate_model()
        resolve_holes(model, plate_geometry(source="dxf_entity"))
        assert model.hole_callouts[0].position_source == "dxf_entity"

    def test_two_anchor_scale_no_high_flag(self):
        model = plate_model()
        rep = resolve_holes(model, plate_geometry())
        assert rep.scale_anchors >= 2
        assert not any(t == "HIGH" and "ONE dimension" in m
                       for hr in rep.holes for (t, m) in hr.flags)

    def test_circles_outside_outline_ignored(self):
        geom = plate_geometry()
        geom.circles.append(VCircle(2000.0, 2000.0, geom.circles[0].r, "pdf_vector"))
        model = plate_model()
        resolve_holes(model, geom)
        assert model.hole_callouts[0].instance_positions == EXPECTED


class TestPrecedenceAndFlags:
    def test_diameter_conflict_callout_value_wins_vector_position_wins(self):
        # Vector circles measure ⌀0.4; callout says ⌀0.531 → CRITICAL, positions
        # from vector, callout diameter untouched.
        model = plate_model()
        rep = resolve_holes(model, plate_geometry(radius=0.2 * 10.0))
        h = model.hole_callouts[0]
        assert h.diameter == 0.531                      # semantics: callout wins
        assert h.instance_positions == EXPECTED         # position: vector wins
        assert rep.holes[0].outcome == "diameter_conflict"
        assert any(t == "CRITICAL" for t, _ in rep.holes[0].flags)
        assert any("CRITICAL" in w for w in model.warnings)

    def test_qty_mismatch_fewer_circles_flagged_high(self):
        model = plate_model()
        rep = resolve_holes(model, plate_geometry(qty=3))
        h = model.hole_callouts[0]
        assert len(h.instance_positions) == 3
        assert any(t == "HIGH" and "only 3" in m for t, m in rep.holes[0].flags)

    def test_extra_circles_trimmed_and_flagged(self):
        model = plate_model(qty=2)
        rep = resolve_holes(model, plate_geometry(qty=4))
        h = model.hole_callouts[0]
        assert len(h.instance_positions) == 2
        assert any(t == "MEDIUM" for t, _ in rep.holes[0].flags)

    def test_no_matching_circle_keeps_vision_and_flags(self):
        model = plate_model(diameter=3.0)  # nothing measures ⌀3.0 and qty!=group
        geom = plate_geometry(qty=3)       # 3 circles vs qty 4 → no unique group
        rep = resolve_holes(model, geom)
        h = model.hole_callouts[0]
        assert h.position_known is False and h.instance_positions == []
        assert rep.holes[0].outcome in ("no_evidence", "diameter_conflict")
        assert any(w for w in model.warnings if "H001" in w)

    def test_no_circles_at_all_never_blocks(self):
        model = plate_model()
        rep = resolve_holes(model, DocGeometry(source_kind="pdf_vector"))
        assert rep.holes[0].outcome == "no_evidence"
        assert model.hole_callouts[0].position_known is False
        assert any("no vector evidence" in w for w in model.warnings)

    def test_hough_source_capped_confidence_and_flagged(self):
        model = plate_model()
        geom = plate_geometry(source="hough")
        geom.source_kind = "raster"
        resolve_holes(model, geom)
        h = model.hole_callouts[0]
        assert h.position_source == "hough"
        assert h.position_confidence <= 0.75
        assert any("RASTER" in w for w in model.warnings)

    def test_unanchorable_scale_keeps_vision(self):
        model = plate_model()
        geom = DocGeometry(source_kind="pdf_vector")
        geom.circles.append(VCircle(10, 10, 1.23, "pdf_vector"))  # matches nothing
        resolve_holes(model, geom)
        assert model.hole_callouts[0].position_known is False


class TestSchemaAndMacroContract:
    def test_old_extraction_json_still_loads(self):
        # Additive fields must not break pre-existing extractions.
        data = plate_model().model_dump(mode="json")
        for h in data["hole_callouts"]:
            h.pop("position_source", None)
            h.pop("position_confidence", None)
        model = DrawingData.model_validate(data)
        assert model.hole_callouts[0].position_source == ""

    def test_macro_stage_places_vector_positions_in_meters(self, tmp_path):
        model = plate_model()
        resolve_holes(model, plate_geometry())
        data = model.model_dump(mode="json")
        vmodel, report = run_verification(data)
        assert report.ok, str(report)
        pkg = generate_macro_package(vmodel, data,
                                     format_verification_report(vmodel, report), tmp_path)
        hole_steps = [s for s in pkg.steps if s.feature_id == "F002"]
        assert hole_steps, "hole step missing from build plan"
        step = hole_steps[0]
        # Exact meters: inches * 0.0254, no re-centering (corner frame kept).
        expect_m = sorted([[round(x * 0.0254, 10), round(y * 0.0254, 10)] for x, y in EXPECTED])
        got_m = sorted([[round(x, 10), round(y, 10)] for x, y in step.positions_xy_meters])
        assert got_m == expect_m
        assert step.position_source == "pdf_vector"
        assert step.position_confidence > 0.9

    def test_build_plan_json_carries_additive_keys(self, tmp_path):
        import json

        model = plate_model()
        resolve_holes(model, plate_geometry(source="dxf_entity"))
        data = model.model_dump(mode="json")
        vmodel, report = run_verification(data)
        pkg = generate_macro_package(vmodel, data,
                                     format_verification_report(vmodel, report), tmp_path)
        plan = json.loads(pkg.build_plan_json.read_text())
        hole = next(s for s in plan["steps"] if s["feature_id"] == "F002")
        assert hole["position_source"] == "dxf_entity"
        assert hole["position_confidence"] > 0.9
        # Legacy keys untouched.
        assert "positions_xy_meters" in hole and "dimensions_meters" in hole

    def test_resolver_no_longer_assumes_position(self):
        """With vector positions, Stage 2.5 must NOT emit POSITION ASSUMED."""
        from pipeline.resolver import resolve_extraction

        model = plate_model()
        resolve_holes(model, plate_geometry())
        resolution = resolve_extraction(model.model_dump(mode="json"))
        texts = " ".join(str(i) for i in resolution.resolved_extraction.get("hole_callouts", []))
        summary = str(resolution.summary.__dict__)
        assert "POSITION ASSUMED" not in texts
        # Without vector data the same model DOES get the assumption:
        bare = plate_model()
        res2 = resolve_extraction(bare.model_dump(mode="json"))
        assert res2 is not None  # sanity
