"""COM .sldprt builder geometry tests (no SolidWorks required).

These exercise the pure-Python placement logic of pipeline.solidworks_builder with
a fake ``sw_doc`` that records the geometry calls, so the regressions that left
features OUT of the built model are locked down on any OS:

  * holes take their diameter + EVERY instance position from the hole CALLOUT
    (not the feature's bolt-circle/spacing dims), so a full pattern is cut;
  * a counterbore adds a second concentric cut;
  * the base solid is built corner-at-origin (matching the macros / build plan).
"""
import pytest

from pipeline import solidworks_builder as swb
from pipeline.schema import DrawingData


# --------------------------------------------------------------------------- #
# Fake SolidWorks document — records the geometry calls the builders make.
# --------------------------------------------------------------------------- #
class _FakeSketchMgr:
    def __init__(self, rec):
        self.rec = rec
        self._active = None

    def InsertSketch(self, flag):
        # Toggle: first call opens a sketch, the matching call closes it.
        self._active = object() if self._active is None else None

    @property
    def ActiveSketch(self):
        return self._active

    def CreateCircleByRadius(self, cx, cy, cz, r):
        self.rec.setdefault("circles", []).append((round(cx, 6), round(cy, 6), round(r, 6)))
        return object()

    def CreateCornerRectangle(self, x1, y1, z1, x2, y2, z2):
        self.rec.setdefault("rects", []).append((round(x1, 6), round(y1, 6), round(x2, 6), round(y2, 6)))
        return object()

    def CreateCenterRectangle(self, *a):  # not expected anymore; flag if used
        self.rec.setdefault("center_rects", []).append(a)
        return object()

    def FullyDefineSketch(self, *a):
        return True


class _FakeFeatureMgr:
    def __init__(self, rec):
        self.rec = rec

    def FeatureExtrusion3(self, *a):
        self.rec.setdefault("extrudes", []).append(a)
        return object()

    def FeatureCut4(self, *a):
        self.rec.setdefault("cuts", []).append(a)
        return object()


class _FakeExt:
    def SelectByID2(self, *a):
        return True


class _FakeSketch:
    def GetConstrainedStatus(self):
        return 1  # fully defined


class _FakeDoc:
    def __init__(self, rec, has_body=True):
        self.rec = rec
        self.SketchManager = _FakeSketchMgr(rec)
        self.FeatureManager = _FakeFeatureMgr(rec)
        self.Extension = _FakeExt()
        self._has_body = has_body

    # _solid_body_exists path
    def GetBodies2(self, body_type, visible):
        return [object()] if self._has_body else []

    def ClearSelection2(self, flag):
        pass


@pytest.fixture(autouse=True)
def _patch_active_sketch(monkeypatch):
    # _verify_sketch_fully_defined reads SketchManager.ActiveSketch.GetConstrainedStatus;
    # our fake ActiveSketch is a bare object(), so give it the method via the verifier.
    monkeypatch.setattr(swb, "_verify_sketch_fully_defined", lambda doc: None)


def _plate_with_bolt_pattern() -> DrawingData:
    return DrawingData.model_validate({
        "part_number": "COM-1", "units": "inch", "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 10.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 6.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "linear", "value": 0.25, "unit": "inch", "applies_to": "thickness"},
            {"id": "D004", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "bolt_circle"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "counterbore", "diameter": 0.25, "thru": True, "qty": 4,
             "cbore_diameter": 0.5, "cbore_depth": 0.12, "feature_ref": "F002",
             "instance_positions": [[1.0, 1.0], [9.0, 1.0], [1.0, 5.0], [9.0, 5.0]]},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
            {"id": "F002", "type": "hole", "description": "corner bolt holes",
             "related_dimensions": ["D004"], "parent_feature": "F001"},
        ],
        "build_order": ["F001", "F002"], "relationships": {},
    })


