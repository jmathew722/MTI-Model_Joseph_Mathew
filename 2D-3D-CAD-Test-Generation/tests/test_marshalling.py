"""Standalone tests for automation.marshalling — VARIANT/SAFEARRAY construction.

VARIANT construction needs pywin32 (win32com/pythoncom) but NOT a running
SolidWorks, so these run on any Windows machine with pywin32 installed. They skip
cleanly where pywin32 is unavailable (e.g. a Linux CI box).
"""
import pytest

pythoncom = pytest.importorskip("pythoncom")
pytest.importorskip("win32com.client")

from automation import marshalling
from automation.com_client import SolidWorksComError


def test_to_point_variant_flags_and_value():
    v = marshalling.to_point_variant(1.0, 2.5, -3.0)
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8)
    assert tuple(v.value) == (1.0, 2.5, -3.0)


def test_to_point_variant_coerces_ints_to_float():
    v = marshalling.to_point_variant(0, 0, 0)
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8)
    assert tuple(v.value) == (0.0, 0.0, 0.0)
    assert all(isinstance(x, float) for x in v.value)


def test_to_double_array_variant_arbitrary_length():
    vals = [0.1, 0.2, 0.3, 0.4, 0.5]
    v = marshalling.to_double_array_variant(vals)
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8)
    assert list(v.value) == pytest.approx(vals)


def test_to_double_array_variant_empty():
    v = marshalling.to_double_array_variant([])
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8)
    assert tuple(v.value) == ()


def test_to_dispatch_array_variant_flags():
    # An empty dispatch array still carries the correct element type flag.
    v = marshalling.to_dispatch_array_variant([])
    assert v.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH)


def test_create_sw_point_without_math_utility_raises_structured():
    with pytest.raises(SolidWorksComError) as ei:
        marshalling.create_sw_point(None, 1.0, 2.0, 3.0)
    assert ei.value.method == "IMathUtility.CreatePoint"
    # The structured payload is lessons-ledger ready.
    lesson = ei.value.as_lesson()
    assert lesson["kind"] == "com_error"
    assert lesson["source"] == "pywin32_build_executor"
