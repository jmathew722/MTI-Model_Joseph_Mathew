"""Exact hole geometry from vector PDFs (PyMuPDF ``page.get_drawings()``).

A CAD-exported PDF is vector line work: circles arrive either as full-circle
curve paths or as the standard 4-cubic-Bézier circle encoding. Both are fitted
analytically (Kåsa least-squares) with a circularity tolerance, giving centers
far more exact than any raster estimate.

Everything is emitted in PDF points in a **bottom-left, y-up** frame (PyMuPDF
page coordinates are y-down; they are flipped here). Conversion to drawing
units happens in the consensus layer once the scale is anchored.

A scanned/raster PDF (no usable vector items) is flagged ``is_raster=True`` so
the pipeline falls back — explicitly, never silently — to the raster path.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from .geometry import DocGeometry, OutlineBox, SOURCE_PDF, VCircle, VText

log = logging.getLogger(__name__)

# A fitted circle is accepted when the worst sampled point deviates from the
# fitted radius by less than this fraction of the radius.
CIRCULARITY_RTOL = 0.02
# Bézier sample parameters per segment (endpoints + interior points).
_TS = (0.0, 0.25, 0.5, 0.75, 1.0)


def _bezier_points(p0, p1, p2, p3):
    """Sample a cubic Bézier at _TS."""
    pts = []
    for t in _TS:
        mt = 1.0 - t
        x = (mt ** 3) * p0.x + 3 * (mt ** 2) * t * p1.x + 3 * mt * (t ** 2) * p2.x + (t ** 3) * p3.x
        y = (mt ** 3) * p0.y + 3 * (mt ** 2) * t * p1.y + 3 * mt * (t ** 2) * p2.y + (t ** 3) * p3.y
        pts.append((x, y))
    return pts


def fit_circle(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    """Kåsa algebraic least-squares circle fit → (cx, cy, r, max_rel_residual).

    Returns None for degenerate input (collinear / too few points).
    """
    n = len(points)
    if n < 3:
        return None
    # Solve x^2+y^2 + D x + E y + F = 0 in least squares.
    sx = sy = sxx = syy = sxy = sxz = syz = sz = 0.0
    for x, y in points:
        z = x * x + y * y
        sx += x; sy += y; sz += z
        sxx += x * x; syy += y * y; sxy += x * y
        sxz += x * z; syz += y * z
    # Normal equations for [D, E, F]:
    a = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, float(n)]]
    b = [-sxz, -syz, -sz]
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
           - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    if abs(det) < 1e-12:
        return None

    def solve3(m, v):
        import copy
        out = []
        for col in range(3):
            mm = copy.deepcopy(m)
            for row in range(3):
                mm[row][col] = v[row]
            d = (mm[0][0] * (mm[1][1] * mm[2][2] - mm[1][2] * mm[2][1])
                 - mm[0][1] * (mm[1][0] * mm[2][2] - mm[1][2] * mm[2][0])
                 + mm[0][2] * (mm[1][0] * mm[2][1] - mm[1][1] * mm[2][0]))
            out.append(d / det)
        return out

    D, E, F = solve3(a, b)
    cx, cy = -D / 2.0, -E / 2.0
    r2 = cx * cx + cy * cy - F
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    worst = max(abs(math.hypot(x - cx, y - cy) - r) for x, y in points)
    return cx, cy, r, (worst / r if r > 0 else float("inf"))


def _seg_intersection(a0, a1, b0, b1, tol_deg=8.0):
    """Interior intersection of a near-horizontal and near-vertical segment,
    or None. Used to find centerline cross marks (hole center markers)."""
    def ang(p, q):
        return math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) % 180.0
    ta, tb = ang(a0, a1), ang(b0, b1)
    horiz, vert = None, None
    for (p0, p1, t) in ((a0, a1, ta), (b0, b1, tb)):
        if t < tol_deg or t > 180.0 - tol_deg:
            horiz = (p0, p1)
        elif abs(t - 90.0) < tol_deg:
            vert = (p0, p1)
    if not (horiz and vert):
        return None
    hx0, hx1 = sorted((horiz[0][0], horiz[1][0]))
    hy = (horiz[0][1] + horiz[1][1]) / 2.0
    vy0, vy1 = sorted((vert[0][1], vert[1][1]))
    vx = (vert[0][0] + vert[1][0]) / 2.0
    margin = 1e-6
    if hx0 - margin <= vx <= hx1 + margin and vy0 - margin <= hy <= vy1 + margin:
        return (vx, hy)
    return None


def extract_pdf_geometry(path: str | Path, page_number: int = 1) -> DocGeometry:
    """Extract vector circles, outline rects, centerline crosses, and positioned
    words from one PDF page (1-based ``page_number``)."""
    import fitz  # PyMuPDF

    notes: list[str] = []
    geom = DocGeometry(source_kind="pdf_vector", notes=notes)

    doc = fitz.open(str(path))
    try:
        page = doc[max(0, page_number - 1)]
        H = float(page.rect.height)

        def fy(y: float) -> float:  # y-down page frame -> y-up drawing frame
            return H - y

        drawings = page.get_drawings()
        n_vector_items = sum(len(p.get("items") or []) for p in drawings)
        segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

        for pi, pathd in enumerate(drawings):
            items = pathd.get("items") or []
            curve_samples: list[tuple[float, float]] = []
            only_curves = True
            for it in items:
                op = it[0]
                if op == "c":
                    p0, p1, p2, p3 = it[1], it[2], it[3], it[4]
                    curve_samples.extend(_bezier_points(p0, p1, p2, p3))
                elif op == "re":
                    rect = it[1]
                    geom.outlines.append(OutlineBox(float(rect.x0), fy(float(rect.y1)),
                                                    float(rect.x1), fy(float(rect.y0)),
                                                    meta=f"re#{pi}"))
                    only_curves = False
                elif op == "l":
                    p0, p1 = it[1], it[2]
                    segments.append(((float(p0.x), fy(float(p0.y))),
                                     (float(p1.x), fy(float(p1.y)))))
                    only_curves = False
                elif op == "qu":
                    only_curves = False
            # Circle candidate: a path made purely of >= 2 Bézier segments.
            if only_curves and len(items) >= 2 and curve_samples:
                fit = fit_circle([(x, fy(y)) for x, y in curve_samples])
                if fit is not None:
                    cx, cy, r, resid = fit
                    if resid <= CIRCULARITY_RTOL and r > 0.05:
                        geom.circles.append(VCircle(cx, cy, r, SOURCE_PDF, meta=f"path#{pi}"))

        # Closed 4-line rectangles are outline candidates too (borders, part
        # outlines drawn as lines). Detect axis-aligned quads from segments of
        # a single path is overkill here; the loose bbox below covers them.

        # Centerline cross marks -> flag circles whose center sits on a cross.
        crosses: list[tuple[float, float]] = []
        for i in range(len(segments)):
            for j in range(i + 1, len(segments)):
                p = _seg_intersection(segments[i][0], segments[i][1],
                                      segments[j][0], segments[j][1])
                if p is not None:
                    crosses.append(p)
        if crosses and geom.circles:
            marked = []
            for c in geom.circles:
                hit = any(math.hypot(c.cx - x, c.cy - y) <= max(1.0, 0.15 * c.r)
                          for x, y in crosses)
                marked.append(VCircle(c.cx, c.cy, c.r, c.source, c.meta, center_marked=hit)
                              if hit else c)
            geom.circles = marked

        # Positioned words (the semantic layer's positioned-OCR equivalent for
        # vector PDFs — exact text with exact coordinates, no ML OCR needed).
        for w in page.get_text("words"):
            x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
            geom.texts.append(VText(word, (x0 + x1) / 2.0, fy((y0 + y1) / 2.0)))

        # Loose bbox over all vector content as a last-resort outline candidate.
        if segments or geom.circles:
            xs, ys = [], []
            for (a, b) in segments:
                xs += [a[0], b[0]]; ys += [a[1], b[1]]
            for c in geom.circles:
                xs += [c.cx - c.r, c.cx + c.r]; ys += [c.cy - c.r, c.cy + c.r]
            geom.outlines.append(OutlineBox(min(xs), min(ys), max(xs), max(ys), meta="loose-bbox"))

        # Raster detection: effectively no vector line work but embedded images.
        if n_vector_items < 10 and page.get_images():
            geom.is_raster = True
            notes.append("RASTER: this PDF page has no usable vector line work "
                         "(scanned drawing) — falling back to raster hole detection; "
                         "positions from this page cannot be vector-exact.")
        elif not geom.circles:
            notes.append("Vector PDF parsed but no circle candidates were found.")
    finally:
        doc.close()
    return geom