class TestHoleCalloutBuild:
    def test_hole_uses_callout_diameter_and_all_positions(self):
        model = _plate_with_bolt_pattern()
        feat = model.feature_by_id("F002")
        rec = {}
        doc = _FakeDoc(rec)
        swb.build_hole(doc, model, feat, {})  # dims empty: must come from the callout
        circles = rec["circles"]
        # 4 main holes + 4 counterbores = 8 circles.
        assert len(circles) == 8
        # Main-hole radius = 0.25/2 in → meters.
        main_r = round(0.25 / 2 * 0.0254, 6)
        cbore_r = round(0.5 / 2 * 0.0254, 6)
        radii = {c[2] for c in circles}
        assert main_r in radii and cbore_r in radii
        # Positions (first instance 1.0,1.0 in → m) are present, NOT the origin.
        first = (round(1.0 * 0.0254, 6), round(1.0 * 0.0254, 6))
        assert any((c[0], c[1]) == first for c in circles)
        # Two cuts: the through hole + the blind counterbore.
        assert len(rec["cuts"]) == 2

    def test_hole_without_callout_falls_back_to_feature_diameter(self):
        model = _plate_with_bolt_pattern()
        # Strip the callout; supply a diameter via dims instead. NOTE: pipeline
        # feature dims are already in METERS (unlike callout values), so the
        # fallback uses them as-is.
        model.hole_callouts = []
        feat = model.feature_by_id("F002")
        rec = {}
        diameter_m = 0.3 * 0.0254
        swb.build_hole(_FakeDoc(rec), model, feat, {"diameter": diameter_m})
        assert len(rec["circles"]) == 1
        assert rec["circles"][0][2] == round(diameter_m / 2, 6)


class TestThreadAndPattern:
    def test_thread_with_callout_drills_the_hole(self):
        model = _plate_with_bolt_pattern()
        # Reuse H001 but make it a tapped feature F003.
        model.hole_callouts[0].feature_ref = "F003"
        model.hole_callouts[0].type = swb.HoleType.TAPPED
        model.features.append(type(model.features[-1]).model_validate({
            "id": "F003", "type": "thread", "description": "tapped holes",
            "related_dimensions": [], "parent_feature": "F001",
        }))
        feat = model.feature_by_id("F003")
        rec = {}
        result = swb.build_thread(_FakeDoc(rec), model, feat, {})
        assert result is not swb._NOOP            # it drilled
        assert len(rec["circles"]) == 4           # the 4 tap holes

    def test_thread_without_callout_is_noop(self):
        model = _plate_with_bolt_pattern()
        model.features.append(type(model.features[-1]).model_validate({
            "id": "F009", "type": "thread", "description": "cosmetic only",
            "related_dimensions": [],
        }))
        feat = model.feature_by_id("F009")
        rec = {}
        assert swb.build_thread(_FakeDoc(rec), model, feat, {}) is swb._NOOP
        assert "circles" not in rec               # nothing drilled

    def test_redundant_pattern_is_noop(self):
        model = _plate_with_bolt_pattern()
        # A pattern whose seed (the F002 hole) already placed all instances.
        model.features.append(type(model.features[-1]).model_validate({
            "id": "F003", "type": "pattern", "description": "bolt pattern",
            "parent_feature": "F002", "quantity": 4,
        }))
        feat = model.feature_by_id("F003")
        result = swb.build_pattern(_FakeDoc({}), model, feat, {}, {"F002": object()})
        assert result is swb._NOOP


class TestBaseFrame:
    def test_base_rectangle_is_corner_at_origin(self):
        model = _plate_with_bolt_pattern()
        feat = model.feature_by_id("F001")
        rec = {}
        swb.build_extrude_boss(_FakeDoc(rec, has_body=True), model, feat,
                               {"length": 10.0 * 0.0254, "width": 6.0 * 0.0254, "depth": 0.25 * 0.0254})
        # Corner rectangle from (0,0) to (L,W) — no center-rectangle calls.
        assert "rects" in rec and rec["rects"]
        x1, y1, x2, y2 = rec["rects"][0]
        assert (x1, y1) == (0.0, 0.0)
        assert x2 > 0 and y2 > 0
        assert "center_rects" not in rec


# --------------------------------------------------------------------------- #
# Richer fake harness (extends the minimal one above) for the feature builders
# not covered before: fillet/chamfer, revolve/mirror, circular pattern, the
# canonical slot (E014 coordinate-normalize routing), and partial-save-on-crash.
# --------------------------------------------------------------------------- #
IN = 0.0254


class _FakeEdge:
    def __init__(self, rec):
        self.rec = rec

    def Select4(self, append, mark):
        self.rec["edges_selected"] = self.rec.get("edges_selected", 0) + 1
        return True


