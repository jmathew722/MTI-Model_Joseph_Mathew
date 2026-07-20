"""VARIANT / SAFEARRAY marshalling — the single source of truth for COM arrays.

SolidWorks' API takes coordinates, vectors, and multi-entity selections as
``VARIANT`` arrays (SAFEARRAYs). Passing a bare Python list/tuple to a late-bound
call is the classic pywin32 SolidWorks gotcha: the array element type is
ambiguous and the call fails with a type mismatch or silently returns ``Nothing``.

Every point/array VARIANT in the codebase MUST be built through the helpers here
so the element type flags (``VT_R8`` for doubles, ``VT_DISPATCH`` for object
arrays) are always explicit and correct. Nothing else should construct an array
VARIANT inline.

The VARIANT constructor itself does not require a running SolidWorks — only
``win32com``/``pythoncom`` (pywin32). These functions are therefore unit-testable
standalone (see ``tests/test_marshalling.py``).
"""
from __future__ import annotations

from typing import Sequence

from automation.com_client import SolidWorksComError


def _variant():
    """Return ``(VARIANT, pythoncom)`` or raise a clear error if pywin32 is absent."""
    try:
        import pythoncom  # type: ignore
        from win32com.client import VARIANT  # type: ignore

        return VARIANT, pythoncom
    except Exception as e:  # pragma: no cover - only on a machine without pywin32
        raise SolidWorksComError(
            "VARIANT", f"pywin32 (win32com/pythoncom) is required to build COM "
            f"arrays: {e}") from e


def to_point_variant(x: float, y: float, z: float):
    """A 3-element ``VT_ARRAY | VT_R8`` VARIANT for an (x, y, z) point.

    This is the single source of truth for point construction — nothing else in
    the codebase should build a point VARIANT inline.
    """
    VARIANT, pythoncom = _variant()
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                   (float(x), float(y), float(z)))


def to_double_array_variant(values: Sequence[float]):
    """A ``VT_ARRAY | VT_R8`` VARIANT for an arbitrary-length double array.

    Used for spline control points, polygon vertices, and any API taking a flat
    ``double[]`` (e.g. ``CreateLine``-style batched coordinates).
    """
    VARIANT, pythoncom = _variant()
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                   tuple(float(v) for v in values))


def to_dispatch_array_variant(objects: Sequence):
    """A ``VT_ARRAY | VT_DISPATCH`` VARIANT for an array of COM objects.

    Used by methods that take a set of entities (faces/edges/features) at once,
    e.g. building a fillet from several selected edges.
    """
    VARIANT, pythoncom = _variant()
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, tuple(objects))


def create_sw_point(math_utility, x: float, y: float, z: float):
    """Build a native SolidWorks ``MathPoint`` at (x, y, z) via ``MathUtility``.

    Logs the exact VARIANT type flags used on failure so a marshalling problem is
    diagnosable rather than a bare ``Nothing``.
    """
    if math_utility is None:
        raise SolidWorksComError(
            "IMathUtility.CreatePoint",
            "no MathUtility available on the session (SolidWorksSession.math_utility "
            "is None) — cannot build a MathPoint.")
    point_variant = to_point_variant(x, y, z)
    try:
        pt = math_utility.CreatePoint(point_variant)
    except Exception as e:
        raise SolidWorksComError(
            "IMathUtility.CreatePoint",
            f"failed for ({x}, {y}, {z}) with VT_ARRAY|VT_R8 point VARIANT: {e}",
            args=(x, y, z)) from e
    if pt is None:
        raise SolidWorksComError(
            "IMathUtility.CreatePoint",
            f"returned Nothing for ({x}, {y}, {z}) — check the VARIANT element "
            "type (must be VT_ARRAY|VT_R8).", args=(x, y, z))
    return pt
