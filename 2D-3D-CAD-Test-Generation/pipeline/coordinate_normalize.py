"""Centralized coordinate normalization — the ONE place semantic drawing anchors
become global CAD coordinates (2026-07-13).

The orientation bug this module exists to make impossible:

    Extraction correctly identifies a notch as opening from the TOP edge and
    stores a top-edge-relative location such as (1.56, 0). The VBA then treats
    y = 0 as an absolute lower-left coordinate, and the notch lands on the
    BOTTOM edge.

The fix is a single canonical coordinate convention and a single resolver. For
the primary front view of a plate-like part:

    Origin = lower-left corner of the finished parent profile
    +X = right, +Y = up, +Z = extrusion/thickness

A TOP-edge feature is resolved as:

    y_min = parent_height - feature_depth
    y_max = parent_height

For 158-C (plate H = 6.25, notch depth = 1.88):  6.25 - 1.88 = 4.37.

Anchor arithmetic MUST live here, never scattered across VBA templates. Both the
UI view-model and the VBA generator consume the SAME resolved object, so the
table and the macro can never disagree. Length values stay in inches through the
normalized model; conversion to meters (SolidWorks API unit) happens exactly
once, at the VBA boundary, via :data:`INCH_TO_M` / :func:`to_meters`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# The single inch→meter conversion. SolidWorks API length arguments are meters;
# every VBA literal is `inches * INCH_TO_M` (or written pre-multiplied), applied
# exactly once at the VBA boundary — never mixed into normalized-model math.
INCH_TO_M = 0.0254


def to_meters(inches: float) -> float:
    """Inches → meters. The ONE conversion; call it only at the VBA boundary."""
    return float(inches) * INCH_TO_M


class Anchor(str, Enum):
    """Every semantic anchor a normalized feature may declare. Point anchors
    resolve to a single (x, y); edge anchors resolve to a notch bounds box."""

    LOWER_LEFT = "LOWER_LEFT"
    LOWER_RIGHT = "LOWER_RIGHT"
    UPPER_LEFT = "UPPER_LEFT"
    UPPER_RIGHT = "UPPER_RIGHT"
    LEFT_EDGE = "LEFT_EDGE"
    RIGHT_EDGE = "RIGHT_EDGE"
    TOP_EDGE = "TOP_EDGE"
    BOTTOM_EDGE = "BOTTOM_EDGE"
    CENTER = "CENTER"
    DATUM_POINT = "DATUM_POINT"
    DATUM_AXIS = "DATUM_AXIS"
    FEATURE_RELATIVE = "FEATURE_RELATIVE"
    ABSOLUTE_GLOBAL = "ABSOLUTE_GLOBAL"


_EDGE_ANCHORS = {Anchor.TOP_EDGE, Anchor.BOTTOM_EDGE, Anchor.LEFT_EDGE, Anchor.RIGHT_EDGE}
_POINT_ANCHORS = {
    Anchor.LOWER_LEFT, Anchor.LOWER_RIGHT, Anchor.UPPER_LEFT, Anchor.UPPER_RIGHT,
    Anchor.CENTER, Anchor.ABSOLUTE_GLOBAL, Anchor.DATUM_POINT, Anchor.FEATURE_RELATIVE,
}

# Map the slot schema's open_edge string to the corresponding edge anchor.
_OPEN_EDGE_TO_ANCHOR = {
    "top": Anchor.TOP_EDGE,
    "bottom": Anchor.BOTTOM_EDGE,
    "left": Anchor.LEFT_EDGE,
    "right": Anchor.RIGHT_EDGE,
}


def anchor_from_open_edge(open_edge: str) -> Optional[Anchor]:
    """Slot ``open_edge`` (top/bottom/left/right) → the edge :class:`Anchor`, or
    None for a closed slot (no open edge)."""
    return _OPEN_EDGE_TO_ANCHOR.get((open_edge or "").lower())


@dataclass(frozen=True)
class Bounds:
    """A resolved global bounds box in DRAWING units (inches), lower-left origin."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def as_dict(self) -> dict:
        return {"x_min": round(self.x_min, 6), "x_max": round(self.x_max, 6),
                "y_min": round(self.y_min, 6), "y_max": round(self.y_max, 6)}

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min