class _FakeBody:
    def __init__(self, rec, box, n_edges=4):
        self.rec = rec
        self._box = box
        self._edges = [_FakeEdge(rec) for _ in range(n_edges)]

    def GetBodyBox(self):
        return self._box

    def GetEdges(self):
        return self._edges

    def GetFaces(self):
        return []


class _RichSketchMgr(_FakeSketchMgr):
    def CreateLine(self, x1, y1, z1, x2, y2, z2):
        self.rec.setdefault("lines", []).append(
            (round(x1, 6), round(y1, 6), round(x2, 6), round(y2, 6)))
        return object()

    def CreateCenterLine(self, x1, y1, z1, x2, y2, z2):
        self.rec.setdefault("centerlines", []).append((round(x1, 6), round(x2, 6)))
        return _FakeSelectable(self.rec)


class _FakeSelectable:
    def __init__(self, rec):
        self.rec = rec

    def Select4(self, append, mark):
        self.rec["selectable_selected"] = self.rec.get("selectable_selected", 0) + 1
        return True

    def Select2(self, append, mark):
        self.rec["seed_selected"] = self.rec.get("seed_selected", 0) + 1
        return True


class _RichFeatureMgr(_FakeFeatureMgr):
    def __init__(self, rec, fillet_returns=True, chamfer_returns=True,
                 revolve_returns=True, mirror_returns=True):
        super().__init__(rec)
        self._fillet_returns = fillet_returns
        self._chamfer_returns = chamfer_returns
        self._revolve_returns = revolve_returns
        self._mirror_returns = mirror_returns

    def FeatureFillet3(self, *a):
        self.rec.setdefault("fillets", []).append(a)
        return object() if self._fillet_returns else None

    def InsertFeatureChamfer(self, *a):
        self.rec.setdefault("chamfers", []).append(a)
        return object() if self._chamfer_returns else None

    def FeatureRevolve2(self, *a):
        self.rec.setdefault("revolves", []).append(a)
        return object() if self._revolve_returns else None

    def InsertMirrorFeature2(self, *a):
        self.rec.setdefault("mirrors", []).append(a)
        return object() if self._mirror_returns else None


class _RichDoc:
    """Body-aware fake doc: supports edge selection, body box, lines/centerlines."""

    def __init__(self, rec, box=(0.0, 0.0, 0.0, 6.0 * IN, 6.25 * IN, 0.25 * IN),
                 n_edges=4, **fm_kwargs):
        self.rec = rec
        self.SketchManager = _RichSketchMgr(rec)
        self.FeatureManager = _RichFeatureMgr(rec, **fm_kwargs)
        self.Extension = _FakeExt()
        self._body = _FakeBody(rec, box, n_edges=n_edges)
        self.FirstFeature = None

    def GetBodies2(self, body_type, visible):
        return [self._body]

    def ClearSelection2(self, flag):
        pass


