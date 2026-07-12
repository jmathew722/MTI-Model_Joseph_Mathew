"""Open-edge cut construction rule (2026-07-12, Task 1) — 158-C.

The notch built as an enclosed window instead of an open-through-top notch: the
sketch rectangle's open side landed exactly coincident with the plate edge
instead of overshooting it. Fixes: slot_cut.corner_array overshoots the OPEN
edge by EDGE_OVERSHOOT_EPS (closed sides stay exact); both the VBA and
CadQuery backends consume the SAME corners (numerical equivalence); a new
feature_verify EDGE_NOT_BROKEN check + geometric-correction-loop wiring catches
a still-enclosed cut post-build.
"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.cq_prevalidate import build_solid_from_plan
from pipeline.feature_verify import EDGE_NOT_BROKEN, OK, _Mesh, _verify_slot_cut
from pipeline.macro_generator import generate_macro_package
from pipeline.reconciliation import plan_corrections
from pipeline.resolver import resolve_extraction
from pipeline.schema import DrawingData
from pipeline.slot_cut import EDGE_OVERSHOOT_EPS, corner_array, interior_corners
from pipeline.validator import format_verification_report, run_verification

FIX = Path(__file__).resolve().parent / "fixtures" / "commit_mode"
cq = pytest.importorskip("cadquery")


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _build(raw):
    res = resolve_extraction(raw)
    model, rep = run_verification(res.clean_extraction)
    tmp = Path(tempfile.mkdtemp())
    pkg = generate_macro_package(model, raw, format_verification_report(model, rep),
                                 tmp, resolution=res)
    plan = json.loads(pkg.build_plan_json.read_text())
    return res, model, pkg, plan


@pytest.fixture(scope="module")
def c158():
    return _load("158-C_extraction.json")


# --------------------------------------------------------------------------- #
# corner_array overshoots only the OPEN edge; closed sides stay exact
# --------------------------------------------------------------------------- #
class TestOvershoot:
    def _slot(self, edge, **kw):
        base = dict(id="F1", slot_kind="open_notch", open_edge=edge, anchor_edge="left",
                    anchor_offset=1.56, anchor_semantics="edge_to_near_edge",
                    width=1.62, depth=1.88, corner_radius=0.25, thru=True,
                    thru_basis="single_view_default")
        base.update(kw)
        return type("S", (), base)()

    def _model(self, length=11.0, height=6.25):
        raw = {"part_number": "T", "units": "inch", "confidence": 0.9,
               "dimensions": [{"id": "D1", "type": "linear", "value": length, "unit": "inch", "applies_to": "length"},
                              {"id": "D2", "type": "linear", "value": height, "unit": "inch", "applies_to": "width"}],
               "features": [], "build_order": []}
        return DrawingData.model_validate(raw)

    def test_top_edge_overshoots_y_only(self):
        m = self._model()
        c = corner_array(self._slot("top"), m)
        # closed (bottom) corners exact; open (top) corners overshoot past height
        assert c[0][1] == pytest.approx(6.25 - 1.88) and c[1][1] == pytest.approx(6.25 - 1.88)
        assert c[2][1] == pytest.approx(6.25 + EDGE_OVERSHOOT_EPS)
        assert c[3][1] == pytest.approx(6.25 + EDGE_OVERSHOOT_EPS)
        # x unaffected
        assert c[0][0] == pytest.approx(1.56) and c[2][0] == pytest.approx(1.56 + 1.62)

    def test_bottom_edge_overshoots_below_zero(self):
        m = self._model()
        c = corner_array(self._slot("bottom"), m)
        ys = [p[1] for p in c]
        assert min(ys) == pytest.approx(-EDGE_OVERSHOOT_EPS)
        assert max(ys) == pytest.approx(1.88)  # closed end, exact

    def test_left_edge_overshoots_below_zero_x(self):
        m = self._model()
        c = corner_array(self._slot("left"), m)
        xs = [p[0] for p in c]
        assert min(xs) == pytest.approx(-EDGE_OVERSHOOT_EPS)

    def test_right_edge_overshoots_past_length(self):
        m = self._model(length=10.0)
        c = corner_array(self._slot("right"), m)
        xs = [p[0] for p in c]
        assert max(xs) == pytest.approx(10.0 + EDGE_OVERSHOOT_EPS)

    def test_closed_slot_no_overshoot(self):
        m = self._model()
        c = corner_array(self._slot("", slot_kind="closed_slot"), m)
        # fully interior — no coordinate should be negative or past the envelope
        assert all(0.0 <= p[0] <= 11.0 for p in c)

    def test_fillet_corners_stay_on_the_closed_exact_end(self):
        # interior_corners must NEVER include an overshot (open) corner — the
        # open end has no real corner to fillet.
        m = self._model()
        slot = self._slot("top")
        corners = corner_array(slot, m)
        fillet_corners = interior_corners(slot, corners)
        assert len(fillet_corners) == 2
        for x, y in fillet_corners:
            assert y == pytest.approx(6.25 - 1.88)  # the exact closed end


# --------------------------------------------------------------------------- #
# Golden: 158-C notch builds open-through-top, correct position/size
# --------------------------------------------------------------------------- #
class TestGolden158COpenEdge:
    def test_notch_corners_overshoot_top_edge(self, c158):
        _res, _m, _pkg, plan = _build(c158)
        rect = next(s for s in plan["steps"] if s.get("type") == "slot_rect_cut")
        corners = rect["sketch"]["corners_drawing_units"]
        assert corners == [[1.56, 4.37], [3.18, 4.37], [3.18, 6.30], [1.56, 6.30]]
        assert rect["sketch"]["open_edges"] == ["top"]

    def test_cadquery_solid_builds_and_breaks_the_edge(self, c158):
        _res, _m, _pkg, plan = _build(c158)
        solid = build_solid_from_plan(plan)
        assert solid.val().Volume() > 0
        # Export + re-measure: the built solid's own material must be ABSENT at
        # the top edge across the notch span (numerical equivalence + the real
        # open-edge check on the CadQuery-built geometry).
        stl_path = Path(tempfile.mktemp(suffix=".stl"))
        cq.exporters.export(solid, str(stl_path))
        mesh = _Mesh(stl_path, 0.105)
        rect = next(s for s in plan["steps"] if s.get("type") == "slot_rect_cut")
        result = _verify_slot_cut(mesh, rect, 0.015)
        assert result["classification"] == OK, result["checks"]

    def test_scoped_corner_fillets_two_bottom_corners(self, c158):
        _res, _m, _pkg, plan = _build(c158)
        fillet = next(s for s in plan["steps"] if s.get("type") == "slot_corner_fillet")
        assert fillet["corner_count_expected"] == 2
        for x, y in fillet["positions_xy"]:
            assert y == pytest.approx(4.37)  # the closed (bottom) end only

    def test_six_holes_present(self, c158):
        _res, _m, _pkg, plan = _build(c158)
        holes = [s for s in plan["steps"] if s.get("type") == "hole"]
        total_instances = sum(len(s.get("positions_xy") or []) for s in holes)
        assert total_instances == 6


# --------------------------------------------------------------------------- #
# EDGE_NOT_BROKEN detection: a still-enclosed window is caught, never silent OK
# --------------------------------------------------------------------------- #
class TestEdgeNotBrokenDetection:
    L, W, T = 11.0, 6.25, 0.105
    NX, NW, ND = 1.56, 1.62, 1.88

    def _step(self):
        return {"feature_id": "F002", "type": "slot_rect_cut",
                "dimensions_drawing_units": {"width": self.NW, "depth": self.ND, "anchor_offset": self.NX},
                "sketch": {"open_edge": "top"}}

    def _stl(self, cutter_wp):
        p = Path(tempfile.mktemp(suffix=".stl"))
        cq.exporters.export(cutter_wp, str(p))
        return p

    def test_open_notch_classifies_ok(self):
        k = 25.4
        plate = cq.Workplane("XY").rect(self.L * k, self.W * k, centered=False).extrude(self.T * k)
        wp = (plate.faces(">Z").workplane(origin=(0, 0, 0))
              .center(self.NX * k, (self.W - self.ND) * k)
              .rect(self.NW * k, (self.ND + 0.1) * k, centered=False))
        good = wp.cutThruAll()
        mesh = _Mesh(self._stl(good), self.T)
        res = _verify_slot_cut(mesh, self._step(), 0.015)
        assert res["classification"] == OK

    def test_enclosed_window_classifies_edge_not_broken(self):
        k = 25.4
        plate = cq.Workplane("XY").rect(self.L * k, self.W * k, centered=False).extrude(self.T * k)
        # pocket stops 0.2" short of the top edge — the exact defect (a lid of
        # material stands between the cut and the drawn open edge).
        wp = (plate.faces(">Z").workplane(origin=(0, 0, 0))
              .center(self.NX * k, (self.W - self.ND - 0.2) * k)
              .rect(self.NW * k, self.ND * k, centered=False))
        bad = wp.cutThruAll()
        mesh = _Mesh(self._stl(bad), self.T)
        res = _verify_slot_cut(mesh, self._step(), 0.015)
        assert res["classification"] == EDGE_NOT_BROKEN
        edge_check = next(c for c in res["checks"] if c["check"] == "edge_broken")
        assert edge_check["status"] == "FAIL"

    def test_missing_slot_classifies_missing(self):
        k = 25.4
        plate = cq.Workplane("XY").rect(self.L * k, self.W * k, centered=False).extrude(self.T * k)
        mesh = _Mesh(self._stl(plate), self.T)
        res = _verify_slot_cut(mesh, self._step(), 0.015)
        assert res["classification"] == "MISSING"


# --------------------------------------------------------------------------- #
# EDGE_NOT_BROKEN is wired into the geometric correction loop
# --------------------------------------------------------------------------- #
class TestCorrectionLoopWiring:
    def test_edge_not_broken_gets_a_reemit_correction(self):
        verification = {"mismatches": [
            {"feature_id": "F002", "classification": "EDGE_NOT_BROKEN",
             "expected": {"x": 1.0, "y": 1.0}, "measured": {"x": 1.0, "y": 1.0}},
        ], "extras": []}
        _transform, corrections = plan_corrections(verification, {})
        assert len(corrections) == 1
        assert corrections[0].mismatch_class == "EDGE_NOT_BROKEN"
        assert corrections[0].action == "reemit_step"


# --------------------------------------------------------------------------- #
# End-to-end: 158-C reaches READY with zero open items
# --------------------------------------------------------------------------- #
class TestEndToEndStatus:
    def test_158c_no_terminal_excluded_states(self, c158):
        _res, _m, _pkg, plan = _build(c158)
        disp = _pkg.dispositions
        assert all(d["state"] != "EXCLUDED_INCOMPLETE" for d in disp)
