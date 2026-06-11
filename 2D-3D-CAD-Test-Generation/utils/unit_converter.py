"""Unit conversion — CRITICAL for the SolidWorks API.

The SolidWorks COM API ALWAYS works in meters and radians internally,
regardless of the document's display units. Every linear dimension passed to
the API must go through :func:`to_meters`, and every angle through
:func:`to_radians`. There are no exceptions — a missed conversion silently
produces a part 1000× too large or small.
"""
from __future__ import annotations

import math

# Multiply a value in the given unit by this factor to get meters.
CONVERSION_TO_METERS: dict[str, float] = {
    "mm": 0.001,
    "millimeter": 0.001,
    "millimeters": 0.001,
    "cm": 0.01,
    "centimeter": 0.01,
    "centimeters": 0.01,
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
    '"': 0.0254,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
}

# Units we accept as a drawing's declared unit system (the documented contract).
SUPPORTED_DRAWING_UNITS = {"mm", "cm", "inch"}


def to_meters(value: float, unit: str) -> float:
    """Convert ``value`` expressed in ``unit`` to meters.

    Args:
        value: The numeric magnitude. Must be a finite, non-negative number —
            a negative length is never meaningful for geometry.
        unit: A unit string (case/space insensitive), e.g. ``"mm"``, ``"inch"``.

    Returns:
        The value in meters as a float.

    Raises:
        TypeError: if ``value`` is not a number.
        ValueError: if ``value`` is negative / non-finite, or ``unit`` is unknown.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"value must be a number, got {type(value).__name__}: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"value must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"value must be non-negative for geometry, got {value}")

    key = str(unit).lower().strip()
    if key not in CONVERSION_TO_METERS:
        raise ValueError(
            f"Unknown unit: {unit!r}. Known units: {sorted(set(CONVERSION_TO_METERS))}"
        )
    return value * CONVERSION_TO_METERS[key]


def to_radians(degrees: float) -> float:
    """Convert an angle in degrees to radians for SolidWorks angular dimensions.

    Raises:
        TypeError: if ``degrees`` is not a number.
        ValueError: if ``degrees`` is non-finite.
    """
    if isinstance(degrees, bool) or not isinstance(degrees, (int, float)):
        raise TypeError(f"degrees must be a number, got {type(degrees).__name__}")
    if not math.isfinite(degrees):
        raise ValueError(f"degrees must be finite, got {degrees!r}")
    return degrees * (math.pi / 180.0)


def assert_meters(value_m: float, label: str = "") -> float:
    """Sanity-gate a value that is *about to* be handed to the SolidWorks API.

    Catches gross unit mistakes before they reach SolidWorks. A real machined
    part dimension in meters is tiny (a 100 mm feature is 0.1 m); anything ≥ 100 m
    almost certainly means a value was passed in millimeters without conversion.

    Returns the value unchanged if it passes, so it can be used inline.
    """
    if not isinstance(value_m, (int, float)) or isinstance(value_m, bool):
        raise TypeError(f"{label}: expected a number in meters, got {value_m!r}")
    if not math.isfinite(value_m):
        raise ValueError(f"{label}: value in meters must be finite, got {value_m!r}")
    if value_m < 0:
        raise ValueError(f"{label}: value in meters must be non-negative, got {value_m}")
    if value_m >= 100.0:
        raise ValueError(
            f"{label}: value {value_m} m is implausibly large for a part feature — "
            "this usually means a dimension was NOT converted to meters via to_meters()."
        )
    return value_m
