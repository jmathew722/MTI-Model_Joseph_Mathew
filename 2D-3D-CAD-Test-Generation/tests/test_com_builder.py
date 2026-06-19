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
