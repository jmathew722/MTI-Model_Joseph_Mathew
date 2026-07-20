"""LIVE SolidWorks tests for the pywin32 build executor.

Skipped unless ``SOLIDWORKS_LIVE_TEST=1`` (CI has no SolidWorks). Run manually on
the Windows dev machine:

    set SOLIDWORKS_LIVE_TEST=1
    python -m pytest tests/test_build_executor_live.py -v

Each test drives a real SolidWorks 2024 session through the BuildExecutor
primitives and verifies the resulting geometry.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("SOLIDWORKS_LIVE_TEST") != "1",
    reason="Set SOLIDWORKS_LIVE_TEST=1 to run live SolidWorks tests.",
)


def _extents_of_stl(path):
    """Return the (dx, dy, dz) bounding-box extents of an STL, in meters."""
    trimesh = pytest.importorskip("trimesh")
    mesh = trimesh.load(path)
    lo, hi = mesh.bounds
    return tuple(float(hi[i] - lo[i]) for i in range(3))


def test_single_extruded_rectangle_stl_dims(tmp_path):
    """A 0.10 x 0.05 x 0.02 m box exports an STL whose extents match within 0.5 mm."""
    from automation.build_executor import BuildExecutor
    from automation.com_client import SolidWorksSession

    w, h, d = 0.10, 0.05, 0.02  # meters
    with SolidWorksSession() as session:
        ex = BuildExecutor(session, part_name="MTI_TEST_rect")
        ex.new_part(os.getenv("SOLIDWORKS_TEMPLATE_PATH"))
        ex.insert_sketch("front")
        # Rectangle with lower-left corner at the origin.
        ex.add_line(0.0, 0.0, w, 0.0)
        ex.add_line(w, 0.0, w, h)
        ex.add_line(w, h, 0.0, h)
        ex.add_line(0.0, h, 0.0, 0.0)
        ex.extrude(d)
        stl = ex.export_stl(tmp_path)

    dx, dy, dz = _extents_of_stl(stl)
    extents = sorted([dx, dy, dz])
    expected = sorted([w, h, d])
    for got, exp in zip(extents, expected):
        assert abs(got - exp) <= 5e-4, f"extent {got} != expected {exp}"


def test_filleted_edge_feature_exists_by_name(tmp_path):
    """A fillet applied to a box edge appears in the feature tree under its name."""
    from automation.build_executor import BuildExecutor
    from automation.com_client import SolidWorksSession

    w, h, d = 0.10, 0.05, 0.02
    with SolidWorksSession() as session:
        ex = BuildExecutor(session, part_name="MTI_TEST_fillet")
        ex.new_part(os.getenv("SOLIDWORKS_TEMPLATE_PATH"))
        ex.insert_sketch("front")
        ex.add_line(0.0, 0.0, w, 0.0)
        ex.add_line(w, 0.0, w, h)
        ex.add_line(w, h, 0.0, h)
        ex.add_line(0.0, h, 0.0, 0.0)
        ex.extrude(d)

        doc = ex.doc
        # Select a vertical edge near the corner (x=0, y=0) and fillet it.
        doc.ClearSelection2(True)
        assert doc.Extension.SelectByID2("", "EDGE", 0.0, h / 2.0, d, False, 0, None, 0) \
            or doc.Extension.SelectByID2("", "EDGE", 0.0, 0.0, d / 2.0, False, 0, None, 0)
        edge = doc.SelectionManager.GetSelectedObject6(1, -1)
        feat = ex.add_fillet([edge], 0.005, critical=True)
        assert feat is not None
        feat.Name = "MTI_TEST_Fillet"

        # Walk the tree and confirm a feature named MTI_TEST_Fillet exists.
        names = []
        f = doc.FirstFeature
        while f is not None:
            try:
                names.append(f.Name)
            except Exception:
                pass
            f = f.GetNextFeature
    assert "MTI_TEST_Fillet" in names
