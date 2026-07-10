"""Tests for pipeline.feature_verify — Phase A per-feature geometric verification.

Meshes are built with CadQuery (headless, deterministic) and exported to STL,
then measured back. This doubles as the integration guarantee the spec asks for:
a CadQuery-built part must pass Phase A against its own build plan — validating
the measurer itself before it ever judges a SolidWorks build. The classifier is
then exercised by deliberately dropping / shifting / resizing / adding a hole
relative to the plan and asserting the verdict.
"""
import json
from pathlib import Path

import pytest

cq = pytest.importorskip("cadquery")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pipeline.feature_verify import (
    MISPLACED,
    MISSING,
    OK,
    WRONG_SIZE,
    EXTRA,
    verify_features,
)

K = 25.4  # inch -> mm


def _plate_with_holes(path: Path, length=4.0, width=2.0, thick=0.25, holes=()):
    """Build an inch-dimensioned plate (lower-left at origin) with through holes
    at the given (x, y, dia) and export mm STL — mirrors the pipeline's frame."""
    wp = (cq.Workplane("XY").center(length / 2 * K, width / 2 * K)
          .box(length * K, width * K, thick * K, centered=(True, True, False)))
    for (x, y, dia) in holes:
        wp = (wp.faces(">Z").workplane(origin=(0, 0, 0))
              .pushPoints([(x * K, y * K)]).hole(dia * K))
    cq.exporters.export(wp, str(path))
    return path


def _plan(holes, length=4.0, width=2.0, thick=0.25):
    """A minimal build_plan.json dict describing a plate + through holes."""
    steps = [{
        "seq": 1, "type": "extrude_boss", "feature_id": "F001",
        "dimensions_drawing_units": {"length": length, "width": width, "depth": thick},
        "positions_xy": [[0.0, 0.0]], "depth_type": "",
    }]
    for i, (x, y, dia) in enumerate(holes, start=2):
        steps.append({
            "seq": i, "type": "hole", "feature_id": f"F{i:03d}",
            "dimensions_drawing_units": {"diameter": dia}, "positions_xy": [[x, y]],
            "depth_type": "through_all",
        })
    return {"units": "inch", "unit_factor_to_meters": 0.0254,
            "coordinate_origin": "lower_left_corner_of_base_solid", "steps": steps}


@pytest.fixture(scope="module")
def good_stl(tmp_path_factory):
    p = tmp_path_factory.mktemp("fv") / "good.stl"
    _plate_with_holes(p, holes=[(1.0, 1.0, 0.5), (3.0, 1.0, 0.5)])
    return p


class TestSelfVerification:
    """A CadQuery part passes Phase A against its own plan (validates the measurer)."""

    def test_matching_part_all_ok(self, good_stl):
        plan = _plan([(1.0, 1.0, 0.5), (3.0, 1.0, 0.5)])
        rep = verify_features(good_stl, plan, Path("."), write=False)
        assert rep["ok"], rep
        assert rep["summary"]["mismatches"] == 0
        assert rep["summary"]["extras"] == 0
        holes = [f for f in rep["features"] if f["kind"] == "hole"]
        assert len(holes) == 2 and all(h["classification"] == OK for h in holes)

    def test_base_envelope_ok(self, good_stl):
        plan = _plan([(1.0, 1.0, 0.5), (3.0, 1.0, 0.5)])
        rep = verify_features(good_stl, plan, Path("."), write=False)
        base = next(f for f in rep["features"] if f["kind"] == "base")
        assert base["classification"] == OK
        assert base["classification"] != "UNMEASURABLE"


class TestClassifier:
    def test_missing_hole(self, good_stl):
        # Plan expects a THIRD hole the mesh does not have.
        plan = _plan([(1.0, 1.0, 0.5), (3.0, 1.0, 0.5), (2.0, 1.5, 0.5)])
        rep = verify_features(good_stl, plan, Path("."), write=False)
        cls = [f["classification"] for f in rep["features"] if f["kind"] == "hole"]
        assert MISSING in cls
        assert not rep["ok"]

    def test_misplaced_hole(self, good_stl):
        # Plan puts the second hole 0.4" from where the mesh actually has it.
        plan = _plan([(1.0, 1.0, 0.5), (3.4, 1.0, 0.5)])
        rep = verify_features(good_stl, plan, Path("."), write=False)
        misplaced = [f for f in rep["features"]
                     if f["kind"] == "hole" and f["classification"] == MISPLACED]
        assert misplaced, [f["classification"] for f in rep["features"]]
        # The measured position is reported (drawing stays truth; model is what's wrong).
        assert misplaced[0]["measured"] is not None
        assert abs(misplaced[0]["measured"]["x"] - 3.0) < 0.1

    def test_wrong_size_hole(self, good_stl):
        # Plan expects dia 0.75 where the mesh has 0.5.
        plan = _plan([(1.0, 1.0, 0.75), (3.0, 1.0, 0.5)])
        rep = verify_features(good_stl, plan, Path("."), write=False)
        wrong = [f for f in rep["features"]
                 if f["kind"] == "hole" and f["classification"] == WRONG_SIZE]
        assert wrong, [f["classification"] for f in rep["features"]]
        assert abs(wrong[0]["measured"]["diameter"] - 0.5) < 0.05

    def test_extra_hole(self, good_stl):
        # Plan omits the second hole the mesh actually has -> EXTRA.
        plan = _plan([(1.0, 1.0, 0.5)])
        rep = verify_features(good_stl, plan, Path("."), write=False)
        assert rep["summary"]["extras"] >= 1
        assert any(e["classification"] == EXTRA for e in rep["extras"])
        assert not rep["ok"]


class TestUnmeasurable:
    def test_fillet_is_unmeasurable_with_reason(self, good_stl):
        plan = _plan([(1.0, 1.0, 0.5), (3.0, 1.0, 0.5)])
        plan["steps"].append({"seq": 9, "type": "fillet", "feature_id": "F009",
                              "dimensions_drawing_units": {"fillet_radius": 0.125},
                              "positions_xy": [], "depth_type": ""})
        rep = verify_features(good_stl, plan, Path("."), write=False)
        fillet = next(f for f in rep["features"] if f["feature_id"] == "F009")
        assert fillet["classification"] == "UNMEASURABLE"
        assert fillet.get("reason")  # never silent

    def test_no_unmeasurable_without_reason(self, good_stl):
        plan = _plan([(1.0, 1.0, 0.5)])
        plan["steps"].append({"seq": 9, "type": "chamfer", "feature_id": "F009",
                              "dimensions_drawing_units": {"chamfer": 0.06},
                              "positions_xy": [], "depth_type": ""})
        rep = verify_features(good_stl, plan, Path("."), write=False)
        for f in rep["features"]:
            if f["classification"] == "UNMEASURABLE":
                assert f.get("reason"), f


class TestReportWriting:
    def test_writes_json(self, good_stl, tmp_path):
        plan = _plan([(1.0, 1.0, 0.5), (3.0, 1.0, 0.5)])
        verify_features(good_stl, plan, tmp_path, part="TESTPART", write=True)
        out = tmp_path / "TESTPART_feature_verification.json"
        assert out.is_file()
        data = json.loads(out.read_text())
        assert data["part"] == "TESTPART"
        assert "summary" in data and "features" in data