@dataclass(frozen=True)
class Point:
    """A resolved global point in DRAWING units (inches), lower-left origin."""

    x: float
    y: float

    def as_dict(self) -> dict:
        return {"x": round(self.x, 6), "y": round(self.y, 6)}


class CoordinateError(ValueError):
    """A feature whose resolved coordinates are impossible or out of bounds."""


def resolve_notch_anchor(
    anchor: Anchor,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    width: float = 0.0,
    depth: float = 0.0,
    height: float = 0.0,
    parent_width: float = 0.0,
    parent_height: float = 0.0,
) -> Bounds:
    """Resolve an EDGE-anchored notch/cut to global :class:`Bounds` (inches).

    The depth always runs INWARD from the named edge; the along-edge span is
    ``width`` for top/bottom and ``height`` for left/right (matching the drawing
    convention). This is the single locus of the ``H - depth`` calculation.
    """
    a = _as_anchor(anchor)
    if a == Anchor.TOP_EDGE:
        return Bounds(offset_x, offset_x + width, parent_height - depth, parent_height)
    if a == Anchor.BOTTOM_EDGE:
        return Bounds(offset_x, offset_x + width, 0.0, depth)
    if a == Anchor.LEFT_EDGE:
        return Bounds(0.0, depth, offset_y, offset_y + height)
    if a == Anchor.RIGHT_EDGE:
        return Bounds(parent_width - depth, parent_width, offset_y, offset_y + height)
    raise CoordinateError(f"{anchor}: not an edge anchor; use resolve_point_anchor for points")


def resolve_point_anchor(
    anchor: Anchor,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    parent_width: float = 0.0,
    parent_height: float = 0.0,
) -> Point:
    """Resolve a POINT anchor (hole center, datum point) to a global
    :class:`Point` (inches). ABSOLUTE_GLOBAL / DATUM_POINT / FEATURE_RELATIVE are
    passed through verbatim — they are already global (or resolved upstream)."""
    a = _as_anchor(anchor)
    if a == Anchor.LOWER_LEFT:
        return Point(offset_x, offset_y)
    if a == Anchor.LOWER_RIGHT:
        return Point(parent_width - offset_x, offset_y)
    if a == Anchor.UPPER_LEFT:
        return Point(offset_x, parent_height - offset_y)
    if a == Anchor.UPPER_RIGHT:
        return Point(parent_width - offset_x, parent_height - offset_y)
    if a == Anchor.CENTER:
        return Point(parent_width / 2.0 + offset_x, parent_height / 2.0 + offset_y)
    if a in (Anchor.ABSOLUTE_GLOBAL, Anchor.DATUM_POINT, Anchor.FEATURE_RELATIVE):
        return Point(offset_x, offset_y)
    raise CoordinateError(f"{anchor}: not a point anchor; use resolve_notch_anchor for edges")


