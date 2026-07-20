"""Standalone tests for pipeline.com_marshal — VARIANT/SAFEARRAY construction.

VARIANT construction needs pywin32 (win32com/pythoncom) but NOT a live SolidWorks
connection, so these run on any Windows machine with pywin32 and skip cleanly
where it is unavailable. The coordinate-gate tests are pure Python and always run.
"""
import math

import pytest

from pipeline import com_marshal


# -- coordinate gate (pure, no pywin32) ----------------------------------- #
def test_coord_gate_accepts_negative_and_zero():
    # Sketch coordinates can be negative (notch overshoot, -thickness/2 face pick).
    assert com_marshal._assert_coord_m(-0.05) == -0.05
    assert com_marshal._assert_coord_m(0.0) == 0.0
    assert com_marshal._assert_coord_m(0.1524) == pytest.approx(0.1524)


def test_coord_gate_rejects_unconverted_mm():
    # 158.75 (mm value not converted to meters) is implausibly large in meters.
    with pytest.raises(ValueError):
        com_marshal._assert_coord_m(158.75, "point.x")


def test_coord_gate_rejects_nonfinite_and_nonnumber():
    with pytest.raises(ValueError):
        com_marshal._assert_coord_m(math.inf)
    with pytest.raises(TypeError):
        com_marshal._assert_coord_m("0.1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        com_marshal._assert_coord_m(True)  # bool is not a coordinate


# -- VARIANT construction (needs pywin32) --------------------------------- #
pythoncom = pytest.importorskip("pythoncom")
pytest.importorskip("win32com.client")


def test_null_dispatch_flags():
    v = com_marshal.null_dispatch()
    assert v.varianttype == pythoncom.VT_DISPATCH
    assert v.value is None


def test_point_variant_flags_and_value():
    v = com_marshal.point_variant(0.1, -0.05, 0.0)
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8)
    assert tuple(v.value) == (0.1, -0.05, 0.0)


def test_point_variant_rejects_unconverted_value():
    with pytest.raises(ValueError):
        com_marshal.point_variant(158.75, 0.0, 0.0)  # mm not converted


def test_double_array_variant_flags_and_value():
    vals = [0.0, 0.01, -0.02, 0.03]
    v = com_marshal.double_array_variant(vals)
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8)
    assert list(v.value) == pytest.approx(vals)


def test_double_array_variant_empty():
    v = com_marshal.double_array_variant([])
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8)
    assert tuple(v.value) == ()


def test_builder_shim_delegates_to_com_marshal():
    # The backward-compat wrapper in solidworks_builder must produce the same thing.
    from pipeline import solidworks_builder as swb

    v = swb._null_dispatch()
    assert v.varianttype == pythoncom.VT_DISPATCH
    assert v.value is None
