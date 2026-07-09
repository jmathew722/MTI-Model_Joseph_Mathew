"""Standard drill-size plausibility table (inch).

A degraded hole-diameter reading is far more trustworthy when it lands on a
standard drill size — ".218" (a #2 letter drill) is almost certainly right,
whereas ".213" is suspect (learning-loop 2026-07-09, Issue "illegible dimension
readings kept as unverified best guesses": part 102 D008/D009). Used to
annotate low-confidence hole diameters and decide whether to route them to
markup review.

Public entry points: :func:`nearest_drill`, :func:`is_standard_drill`.
"""
from __future__ import annotations

from typing import Optional

# Fractional drills 1/64" .. 1/2" in 1/64 steps.
_FRACTIONAL = [round(n / 64.0, 5) for n in range(1, 33)]

# Number drills #1..#60 (decimal inch), the common subset.
_NUMBER = [
    0.2280, 0.2210, 0.2130, 0.2090, 0.2055, 0.2040, 0.2010, 0.1990, 0.1960, 0.1935,  # 1-10
    0.1910, 0.1890, 0.1850, 0.1820, 0.1800, 0.1770, 0.1730, 0.1695, 0.1660, 0.1610,  # 11-20
    0.1590, 0.1570, 0.1540, 0.1520, 0.1495, 0.1470, 0.1440, 0.1405, 0.1360, 0.1285,  # 21-30
    0.1200, 0.1160, 0.1130, 0.1110, 0.1100, 0.1065, 0.1040, 0.1015, 0.0995, 0.0980,  # 31-40
    0.0960, 0.0935, 0.0890, 0.0860, 0.0820, 0.0810, 0.0785, 0.0760, 0.0730, 0.0700,  # 41-50
    0.0670, 0.0635, 0.0595, 0.0550, 0.0520, 0.0465, 0.0430, 0.0420, 0.0410, 0.0400,  # 51-60
]

# Letter drills A..Z (decimal inch).
_LETTER = [
    0.234, 0.238, 0.242, 0.246, 0.250, 0.257, 0.261, 0.266, 0.272, 0.277,  # A-J
    0.281, 0.290, 0.295, 0.302, 0.316, 0.323, 0.332, 0.339, 0.348, 0.358,  # K-T
    0.368, 0.377, 0.386, 0.397, 0.404, 0.413,                              # U-Z
]

_ALL: list[float] = sorted(set(_FRACTIONAL + _NUMBER + _LETTER))


def nearest_drill(value_in: float) -> tuple[float, float]:
    """Return ``(nearest_standard_size, abs_difference)`` in inches."""
    if not _ALL:
        return (value_in, 0.0)
    best = min(_ALL, key=lambda d: abs(d - value_in))
    return best, abs(best - value_in)


def is_standard_drill(value_in: float, tol: float = 0.002) -> bool:
    """True if ``value_in`` matches a standard drill size within ``tol`` inches
    (default .002 — tighter than a typical drill tolerance, so a match is real
    evidence the reading is correct)."""
    if value_in <= 0:
        return False
    return nearest_drill(value_in)[1] <= tol
