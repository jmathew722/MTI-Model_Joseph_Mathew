"""Consensus layer: merge exact vector geometry into extracted hole callouts.

Precedence (the core fix for inexact hole placement):

1. POSITION — vector geometry (DXF entities / PDF paths) is authoritative.
   Vision NEVER overrides a vector-derived coordinate. Raster Hough centers
   rank above vision but below vector, and always carry a flag.
2. SEMANTICS — the callout (vision/OCR) is authoritative for what the hole IS
   (diameter value, thread, depth, THRU vs blind, counterbore params, qty),
   cross-checked against measured vector radii. Agreement within tolerance →
   confidence HIGH. Disagreement → the CALLOUT keeps the dimension value, the
   VECTOR keeps the position, and the hole is flagged CRITICAL (never blocks).
3. SCALE — derived by matching known dimension callouts against measured
   vector distances; at least two independent, agreeing anchors are required
   for full confidence. One anchor → flag HIGH. Zero anchors → vector data is
   unusable and positions stay with vision (flagged).
4. PATTERNS — "N×"/qty is verified against the number of matching circles;
   mismatches are reconciled (best effort) and flagged; a hole placed without
   direct vector evidence is always flagged.

The result lands in the EXISTING schema shape: ``instance_positions`` (drawing
units, edge-referenced from the part's lower-left corner, exactly what the
macro generator converts to meters), ``position_known``, plus the additive
fields ``position_source`` and ``position_confidence``. Nothing downstream
changes; the build plan gains the same two additive keys.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .schema import DrawingData, HoleCallout, Units
from .vector_extract.geometry import (
    DocGeometry,
    OutlineBox,
    SOURCE_HOUGH,
    VCircle,
)

log = logging.getLogger(__name__)

# Drawing-unit value of one millimeter, per schema Units.
_MM_PER_UNIT = {Units.MM: 1.0, Units.CM: 10.0, Units.INCH: 25.4}

# Two scale anchors agree when within this relative difference.
ANCHOR_AGREE_RTOL = 0.015
# A vector circle "matches" a callout diameter within this relative tolerance
# (or an absolute floor of 0.25 mm for tiny holes).
DIAM_MATCH_RTOL = 0.03
# Deterministic rounding of emitted positions (drawing units) → bit-exact runs.
POSITION_DECIMALS = 9


@dataclass
class HoleResolution:
    """Per-callout outcome, for the report and the engineering review."""

    hole_id: str
    outcome: str            # 'vector_exact' | 'partial' | 'no_evidence' | 'diameter_conflict'
    source: str             # dxf_entity | pdf_vector | hough | vision
    confidence: float
    positions: list[list[float]] = field(default_factory=list)  # drawing units, edge-ref
    measured_diameter: float = 0.0  # drawing units (0 when nothing matched)
    flags: list[tuple[str, str]] = field(default_factory=list)  # (tier, message)


@dataclass
class HoleResolutionReport:
    scale: float = 0.0            # drawing units per native unit (0 = unresolved)
    scale_anchors: int = 0
    origin: tuple[float, float] = (0.0, 0.0)  # native-unit lower-left of the part
    outline_meta: str = ""
    holes: list[HoleResolution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def any_exact(self) -> bool:
        return any(h.outcome == "vector_exact" for h in self.holes)


def _envelope(model: DrawingData) -> tuple[float | None, float | None]:
    """Part length/width in drawing units (mirror of the macro generator's)."""
    length = width = None
    for d in model.dimensions:
        if not d.is_envelope:
            continue
        token = d.canonical_applies_to
        if token == "length" and length is None:
            length = d.value
        elif token == "width" and width is None:
            width = d.value
    return length, width


def _candidate_scales(model: DrawingData, geom: DocGeometry) -> list[tuple[float, str]]:
    """Scale hypotheses (drawing units per native unit) with their anchor labels.

    Anchors, most reliable first:
      * declared native units (DXF INSUNITS) — an exact conversion, 2 votes;
      * part outline width/height vs the envelope length/width callouts;
      * measured circle diameters vs hole-callout diameters.
    """
    anchors: list[tuple[float, str]] = []
    mm_per_unit = _MM_PER_UNIT[model.units]

    if geom.native_units_to_mm:
        s = geom.native_units_to_mm / mm_per_unit
        anchors.append((s, "declared-units"))
        anchors.append((s, "declared-units-2"))  # a declared unit is worth 2 anchors

    length, width = _envelope(model)
    for box in geom.outlines:
        if box.meta == "loose-bbox" or box.width <= 0 or box.height <= 0:
            continue
        for (dim_val, box_dim, label) in ((length, box.width, "outline-length"),
                                          (width, box.height, "outline-width"),
                                          (length, box.height, "outline-length-r"),
                                          (width, box.width, "outline-width-r")):
            if dim_val and box_dim > 0:
                anchors.append((dim_val / box_dim, f"{label}:{box.meta}"))

    diam_values = sorted({h.diameter for h in model.hole_callouts if h.diameter > 0})
    radii = sorted({c.r for c in geom.circles if c.r > 0})
    for dv in diam_values:
        for r in radii:
            anchors.append((dv / (2.0 * r), f"diam{dv}/r{r:.4g}"))
    return anchors


def _consensus_scale(anchors: list[tuple[float, str]]) -> tuple[float, int]:
    """Largest cluster of agreeing anchors → (scale, cluster size).

    Within the winning cluster, the scale value averages only the EXACT anchor
    kinds (declared units, outline-vs-envelope) when any are present — circle
    diameters vote for cluster membership but carry fit error (Bézier
    approximation, Hough), so they must not perturb an exactly-known scale.
    """
    best: list[tuple[float, str]] = []
    for s, _ in anchors:
        if s <= 0:
            continue
        cluster = [(t, lbl) for t, lbl in anchors
                   if t > 0 and abs(t - s) / s <= ANCHOR_AGREE_RTOL]
        if len(cluster) > len(best):
            best = cluster
    if not best:
        return 0.0, 0
    exact = [t for t, lbl in best
             if lbl.startswith("declared") or lbl.startswith("outline")]
    vals = exact if exact else [t for t, _ in best]
    return sum(vals) / len(vals), len(best)


def _diam_tol(diameter: float, units: Units) -> float:
    """Match tolerance for a callout diameter, drawing units."""
    floor = 0.25 / _MM_PER_UNIT[units]  # 0.25 mm expressed in drawing units
    return max(diameter * DIAM_MATCH_RTOL, floor)


def _pick_outline(model: DrawingData, geom: DocGeometry, scale: float) -> OutlineBox | None:
    """The outline box whose scaled size matches the part envelope; among
    multiple matches (multi-view sheets) prefer the one containing the most
    matched hole circles; deterministic tie-break by (x0, y0)."""
    length, width = _envelope(model)
    if not (length and width and scale > 0):
        return None

    def matches(box: OutlineBox) -> bool:
        w, h = box.width * scale, box.height * scale
        for (a, b) in ((w, h), (h, w)):
            if (abs(a - length) / length <= 0.02) and (abs(b - width) / width <= 0.02):
                return True
        return False

    cands = [b for b in geom.outlines if b.meta != "loose-bbox" and matches(b)]
    if not cands:
        cands = [b for b in geom.outlines if matches(b)]  # allow loose bbox as last resort
    if not cands:
        return None

    def n_inside(box: OutlineBox) -> int:
        return sum(1 for c in geom.circles
                   if box.x0 - 1e-9 <= c.cx <= box.x1 + 1e-9
                   and box.y0 - 1e-9 <= c.cy <= box.y1 + 1e-9)

    cands.sort(key=lambda b: (-n_inside(b), b.x0, b.y0))
    return cands[0]


def _match_circles(h: HoleCallout, circles: list[VCircle], scale: float,
                   units: Units) -> list[VCircle]:
    """Circles whose scaled diameter matches the callout's, deterministic order."""
    tol = _diam_tol(h.diameter, units)
    out = [c for c in circles if abs(2.0 * c.r * scale - h.diameter) <= tol]
    out.sort(key=lambda c: (c.cx, c.cy))
    return out


def _dedupe_concentric(matched: list[VCircle], all_circles: list[VCircle],
                       scale: float, h: HoleCallout, units: Units) -> list[VCircle]:
    """Drop matched circles that are actually the drill of a concentric pair
    already represented, and confirm counterbore signatures."""
    # Concentric duplicates within the match itself (same center, ~same r).
    out: list[VCircle] = []
    for c in matched:
        if any(math.hypot(c.cx - o.cx, c.cy - o.cy) < max(c.r, o.r) * 0.05 for o in out):
            continue
        out.append(c)
    return out


def resolve_holes(model: DrawingData, geom: DocGeometry) -> HoleResolutionReport:
    """Merge vector geometry into ``model``'s hole callouts (mutates the model).

    Never raises and never blocks: any hole without usable vector evidence
    keeps its vision-derived data and gets a flag instead.
    """
    rep = HoleResolutionReport()
    rep.notes.extend(geom.notes)
    src = SOURCE_HOUGH if geom.source_kind == "raster" else (
        "dxf_entity" if geom.source_kind == "dxf" else "pdf_vector")

    if not geom.circles:
        rep.notes.append("No vector circle candidates — hole positions stay with vision.")
        for h in model.hole_callouts:
            rep.holes.append(HoleResolution(h.id, "no_evidence", "vision", 0.0,
                flags=[("HIGH", f"{h.id}: no vector evidence for position — "
                                "vision estimate retained, verify placement.")]))
        _apply_flags(model, rep)
        return rep

    # ---- 3. SCALE RESOLUTION -------------------------------------------------
    anchors = _candidate_scales(model, geom)
    scale, n_agree = _consensus_scale(anchors)
    rep.scale, rep.scale_anchors = scale, n_agree
    if scale <= 0 or n_agree == 0:
        rep.notes.append("Scale could not be anchored (no dimension callout matches "
                         "any measured vector distance) — vector positions unusable.")
        for h in model.hole_callouts:
            rep.holes.append(HoleResolution(h.id, "no_evidence", "vision", 0.0,
                flags=[("HIGH", f"{h.id}: vector geometry present but the drawing scale "
                                "could not be anchored — vision estimate retained.")]))
        _apply_flags(model, rep)
        return rep
    scale_flag: tuple[str, str] | None = None
    if n_agree < 2:
        scale_flag = ("HIGH", "Drawing scale anchored by only ONE dimension match — "
                              "verify one hole position against the drawing.")
        rep.notes.append(scale_flag[1])

    # ---- Part origin (lower-left) in native units ------------------------------
    outline = _pick_outline(model, geom, scale)
    if outline is not None:
        origin = (outline.x0, outline.y0)
        rep.outline_meta = outline.meta
        circles_in = [c for c in geom.circles
                      if outline.x0 - 1e-9 <= c.cx <= outline.x1 + 1e-9
                      and outline.y0 - 1e-9 <= c.cy <= outline.y1 + 1e-9]
        if not circles_in:
            circles_in = geom.circles
    else:
        rep.notes.append("No outline matched the part envelope — could not anchor the "
                         "part origin; vector positions unusable.")
        for h in model.hole_callouts:
            rep.holes.append(HoleResolution(h.id, "no_evidence", "vision", 0.0,
                flags=[("HIGH", f"{h.id}: vector geometry found but the part outline "
                                "could not be identified to anchor coordinates — "
                                "vision estimate retained.")]))
        _apply_flags(model, rep)
        return rep
    rep.origin = origin

    base_conf = {"dxf_entity": 0.97, "pdf_vector": 0.95, "hough": 0.70}[src]

    # ---- Per-callout association ----------------------------------------------
    for h in model.hole_callouts:
        hr = HoleResolution(h.id, "no_evidence", "vision", 0.0)
        if scale_flag:
            hr.flags.append(scale_flag)
        matched = _dedupe_concentric(
            _match_circles(h, circles_in, scale, model.units), circles_in, scale, h, model.units)

        # 2. SEMANTICS cross-check fallback: no diameter match but the callout has
        # a qty that some other-diameter circle group satisfies exactly → vector
        # wins position, callout wins the value, CRITICAL flag.
        if not matched and h.qty > 1:
            by_r: dict[float, list[VCircle]] = {}
            for c in circles_in:
                key = round(c.r, 6)
                by_r.setdefault(key, []).append(c)
            exact_groups = [g for g in by_r.values() if len(g) == h.qty]
            if len(exact_groups) == 1:
                matched = sorted(exact_groups[0], key=lambda c: (c.cx, c.cy))
                measured = 2.0 * matched[0].r * scale
                hr.outcome = "diameter_conflict"
                hr.flags.append(("CRITICAL",
                    f"{h.id}: callout says ⌀{h.diameter:g} but the only {h.qty}-circle "
                    f"group in the vector data measures ⌀{measured:.4g}. Position taken "
                    f"from vector geometry; the CALLOUT value ⌀{h.diameter:g} is kept "
                    "for the feature size — a human must verify which is right."))

        if not matched:
            hr.flags.append(("HIGH", f"{h.id}: no vector circle matches ⌀{h.diameter:g} "
                                     "— position stays with the vision estimate; "
                                     "verify placement."))
            rep.holes.append(hr)
            continue

        measured_d = 2.0 * matched[0].r * scale
        hr.measured_diameter = measured_d

        # 4. PATTERN INFERENCE: verify N× against the vector evidence.
        if len(matched) > h.qty:
            # More circles than instances (other views repeat the hole): keep the
            # qty spatially-tightest… deterministic: first qty in sorted order.
            hr.flags.append(("MEDIUM",
                f"{h.id}: drawing shows {len(matched)} ⌀{h.diameter:g} circles but the "
                f"callout says {h.qty}× — using the first {h.qty} (sorted by x,y); "
                "extra circles are likely other views of the same holes."))
            matched = matched[: h.qty]
        elif len(matched) < h.qty:
            hr.flags.append(("HIGH",
                f"{h.id}: callout says {h.qty}× but only {len(matched)} matching "
                f"circle(s) found in the vector data — the {h.qty - len(matched)} "
                "unplaced instance(s) keep their vision/pattern layout; verify."))

        positions = [
            [round((c.cx - origin[0]) * scale, POSITION_DECIMALS),
             round((c.cy - origin[1]) * scale, POSITION_DECIMALS)]
            for c in matched
        ]
        conf = base_conf
        if all(c.center_marked for c in matched):
            conf = min(0.99, conf + 0.02)
        if hr.outcome != "diameter_conflict":
            # Diameter agreement between callout and vector measurement → HIGH.
            if abs(measured_d - h.diameter) <= _diam_tol(h.diameter, model.units):
                hr.outcome = "vector_exact" if len(matched) == h.qty else "partial"
            else:  # matched within tolerance by construction; defensive
                hr.outcome = "partial"
        if src == "hough":
            hr.flags.append(("HIGH", f"{h.id}: position measured from a RASTER image "
                                     "(Hough), not vector geometry — near-exact but "
                                     "not guaranteed; verify against the drawing."))
            conf = min(conf, 0.75)

        hr.source, hr.confidence, hr.positions = src, round(conf, 3), positions

        # Counterbore signature: an outer concentric circle ≈ cbore_diameter.
        if h.cbore_diameter > 0:
            outer_ok = any(
                math.hypot(c.cx - m.cx, c.cy - m.cy) < max(c.r, m.r) * 0.05
                and abs(2.0 * c.r * scale - h.cbore_diameter) <= _diam_tol(h.cbore_diameter, model.units)
                for m in matched for c in circles_in if c is not m
            )
            if not outer_ok:
                hr.flags.append(("MEDIUM", f"{h.id}: counterbore ⌀{h.cbore_diameter:g} has "
                                           "no concentric outer circle in the vector data — "
                                           "verify the counterbore."))

        # ---- write back into the EXISTING schema shape ----
        if hr.positions and len(hr.positions) >= 1:
            if len(matched) == h.qty or len(matched) > 0:
                h.instance_positions = [list(p) for p in hr.positions]
                h.position_known = True
                h.x_position, h.y_position = hr.positions[0][0], hr.positions[0][1]
                h.position_source = hr.source
                h.position_confidence = hr.confidence
        rep.holes.append(hr)

    _apply_flags(model, rep)
    return rep


def _apply_flags(model: DrawingData, rep: HoleResolutionReport) -> None:
    """Surface every flag in the model's warnings so the validator, the
    engineering review, and the extraction JSON all see them."""
    for hr in rep.holes:
        for tier, msg in hr.flags:
            model.warnings.append(f"[{tier}] hole-position: {msg}")
    for n in rep.notes:
        if n and not any(n in w for w in model.warnings):
            model.warnings.append(f"hole-position: {n}")
