"""Shared geometry types for the vector hole-extraction subsystem.

All extractors (DXF, vector PDF, raster) normalize their findings into a
:class:`DocGeometry` so the consensus layer (:mod:`pipeline.hole_resolution`)
can treat every source identically.

COORDINATE CONVENTION: every extractor emits coordinates in a **bottom-left
origin, y-up** frame (the drawing frame the rest of the pipeline thinks in).
PDF page coordinates are y-down and are flipped by the PDF extractor before
they get here. Values are in the document's NATIVE units (DXF drawing units,
PDF points, raster pixels); conversion to the drawing's declared units happens
in the consensus layer via scale anchoring.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# position_source values, most→least authoritative for POSITION.
SOURCE_DXF = "dxf_entity"
SOURCE_PDF = "pdf_vector"
SOURCE_HOUGH = "hough"
SOURCE_VISION = "vision"


@dataclass(frozen=True)
class VCircle:
    """One circle found in the document, native units, bottom-left frame."""

    cx: float
    cy: float
    r: float
    source: str  # SOURCE_DXF | SOURCE_PDF | SOURCE_HOUGH
    meta: str = ""  # entity handle / block name / path index — for tracing
    center_marked: bool = False  # a centerline cross coincides with this center


@dataclass(frozen=True)
class VDim:
    """A dimension measurement with a position (DXF DIMENSION entity or a
    positioned dimension string). ``value`` is in native units for DXF
    measurements; for text-parsed dims it is the printed number (drawing units)."""

    value: float
    x: float
    y: float
    kind: str = ""  # linear | diameter | ordinate | text
    text: str = ""  # raw text when parsed from a string


@dataclass(frozen=True)
class VText:
    """A positioned text string (DXF TEXT/MTEXT or PDF word)."""

    text: str
    x: float
    y: float


@dataclass(frozen=True)
class OutlineBox:
    """A candidate part-outline rectangle (closed path / rect entity), native
    units, bottom-left frame. (x0, y0) is the lower-left corner."""

    x0: float
    y0: float
    x1: float
    y1: float
    meta: str = ""

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class DocGeometry:
    """Everything one extractor learned about one document/page."""

    source_kind: str  # 'dxf' | 'pdf_vector' | 'raster'
    circles: list[VCircle] = field(default_factory=list)
    dims: list[VDim] = field(default_factory=list)
    texts: list[VText] = field(default_factory=list)
    outlines: list[OutlineBox] = field(default_factory=list)
    # Native unit → millimeters, when the document declares it (DXF INSUNITS).
    # None means unknown (PDF points, raster pixels) — scale must be anchored.
    native_units_to_mm: float | None = None
    is_raster: bool = False  # vector extraction found nothing usable
    notes: list[str] = field(default_factory=list)


def group_full_circles(
    arcs: list[tuple[float, float, float, float, float]],
    min_coverage_deg: float = 350.0,
    tol: float = 1e-6,
) -> list[tuple[float, float, float]]:
    """Group ARC segments (cx, cy, r, start_deg, end_deg) into full circles.

    Holes are sometimes drawn as 2-4 arc segments instead of one CIRCLE entity.
    Arcs sharing a center+radius (within ``tol``) whose combined angular extent
    covers >= ``min_coverage_deg`` are reported as one circle. Fillet arcs
    (quarter arcs with unique centers) never reach the threshold.
    """
    buckets: dict[tuple[float, float, float], float] = {}
    for cx, cy, r, a0, a1 in arcs:
        key = (round(cx / tol) * tol, round(cy / tol) * tol, round(r / tol) * tol)
        sweep = (a1 - a0) % 360.0
        if sweep == 0.0:
            sweep = 360.0
        buckets[key] = buckets.get(key, 0.0) + sweep
    out = []
    for (cx, cy, r), cover in buckets.items():
        if cover >= min_coverage_deg and r > 0:
            out.append((cx, cy, r))
    return out