class TestFilletAndChamfer:
    def test_fillet_edge_strategy_all_vs_feature(self):
        model = _plate_with_bolt_pattern()
        fil = type(model.features[-1]).model_validate({
            "id": "F004", "type": "fillet", "description": "R.25", "related_dimensions": []})
        # No parent → all edges.
        assert swb._fillet_edge_strategy(fil, {}) == ("all", "")
        # Parent built → scope to feature.
        fil2 = type(model.features[-1]).model_validate({
            "id": "F005", "type": "fillet", "description": "R.25",
            "related_dimensions": [], "parent_feature": "F001"})
        assert swb._fillet_edge_strategy(fil2, {"F001": object()}) == ("feature", "F001")

    def test_build_fillet_selects_all_body_edges(self):
        model = _plate_with_bolt_pattern()
        fil = type(model.features[-1]).model_validate({
            "id": "F004", "type": "fillet", "description": "R.25", "related_dimensions": []})
        rec = {}
        feat = swb.build_fillet(_RichDoc(rec, n_edges=4), model, fil,
                                {"radius": 0.25 * IN}, {})
        assert feat is not None
        assert rec.get("edges_selected") == 4       # all body edges selected
        assert len(rec.get("fillets", [])) == 1     # one FeatureFillet3 call

    def test_build_fillet_zero_edges_raises_precondition(self):
        model = _plate_with_bolt_pattern()
        fil = type(model.features[-1]).model_validate({
            "id": "F004", "type": "fillet", "description": "R.25", "related_dimensions": []})
        with pytest.raises(swb.SolidWorksError):
            swb.build_fillet(_RichDoc({}, n_edges=0), model, fil, {"radius": 0.25 * IN}, {})

    def test_fragile_fillet_failure_is_demoted_not_fatal(self, monkeypatch):
        # A fillet whose FeatureFillet3 returns None must be SKIPPED (fragile),
        # never abort the build — matches the module's stated design discipline.
        model = DrawingData.model_validate({
            "part_number": "FIL-1", "units": "inch", "confidence": 0.9,
            "dimensions": [
                {"id": "D001", "type": "linear", "value": 6.0, "unit": "inch", "applies_to": "length"},
                {"id": "D002", "type": "linear", "value": 6.0, "unit": "inch", "applies_to": "width"},
                {"id": "D003", "type": "linear", "value": 0.25, "unit": "inch", "applies_to": "thickness"},
                {"id": "D004", "type": "radial", "value": 0.25, "unit": "inch", "applies_to": "fillet_radius"},
            ],
            "features": [
                {"id": "F001", "type": "extrude_boss", "description": "plate",
                 "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
                {"id": "F004", "type": "fillet", "description": "R.25 all edges",
                 "related_dimensions": ["D004"]},
            ],
            "build_order": ["F001", "F004"], "relationships": {},
        })
        monkeypatch.setattr(swb, "_require_windows", lambda: None)
        monkeypatch.setattr(swb, "create_new_part",
                            lambda app, tpl=None: _RichDoc({}, fillet_returns=False))
        monkeypatch.setattr(swb, "set_document_units", lambda doc, u: None)
        monkeypatch.setattr(swb, "save_model", lambda doc, name, out=None: "C:/fake.sldprt")

        skipped: list = []
        # Must NOT raise — the fragile fillet is demoted and the build completes.
        swb.build_model(object(), model, output_dir=".", strict=True, skipped_out=skipped)
        assert any(fid == "F004" for fid, *_ in skipped), skipped


class TestChamfer:
    def test_build_chamfer_applies_and_warns(self):
        model = _plate_with_bolt_pattern()
        ch = type(model.features[-1]).model_validate({
            "id": "F006", "type": "chamfer", "description": "C.05", "related_dimensions": []})
        rec = {}
        feat = swb.build_chamfer(_RichDoc(rec, n_edges=4), model, ch, {"chamfer": 0.05 * IN}, {})
        assert feat is not None
        assert len(rec.get("chamfers", [])) == 1
        assert any("chamfer" in w.lower() for w in model.warnings)


def _revolve_model():
    return DrawingData.model_validate({
        "part_number": "REV-1", "units": "inch", "confidence": 0.9,
        "dimensions": [{"id": "D001", "type": "linear", "value": 1.0, "unit": "inch",
                        "applies_to": "length"}],
        "features": [{"id": "F001", "type": "revolve", "description": "shaft",
                      "related_dimensions": ["D001"],
                      "revolve_profile": [[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]]}],
        "build_order": ["F001"], "relationships": {},
    })


class TestRevolveAndMirror:
    def test_build_revolve_makes_axis_and_revolve_feature(self):
        model = _revolve_model()
        feat = model.feature_by_id("F001")
        rec = {}
        result = swb.build_revolve(_RichDoc(rec), model, feat, {})
        assert result is not None
        assert rec.get("centerlines")           # a revolve axis centerline drawn
        assert rec.get("lines")                 # the profile polyline
        assert len(rec.get("revolves", [])) == 1

    def test_build_mirror_selects_seed_and_plane(self):
        model = _plate_with_bolt_pattern()
        mir = type(model.features[-1]).model_validate({
            "id": "F007", "type": "mirror", "description": "mirror",
            "related_dimensions": [], "parent_feature": "F001", "mirror_plane": "front"})
        seed = _FakeSelectable({})
        rec = {}
        result = swb.build_mirror(_RichDoc(rec), model, mir, {}, {"F001": seed})
        assert result is not None
        assert len(rec.get("mirrors", [])) == 1

    def test_build_mirror_without_seed_raises(self):
        model = _plate_with_bolt_pattern()
        mir = type(model.features[-1]).model_validate({
            "id": "F007", "type": "mirror", "description": "mirror",
            "related_dimensions": [], "parent_feature": "F404"})
        with pytest.raises(swb.SolidWorksError):
            swb.build_mirror(_RichDoc({}), model, mir, {}, {})


class TestCircularPattern:
    def test_canonical_spec_instance_count_and_angle(self):
        # The values FeatureCircularPattern5 receives: total_instances INCLUDES
        # the seed, and equal spacing over 360°. (The full COM pattern trio is
        # exercised by the live build in Step 6.)
        from pipeline.macro_generator import canonical_circular_pattern
        from pipeline.schema import HoleCallout

        model = _plate_with_bolt_pattern()
        feat = model.feature_by_id("F002")
        h = HoleCallout.model_validate({
            "id": "H9", "type": "thru", "diameter": 0.25, "qty": 6,
            "pattern": "circular", "bolt_circle_diameter": 4.0, "start_angle": 0.0})
        spec = canonical_circular_pattern(model, feat, h, "PatternAxis1", "bore face")
        assert spec["total_instances"] == 6          # seed + 5 copies
        assert spec["total_angle_deg"] == 360.0
        assert spec["equal_spacing"] is True


def _top_notch_model():
    """A plate 6.00 wide × 6.25 tall with a TOP-edge notch 1.88 deep (the
    coordinate_normalize.py worked example → notch y_min = 6.25 − 1.88 = 4.37)."""
    return DrawingData.model_validate({
        "part_number": "NOTCH-1", "units": "inch", "confidence": 0.9,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 6.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 6.25, "unit": "inch", "applies_to": "height"},
            {"id": "D003", "type": "linear", "value": 0.25, "unit": "inch", "applies_to": "thickness"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003"},
            {"id": "F002", "type": "extrude_cut", "description": "top notch",
             "related_dimensions": [], "sketch_plane": "front"},
        ],
        "slot_cuts": [
            {"id": "F002", "slot_kind": "open_notch", "open_edge": "top",
             "anchor_edge": "left", "anchor_offset": 2.5, "width": 1.0, "depth": 1.88,
             "corner_radius": 0.25, "thru": True},
        ],
        "build_order": ["F001", "F002"], "relationships": {},
    })