def validate_bounds(
    bounds: Bounds,
    *,
    parent_width: float,
    parent_height: float,
    overshoot_edge: Optional[Anchor] = None,
    overshoot_eps: float = 0.06,
) -> list[str]:
    """Check a resolved notch is geometrically sane and inside the parent.

    An open edge is allowed to exceed the parent by up to ``overshoot_eps`` (the
    intentional edge-breaking overshoot); every other side must stay inside.
    Returns a list of violation strings (empty = OK). Never raises — the caller
    decides whether a violation gates.
    """
    v: list[str] = []
    if not all(math.isfinite(x) for x in (bounds.x_min, bounds.x_max, bounds.y_min, bounds.y_max)):
        v.append("resolved coordinates are not finite")
        return v
    if bounds.x_max <= bounds.x_min or bounds.y_max <= bounds.y_min:
        v.append(f"degenerate bounds {bounds.as_dict()} (max <= min)")
    oe = _as_anchor(overshoot_edge) if overshoot_edge is not None else None
    lo = -overshoot_eps
    hi_w = parent_width + overshoot_eps
    hi_h = parent_height + overshoot_eps
    # Sides that are NOT the open edge must stay strictly inside the parent.
    if oe != Anchor.LEFT_EDGE and bounds.x_min < lo:
        v.append(f"x_min {bounds.x_min:.4g} < 0")
    if oe != Anchor.RIGHT_EDGE and parent_width and bounds.x_max > hi_w:
        v.append(f"x_max {bounds.x_max:.4g} > parent width {parent_width:.4g}")
    if oe != Anchor.BOTTOM_EDGE and bounds.y_min < lo:
        v.append(f"y_min {bounds.y_min:.4g} < 0")
    if oe != Anchor.TOP_EDGE and parent_height and bounds.y_max > hi_h:
        v.append(f"y_max {bounds.y_max:.4g} > parent height {parent_height:.4g}")
    return v


def assert_edge_orientation(
    anchor: Anchor,
    bounds: Bounds,
    *,
    parent_height: float,
    parent_width: float,
    depth: float,
    tol: float = 1e-3,
) -> None:
    """The 158-C regression guard. An edge notch resolved to the WRONG side is
    the exact orientation bug this module prevents — refuse it loudly.

    A TOP_EDGE notch on a plate of finite height must NOT sit at y ≈ 0..depth
    (that is the bottom-edge placement); it must sit at H-depth..H. Symmetric
    checks for the other three edges.
    """
    a = _as_anchor(anchor)
    if a == Anchor.TOP_EDGE and parent_height > depth + tol:
        if abs(bounds.y_min) <= tol and abs(bounds.y_max - depth) <= tol:
            raise CoordinateError(
                f"TOP_EDGE notch resolved to the BOTTOM (y={bounds.y_min:.4g}..{bounds.y_max:.4g}); "
                f"a top-edge feature must be at y={parent_height - depth:.4g}..{parent_height:.4g} "
                f"(parent_height - depth). This is the 158-C top/bottom orientation bug.")
    if a == Anchor.BOTTOM_EDGE and parent_height > depth + tol:
        if abs(bounds.y_max - parent_height) <= tol and abs(bounds.y_min - (parent_height - depth)) <= tol:
            raise CoordinateError(
                f"BOTTOM_EDGE notch resolved to the TOP (y={bounds.y_min:.4g}..{bounds.y_max:.4g}); "
                f"a bottom-edge feature must be at y=0..{depth:.4g}.")
    if a == Anchor.LEFT_EDGE and parent_width > depth + tol:
        if abs(bounds.x_min - (parent_width - depth)) <= tol and abs(bounds.x_max - parent_width) <= tol:
            raise CoordinateError(
                f"LEFT_EDGE notch resolved to the RIGHT (x={bounds.x_min:.4g}..{bounds.x_max:.4g}); "
                f"a left-edge feature must be at x=0..{depth:.4g}.")
    if a == Anchor.RIGHT_EDGE and parent_width > depth + tol:
        if abs(bounds.x_min) <= tol and abs(bounds.x_max - depth) <= tol:
            raise CoordinateError(
                f"RIGHT_EDGE notch resolved to the LEFT (x={bounds.x_min:.4g}..{bounds.x_max:.4g}); "
                f"a right-edge feature must be at x={parent_width - depth:.4g}..{parent_width:.4g}.")


def _as_anchor(anchor) -> Anchor:
    if isinstance(anchor, Anchor):
        return anchor
    try:
        return Anchor(str(anchor).upper())
    except ValueError as e:
        raise CoordinateError(f"unknown anchor {anchor!r}") from e
