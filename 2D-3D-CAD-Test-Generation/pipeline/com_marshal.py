"""Centralized VARIANT / SAFEARRAY marshalling for the SolidWorks COM builder.

Every VARIANT the COM builder hands to SolidWorks is constructed HERE, so there is
exactly one place that can get the element-type flags wrong (the classic pywin32
SAFEARRAY gotcha). `pipeline/solidworks_builder.py` keeps thin re-exporting
wrappers (e.g. ``_null_dispatch``) for backward compatibility, so no other caller
in the codebase breaks.

WINDOWS ONLY at call time. ``win32com``/``pythoncom`` are imported lazily inside
each function so this module imports on any OS (the marshalling tests construct
real VARIANTs on Windows; they skip where pywin32 is unavailable).

Units contract
--------------
Every coordinate passed to these functions is **already in meters** (SolidWorks'
internal unit). Each function gates its inputs with :func:`_assert_coord_m`, which
enforces finiteness and a plausible magnitude bound (|v| < 100 m — the same gross
"someone forgot to convert mm" tripwire as :func:`utils.unit_converter.assert_meters`)
but, unlike ``assert_meters``, PERMITS NEGATIVE values: a sketch coordinate can be
negative (e.g. a notch overshoot below y=0, or a face pick at ``-thickness/2``),
whereas ``assert_meters`` is for magnitudes (lengths) and forbids negatives. The
length-magnitude gate (``assert_meters``) still applies at the geometry call sites
in ``solidworks_builder.py``; this gate is the coordinate-appropriate complement.
"""
from __future__ import annotations

import math
from typing import Sequence

# Coordinates larger than this in absolute value almost certainly mean a value
# reached the API without a to_meters() conversion (a 100 mm feature is 0.1 m).
_MAX_PLAUSIBLE_COORD_M = 100.0


def _assert_coord_m(value: float, label: str = "coord") -> float:
    """Gate a single coordinate that is about to enter a VARIANT (meters).

    Finite and |value| < 100 m; negatives allowed (see the module docstring on why
    this is NOT :func:`utils.unit_converter.assert_meters`). Returns it unchanged.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label}: expected a number in meters, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{label}: coordinate in meters must be finite, got {value!r}")
    if abs(value) >= _MAX_PLAUSIBLE_COORD_M:
        raise ValueError(
            f"{label}: coordinate {value} m is implausibly large for a part feature — "
            "this usually means a value was NOT converted to meters via to_meters().")
    return float(value)


def _win32():
    """Return ``(VARIANT, pythoncom)`` or raise a clear error if pywin32 is absent."""
    try:
        import pythoncom  # type: ignore
        from win32com.client import VARIANT  # type: ignore

        return VARIANT, pythoncom
    except Exception as e:  # pragma: no cover - only on a machine without pywin32
        raise RuntimeError(
            f"pywin32 (win32com/pythoncom) is required to build COM VARIANTs: {e}") from e


def null_dispatch():
    """A VT_DISPATCH NULL VARIANT (the value SolidWorks wants for a null Object arg).

    Moved verbatim from ``solidworks_builder._null_dispatch``. Late-bound COM calls
    can't pass plain Python ``None`` for an ``Object``-typed parameter — dynamic
    dispatch sends VT_EMPTY and SolidWorks' Invoke rejects it with "Type mismatch".
    An explicit VT_DISPATCH NULL VARIANT is accepted.
    """
    VARIANT, pythoncom = _win32()
    return VARIANT(pythoncom.VT_DISPATCH, None)


def point_variant(x_m: float, y_m: float, z_m: float):
    """A ``VT_ARRAY | VT_R8`` VARIANT for a 3-D point (x, y, z), meters.

    The single source of truth for point construction — nothing else should build
    a point VARIANT inline.
    """
    VARIANT, pythoncom = _win32()
    x = _assert_coord_m(x_m, "point.x")
    y = _assert_coord_m(y_m, "point.y")
    z = _assert_coord_m(z_m, "point.z")
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (x, y, z))


def double_array_variant(values: Sequence[float]):
    """A ``VT_ARRAY | VT_R8`` VARIANT for an arbitrary-length double array (meters).

    For any multi-point API (spline/polyline control points). Each element is gated
    by :func:`_assert_coord_m`.
    """
    VARIANT, pythoncom = _win32()
    gated = tuple(_assert_coord_m(v, f"array[{i}]") for i, v in enumerate(values))
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, gated)