class TestCoordinateNormalizeIntegration:
    def test_top_notch_builds_at_ymin_4p37_not_zero(self):
        # E014: the COM builder must place a top-edge notch at y_min = 4.37 (in),
        # never y_min = 0 (the bottom edge) — routed through coordinate_normalize.
        model = _top_notch_model()
        feat = model.feature_by_id("F002")
        rec = {}
        swb.build_extrude_cut(_RichDoc(rec), model, feat, {})
        assert rec.get("lines"), "slot rectangle should be drawn as 4 lines"
        ys_m = [y for line in rec["lines"] for y in (line[1], line[3])]
        y_min_in = min(ys_m) / IN
        assert abs(y_min_in - 4.37) < 1e-3, f"notch y_min={y_min_in:.4f} in (expected 4.37)"
        # And definitely not sitting at the bottom edge.
        assert y_min_in > 4.0


class TestPartialSaveOnCrash:
    def test_partial_saved_before_exception_propagates(self, monkeypatch):
        # When a non-fragile feature raises mid-build in STRICT mode, build_model
        # must save a PARTIAL_<feature> model before re-raising.
        model = _plate_with_bolt_pattern()
        saves: list[str] = []
        monkeypatch.setattr(swb, "save_model",
                            lambda doc, name, out=None: saves.append(name) or "C:/fake.sldprt")

        # Make the SECOND feature (the hole, non-fragile) fail deterministically.
        real_dispatch = swb.dispatch_feature_builder

        def _boom(sw_doc, m, feature, dims, fmap):
            if feature.id == "F002":
                raise swb.SolidWorksError("forced failure for test")
            return real_dispatch(sw_doc, m, feature, dims, fmap)

        monkeypatch.setattr(swb, "dispatch_feature_builder", _boom)
        monkeypatch.setattr(swb, "_require_windows", lambda: None)
        monkeypatch.setattr(swb, "create_new_part", lambda app, tpl=None: _RichDoc({}))
        monkeypatch.setattr(swb, "set_document_units", lambda doc, u: None)

        with pytest.raises(swb.SolidWorksError):
            swb.build_model(object(), model, output_dir=".", strict=True)
        assert any(n.startswith("PARTIAL_F002") for n in saves), saves
