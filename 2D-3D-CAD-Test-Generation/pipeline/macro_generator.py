"""SolidWorks VBA macro generation (Phase 2, build engine "vba").

Turns verified :class:`~pipeline.schema.DrawingData` into a self-contained
output package the user can carry to ANY Windows machine with SolidWorks —
no Python required there:

    output/<PartName>/
    ├── <PartName>_extraction.json          # full Phase 1 extraction
    ├── <PartName>_verification_report.txt  # Phase 1 verification report
    ├── <PartName>_build_plan.json          # ordered feature build plan
    ├── macros/
    │   ├── README.md                       # how to run the macros in SolidWorks
    │   ├── 00_setup.vba
    │   ├── 01_<F001_desc>.vba
    │   ├── ...
    │   ├── NN_fillets_chamfers.vba         # (when fillets/chamfers exist)
    │   └── ZZ_final_verify.vba
    └── logs/                               # build_log.txt is appended here by the macros

Generation discipline:
  * VBA uses **named enum constants** (``swEndConditions_e.swEndCondBlind`` …) —
    SolidWorks VBA references the SwConst type library by default, so names
    resolve and no numeric constants are guessed.
  * Verified call shapes only: ``FeatureExtrusion3`` (signature confirmed against
    SolidWorks API docs/examples) and ``FeatureCut4`` (mirrors the working call in
    pipeline/solidworks_builder.py). Anything we could not ground in a documented
    pattern is emitted with a ``' TODO: VERIFY API CALL`` block, never invented
    silently.
  * Every dimension is written as ``<drawing value> * UNIT_FACTOR`` so macros stay
    traceable to the drawing; UNIT_FACTOR converts to meters (SolidWorks API unit).
  * One macro per feature; each appends PASS/FAIL to ``logs/build_log.txt``
    (path derived from the macro's own location) and stops with a message box on
    failure — never build on a broken state.
  * PROHIBITED feature types (loft, sweep, shell, …) are never generated —
    they're flagged in the build plan and skipped.
  * Holes are generated as exact circle sketches + a single cut (positions baked
    in), which is far more robust than scripted Hole Wizard or pattern features.
    Counterbores get a second concentric blind cut. Tapped holes get a cosmetic-
    thread step marked TODO-VERIFY (never modeled helically).

Public entry point: :func:`generate_macro_package`.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pipeline.macro_audit import audit_package, write_audit_report
from pipeline.schema import (
    Dimension,
    DrawingData,
    Feature,
    FeatureType,
    HoleCallout,
    HoleType,
    PatternKind,
    Units,
)
from utils.logger import get_logger

log = get_logger()

UNIT_FACTORS = {Units.MM: 0.001, Units.CM: 0.01, Units.INCH: 0.0254}
UNIT_SYSTEM_ENUM = {  # document unit system, by drawing units
    Units.MM: "swUnitSystem_e.swUnitSystem_MMGS",
    Units.CM: "swUnitSystem_e.swUnitSystem_MMGS",
    Units.INCH: "swUnitSystem_e.swUnitSystem_IPS",
}
# Sketch-plane names keyed by a feature's sketch_plane / source-view label.
# side & second_side build on the Right Plane; bottom on the Top Plane (opposite
# face) — through-cuts are direction-proof, so the cut reaches material either way.
PLANE_NAMES = {
    "front": "Front Plane",
    "top": "Top Plane",
    "right": "Right Plane",
    "side": "Right Plane",
    "second_side": "Right Plane",
    "second side": "Right Plane",
    "left": "Right Plane",
    "bottom": "Top Plane",
    "back": "Front Plane",
    "rear": "Front Plane",
}
# 1-based position of each standard plane in a default template's feature tree
# (used as a name-independent fallback when selecting by name fails).
PLANE_INDEX = {"Front Plane": 1, "Top Plane": 2, "Right Plane": 3}

# Feature types we can emit reliable macros for.
SUPPORTED = {
    FeatureType.EXTRUDE_BOSS,
    FeatureType.EXTRUDE_CUT,
    FeatureType.HOLE,
    FeatureType.FILLET,
    FeatureType.CHAMFER,
    FeatureType.PATTERN,
    FeatureType.MIRROR,
    FeatureType.THREAD,   # cosmetic thread only (TODO-marked)
    FeatureType.REVOLVE,  # real revolve when a profile exists, else skeleton + needs_review
}
# Schema types that are prohibited outright (plus anything not in SUPPORTED).
PROHIBITED = {FeatureType.SHELL}


class MacroGenerationError(Exception):
    """Raised when macro generation cannot proceed (e.g. BLOCKED data)."""


@dataclass
class BuildStep:
    seq: int
    macro_file: str
    feature_id: str
    feature_type: str
    description: str
    status: str  # generated | needs_review | skipped_prohibited | merged
    dimensions: dict[str, float] = field(default_factory=dict)
    notes: str = ""
    # --- Stage 2.5 / self-contained build-plan fields (zero cross-referencing) ---
    dimensions_meters: dict[str, float] = field(default_factory=dict)
    positions_xy: list[list[float]] = field(default_factory=list)        # drawing units
    positions_xy_meters: list[list[float]] = field(default_factory=list)
    sketch_plane: str = ""
    parent_feature_id: str = ""
    depth_type: str = ""           # blind | through_all | ""
    flags: list[dict] = field(default_factory=list)
    requires_input: bool = False
    auto_select_strategy: str = ""
    expected_edge_count: int = 0
    edge_selection_note: str = ""
    assumption_made: bool = False
    assumption_confidence: float = 1.0
    flag_tier: str = "HIGH"
    # Additive (vector hole extraction): where hole positions came from.
    position_source: str = ""          # dxf_entity | pdf_vector | hough | vision | ""
    position_confidence: float = 0.0   # 0..1; 0.0 when not applicable
    # Additive: canonical circular-pattern schema (only on circular_pattern steps).
    circular_pattern: dict = field(default_factory=dict)


@dataclass
class MacroPackage:
    root: Path
    macros_dir: Path
    extraction_json: Path
    verification_report: Path
    build_plan_json: Path
    resolved_extraction_json: Optional[Path] = None
    steps: list[BuildStep] = field(default_factory=list)
    skipped: list[BuildStep] = field(default_factory=list)
    needs_review: list[BuildStep] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return cleaned or "part"


def _vba_name(text: str, limit: int = 40) -> str:
    """A VBA-identifier-safe fragment from a description."""
    frag = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:limit]
    return frag or "feature"


def _vba_str(text: str, limit: int = 120) -> str:
    """Make model-supplied text safe inside a VBA string literal/comment.

    Doubles quotes (VBA escaping), strips newlines and non-ASCII (the VBA
    editor's ANSI handling mangles Unicode), and bounds the length.
    """
    cleaned = str(text).replace('"', '""')
    cleaned = re.sub(r"[\r\n]+", " ", cleaned)
    cleaned = cleaned.encode("ascii", errors="replace").decode("ascii")
    return cleaned[:limit]


def _v(value: float) -> str:
    """Format a drawing-unit value as a VBA literal."""
    return f"{value:.6g}"


def _dims_map(model: DrawingData, feature: Feature) -> dict[str, float]:
    """Feature's dimensions in DRAWING units, keyed by applies_to (or type)."""
    out: dict[str, float] = {}
    ids = list(feature.related_dimensions)
    if feature.depth_dimension_id and feature.depth_dimension_id not in ids:
        ids.append(feature.depth_dimension_id)
    for did in ids:
        d = model.dimension_by_id(did)
        if d is None:
            continue
        # Prefer a canonical token from the (often verbose) applies_to label so
        # "thru hole diameter (4 places)" still resolves to "hole_diameter"; fall
        # back to the dimension type. Fixes failure class E010.
        key = d.canonical_applies_to or (d.applies_to or d.type.value).lower().strip()
        out.setdefault(key, d.value)
    if feature.depth_dimension_id:
        d = model.dimension_by_id(feature.depth_dimension_id)
        if d is not None:
            out.setdefault("depth", d.value)
    return out


def _depth_of(dims: dict[str, float]) -> Optional[float]:
    for key in ("depth", "height", "thickness", "length_depth"):
        if dims.get(key):
            return dims[key]
    return None


def _plane_for(feature: Feature) -> str:
    return PLANE_NAMES.get((feature.sketch_plane or "front").lower().strip(), "Front Plane")


def _envelope(model: DrawingData) -> tuple[Optional[float], Optional[float]]:
    """The part's length/width envelope in drawing units (None when not extracted)."""
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


def _effective_spacing(model: DrawingData, h: HoleCallout) -> tuple[float, int]:
    """Best available (spacing, qty) for a callout, in drawing units.

    Prefer the callout's own ``pattern_spacing``; otherwise fall back to a
    STRUCTURED ``equal_spacing`` relationship keyed by the callout's feature_ref.
    Returns ``(0.0, qty)`` when no spacing can be grounded in extracted data — so
    no positions are ever invented. Free-text descriptions are never parsed.
    """
    if h.pattern_spacing and h.pattern_spacing > 0:
        return h.pattern_spacing, h.qty
    if h.feature_ref:
        for s in model.relationships.equal_spacing:
            if s.feature_ref == h.feature_ref and s.spacing_value > 0:
                return s.spacing_value, max(h.qty, s.qty)
    return 0.0, h.qty


def _corner_frame_shift(model: DrawingData, positions: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Shift positions into the corner-origin (lower-left) frame when they look
    CENTER-referenced.

    Drawings dimension hole centers either from a part edge (the corner frame this
    pipeline builds in) or from a centerline/center datum. A negative coordinate is
    an unambiguous signal of center-referencing — the corner frame has no negatives —
    so when the envelope is known we re-origin by half the envelope, putting the
    holes where they were drawn instead of off the part.
    """
    if not positions:
        return positions
    length, width = _envelope(model)
    if not (length and width):
        return positions
    min_x = min(p[0] for p in positions)
    min_y = min(p[1] for p in positions)
    if min_x < 0 or min_y < 0:
        return [(x + length / 2.0, y + width / 2.0) for x, y in positions]
    return positions


def _circular_positions(model: DrawingData, h: HoleCallout) -> Optional[list[tuple[float, float]]]:
    """Instance centers for a CIRCULAR (bolt-circle) pattern, or None if it isn't
    one / lacks a bolt-circle diameter.

    The qty instances are placed evenly around the bolt circle starting at
    ``start_angle`` (degrees, CCW from +X). The center comes from
    ``bolt_circle_center`` when given, else the part-envelope center — so a bolt
    pattern lands as a real ring of holes instead of the single-instance fallback.
    """
    if h.pattern != PatternKind.CIRCULAR or h.bolt_circle_diameter <= 0 or h.qty < 1:
        return None
    radius = h.bolt_circle_diameter / 2.0
    if len(h.bolt_circle_center) == 2:
        cx, cy = h.bolt_circle_center[0], h.bolt_circle_center[1]
    else:
        # No explicit center: use the envelope center when known, else place the
        # bolt circle in the corner frame (center at the radius) so every hole
        # stays non-negative. The COM builder re-centers a concentric pattern on
        # the actual body, so this only has to be a valid corner-frame placement.
        length, width = _envelope(model)
        cx = (length / 2.0) if length else radius
        cy = (width / 2.0) if width else radius
    start = math.radians(h.start_angle)
    step = 2.0 * math.pi / h.qty
    return [
        (cx + radius * math.cos(start + i * step), cy + radius * math.sin(start + i * step))
        for i in range(h.qty)
    ]


def _hole_positions(model: DrawingData, h: HoleCallout) -> list[tuple[float, float]]:
    """Instance centers in the DRAWING FRAME (base plate lower-left corner at origin).

    Known positions are used as-is — drawings dimension hole centers from the
    part edges, which is exactly this frame. When positions are unknown but a
    spacing can be GROUNDED in extracted data (the callout's pattern_spacing or a
    structured equal_spacing relationship), instances are laid out as a centered
    row about the plate envelope. A grounded circular (bolt-circle) pattern is laid
    out as a ring. With no such evidence, a single instance is placed at the
    envelope center and the macro flags POSITION ASSUMED — positions are never
    invented from free text.
    """
    # Most reliable: explicit per-instance centers read straight from the drawing
    # (re-origined to the corner frame if they were dimensioned from a centerline).
    if h.instance_positions:
        return _corner_frame_shift(model, [(p[0], p[1]) for p in h.instance_positions if len(p) == 2])

    # Circular (bolt-circle) pattern grounded in a bolt-circle diameter.
    circular = _circular_positions(model, h)
    if circular is not None:
        return circular

    length, width = _envelope(model)
    ecx = (length / 2.0) if length else 0.0
    ecy = (width / 2.0) if width else 0.0
    spacing, qty = _effective_spacing(model, h)
    # A grounded spacing lays out a centered row for linear/unspecified patterns.
    # A circular pattern with no bolt-circle diameter falls through to the
    # single-instance fallback rather than being guessed.
    linear_like = h.pattern in (PatternKind.LINEAR, PatternKind.NONE)
    if linear_like and qty > 1 and spacing > 0:
        if h.position_known:
            x0, y0 = h.x_position, h.y_position
        else:
            span = (qty - 1) * spacing
            x0, y0 = ecx - span / 2.0, ecy
        return _corner_frame_shift(model, [(x0 + i * spacing, y0) for i in range(qty)])
    # Single position (or qty>1 with no grounded spacing — macro comments flag it).
    if h.position_known:
        return _corner_frame_shift(model, [(h.x_position, h.y_position)])
    return [(ecx, ecy)]


def revolve_sketch_points(
    profile: list[list[float]],
) -> tuple[list[tuple[float, float]], tuple[float, float]]:
    """Closed sketch polygon for a revolve half-profile, plus the axis endpoints.

    Input is the OUTER boundary as ordered ``[axial, radial]`` points; in the
    sketch these map to ``(x=axial, y=radial)``. The region is closed back to the
    revolve axis (radial = 0): the polygon drops from the last point to the axis,
    runs along the axis, and back up to the first point (the sketch closes the
    final segment to the start). Returns ``(closed_points, (x_min, x_max))`` where
    the centerline runs from ``x_min`` to ``x_max`` at y = 0.

    Raises MacroGenerationError when fewer than two points are supplied.
    """
    pts = [(float(a), float(r)) for a, r in profile]
    if len(pts) < 2:
        raise MacroGenerationError("revolve profile needs at least 2 points.")
    xs = [p[0] for p in pts]
    x_min, x_max = min(xs), max(xs)
    closed = list(pts)
    if pts[-1][1] > 0:                 # drop the last point to the axis
        closed.append((pts[-1][0], 0.0))
    if pts[0][1] > 0:                  # and return along the axis to the start
        closed.append((pts[0][0], 0.0))
    return closed, (x_min, x_max)


# --------------------------------------------------------------------------- #
# Stage 2.5 flags → VBA, and self-contained build-plan enrichment
# --------------------------------------------------------------------------- #
def _to_meters(value: float, units: Units) -> float:
    """Drawing-unit value → meters using this part's unit factor."""
    return round(value * UNIT_FACTORS[units], 9)


def _dims_in_meters(dims: dict[str, float], units: Units) -> dict[str, float]:
    """Convert a drawing-unit dims map to meters, leaving counts (qty) alone."""
    out: dict[str, float] = {}
    for k, v in dims.items():
        if k == "qty":
            out[k] = v
        else:
            out[k] = _to_meters(v, units)
    return out


def _collect_step_flags(model: DrawingData, feature: Feature, resolution) -> list[dict]:
    """Build-plan flags affecting this feature's macro: the feature's own
    position flag, every flag on a dimension the feature consumes, and (for hole
    features) any unknown-position hole flags. Empty list when nothing applies."""
    if resolution is None:
        return []
    flags: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(flag: dict) -> None:
        key = (flag.get("dimension_id", ""), flag.get("human_note", ""))
        if key not in seen:
            seen.add(key)
            flags.append(flag)

    fres = resolution.feature(feature.id)
    if fres is not None and fres.flag_tier in ("MEDIUM", "LOW", "CRITICAL"):
        from pipeline.resolver import behavior_for_tier

        _add({
            "dimension_id": feature.id, "flag_tier": fres.flag_tier,
            "human_note": fres.human_note, "macro_behavior": behavior_for_tier(fres.flag_tier),
        })

    dim_ids = set(feature.related_dimensions)
    if feature.depth_dimension_id:
        dim_ids.add(feature.depth_dimension_id)
    for did in dim_ids:
        dres = resolution.dim(did)
        if dres is not None and dres.flag_tier in ("MEDIUM", "LOW", "CRITICAL"):
            from pipeline.resolver import behavior_for_tier

            _add({
                "dimension_id": did, "flag_tier": dres.flag_tier,
                "human_note": dres.human_note, "macro_behavior": behavior_for_tier(dres.flag_tier),
            })

    if feature.type in (FeatureType.HOLE, FeatureType.THREAD):
        h = model.hole_callout_for_feature(feature.id)
        if h is not None and not h.instance_positions and not h.position_known:
            _add({
                "dimension_id": h.id, "flag_tier": "LOW",
                "human_note": (
                    f"POSITION ASSUMED for hole {h.id}: centered/laid-out from the envelope; "
                    f"verify hole locations in SolidWorks."
                ),
                "macro_behavior": "msgbox_on_run",
            })
    return flags


def _flag_vba_block(step_name: str, flags: list[dict]) -> str:
    """Emit the VBA the spec mandates per flag tier, run at the TOP of a macro.

    HIGH      → a ' NOTE comment only (no interruption).
    MEDIUM    → MsgBox vbInformation ("Review recommended").
    LOW       → MsgBox vbExclamation ("Verify before continuing").
    CRITICAL  → a banner comment block + a confirmation dialog the user must
                acknowledge (Cancel logs and exits the macro).
    """
    if not flags:
        return ""
    lines: list[str] = ["    ' --- Stage 2.5 assumption flags ---"]
    for fl in flags:
        tier = fl.get("flag_tier", "HIGH")
        note = _vba_str(fl.get("human_note", ""), limit=240)
        did = fl.get("dimension_id", "")
        if tier == "HIGH":
            lines.append(f"    ' NOTE [{did}]: {note}")
        elif tier == "MEDIUM":
            lines.append(f'    MsgBox "{note}", vbInformation, "Review recommended ({did})"')
        elif tier == "LOW":
            lines.append(f'    MsgBox "{note}", vbExclamation, "Verify before continuing ({did})"')
        else:  # CRITICAL
            lines.append(f"    ' !! CRITICAL ASSUMPTION [{did}] — VERIFY BEFORE REBUILD")
            lines.append(f"    ' !! {note}")
            lines.append(
                f'    If MsgBox("CRITICAL ASSUMPTION [{did}]:" & vbCrLf & "{note}" & vbCrLf & vbCrLf '
                f'& "Click OK to build with this assumption, or Cancel to stop.", '
                f'vbOKCancel + vbExclamation, "Critical assumption — {did}") = vbCancel Then'
            )
            lines.append(f'        LogResult "STOP", "{step_name}", "User cancelled at critical assumption {did}"')
            lines.append("        Exit Sub")
            lines.append("    End If")
    return "\n".join(lines) + "\n"


def _depth_type_for(model: DrawingData, feature: Feature, dims: dict[str, float]) -> str:
    """Classify a cut/hole's depth as ``through_all`` or ``blind`` for the plan."""
    if feature.type in (FeatureType.HOLE, FeatureType.THREAD):
        h = model.hole_callout_for_feature(feature.id)
        if h is not None:
            return "through_all" if h.thru else "blind"
    if feature.type == FeatureType.EXTRUDE_CUT:
        return "blind" if _depth_of(dims) else "through_all"
    return ""


def _worst_resolution(model: DrawingData, feature: Feature, resolution) -> tuple[bool, float, str]:
    """(assumption_made, confidence, flag_tier) for a feature: the worst across
    the feature's own resolution and every dimension it consumes."""
    if resolution is None:
        return False, 1.0, "HIGH"
    from pipeline.resolver import worst_tier

    tiers: list[str] = []
    confs: list[float] = []
    assumed = False
    fres = resolution.feature(feature.id)
    if fres is not None:
        tiers.append(fres.flag_tier)
    dim_ids = set(feature.related_dimensions)
    if feature.depth_dimension_id:
        dim_ids.add(feature.depth_dimension_id)
    for did in dim_ids:
        dres = resolution.dim(did)
        if dres is not None:
            tiers.append(dres.flag_tier)
            confs.append(dres.assumption_confidence)
            assumed = assumed or dres.assumption_made
    tier = worst_tier(*tiers) if tiers else "HIGH"
    conf = min(confs) if confs else (0.95 if tier == "HIGH" else 0.6)
    return assumed, conf, tier


def _enrich_feature_step(step: BuildStep, model: DrawingData, feature: Feature,
                         resolution, step_flags: list[dict]) -> None:
    """Populate the self-contained build-plan fields on a feature step."""
    step.dimensions_meters = _dims_in_meters(step.dimensions, model.units)
    step.sketch_plane = (feature.sketch_plane or "front").lower().strip() or "front"
    step.parent_feature_id = feature.parent_feature or ""
    step.depth_type = _depth_type_for(model, feature, step.dimensions)
    step.flags = step_flags

    if feature.type in (FeatureType.HOLE, FeatureType.THREAD):
        h = model.hole_callout_for_feature(feature.id)
        if h is not None:
            pts = _hole_positions(model, h)
            step.positions_xy = [[round(x, 6), round(y, 6)] for x, y in pts]
            step.positions_xy_meters = [
                [_to_meters(x, model.units), _to_meters(y, model.units)] for x, y in pts
            ]
            # Additive provenance from the vector hole-extraction stage.
            step.position_source = h.position_source or ("vision" if h.position_known else "")
            step.position_confidence = h.position_confidence
    elif feature.type in (FeatureType.EXTRUDE_BOSS, FeatureType.EXTRUDE_CUT):
        # Self-contained sketch anchor (same rules as _macro_extrude): circle =
        # center, rectangle = lower-left corner. Consumers (e.g. the CadQuery
        # pre-validation) must never re-derive placement from the extraction.
        if step.dimensions.get("diameter") or step.dimensions.get("hole_diameter"):
            if feature.position_known:
                cx, cy = feature.offset_x, feature.offset_y
            else:
                length, width = _envelope(model)
                cx, cy = (length or 0.0) / 2.0, (width or 0.0) / 2.0
        else:
            cx, cy = ((feature.offset_x, feature.offset_y)
                      if feature.position_known else (0.0, 0.0))
        step.positions_xy = [[round(cx, 6), round(cy, 6)]]
        step.positions_xy_meters = [[_to_meters(cx, model.units),
                                     _to_meters(cy, model.units)]]

    step.assumption_made, step.assumption_confidence, step.flag_tier = \
        _worst_resolution(model, feature, resolution)


def _step_to_dict(s: BuildStep) -> dict[str, Any]:
    """One build-plan step as a fully self-contained dict.

    Carries every legacy key (so existing consumers/tests keep working) plus the
    self-contained Stage-2.5 fields: a macro generator can produce this step's
    ``.vba`` from this object alone — no cross-referencing the extraction JSON.
    """
    return {
        # --- legacy keys (unchanged) ---
        "seq": s.seq,
        "macro_file": s.macro_file,
        "feature_id": s.feature_id,
        "type": s.feature_type,
        "description": s.description,
        "status": s.status,
        "dimensions_drawing_units": s.dimensions,
        "notes": s.notes,
        # --- self-contained additions ---
        "dimensions_meters": s.dimensions_meters,
        "positions_xy": s.positions_xy,
        "positions_xy_meters": s.positions_xy_meters,
        "sketch_plane": s.sketch_plane,
        "parent_feature_id": s.parent_feature_id,
        "depth_type": s.depth_type,
        "flags": s.flags,
        "requires_input": s.requires_input,
        "auto_select_strategy": s.auto_select_strategy,
        "expected_edge_count": s.expected_edge_count,
        "edge_selection_note": s.edge_selection_note,
        "assumption_made": s.assumption_made,
        "assumption_confidence": round(s.assumption_confidence, 3),
        "flag_tier": s.flag_tier,
        # --- additive: vector hole-extraction provenance ---
        "position_source": s.position_source,
        "position_confidence": round(s.position_confidence, 3),
        # --- additive: canonical circular-pattern schema (Part 2a) ---
        **({"circular_pattern": s.circular_pattern} if s.circular_pattern else {}),
    }


def _build_plan_dict(model: DrawingData, pkg: MacroPackage, unit_factor: float,
                     audit, resolution) -> dict[str, Any]:
    """Assemble the self-contained ``build_plan.json`` (superset of the v2 schema).

    The header states the coordinate convention explicitly so a downstream macro
    generator never has to infer the origin. When a Stage-2.5 ``resolution`` is
    present, a ``resolution_summary`` block is included; otherwise the plan is the
    backward-compatible v2 shape with the extra per-step fields defaulted.
    """
    plan: dict[str, Any] = {
        "part": model.display_name,
        "units": model.units.value,
        "unit_factor_to_meters": unit_factor,
        "coordinate_origin": "lower_left_corner_of_base_solid",
        "x_direction": "positive_right",
        "y_direction": "positive_up",
        "confidence": model.confidence,
        "audit": audit.to_dict(),
    }
    if resolution is not None:
        sm = resolution.summary
        plan["resolution_summary"] = {
            "total_dimensions": sm.total_dimensions,
            "assumptions_made": sm.assumptions_made,
            "critical_flags": sm.critical_flags,
            "low_flags": sm.low_flags,
            "medium_flags": sm.medium_flags,
            "high_flags": sm.high_flags,
            "rebuild_confidence": round(sm.rebuild_confidence, 3),
            "plain_english": sm.plain_english,
        }
    plan["steps"] = [_step_to_dict(s) for s in pkg.steps]
    plan["skipped_prohibited"] = [s.feature_id for s in pkg.skipped]
    plan["needs_review"] = [s.feature_id for s in pkg.needs_review]
    # Severity-ranked engineering review (CRITICAL..LOW, most urgent first) —
    # the same items are written per part as <Part>_engineering_review.txt.
    from pipeline.engineering_review import build_review_items

    plan["engineering_review"] = build_review_items(resolution=resolution, pkg=pkg)
    return plan


# --------------------------------------------------------------------------- #
# VBA scaffolding shared by every macro
# --------------------------------------------------------------------------- #
# Shared helper Subs/Functions — defined ONCE here so the per-feature macros and
# the single-run RUN_ALL.vba cannot drift apart.
_HELPERS_VBA = """
' --- Append a PASS/FAIL line to ..\\logs\\build_log.txt next to the macros folder ---
Sub LogResult(status As String, step As String, detail As String)
    On Error Resume Next
    Dim macroPath As String, logPath As String, f As Integer
    macroPath = swApp.GetCurrentMacroPathName
    logPath = Left$(macroPath, InStrRev(macroPath, "\\")) & "..\\logs\\build_log.txt"
    f = FreeFile
    Open logPath For Append As #f
    Print #f, Format$(Now, "yyyy-mm-dd hh:nn:ss") & "  [" & status & "]  " & step & _
        IIf(Len(detail) > 0, "  -- " & detail, "")
    Close #f
    On Error GoTo 0
End Sub

' --- Verify a solid body exists; log and report its bounding box ---
Function VerifySolidBody(step As String) As Boolean
    Dim swPart As SldWorks.PartDoc
    Dim vBodies As Variant
    Set swPart = swModel
    vBodies = swPart.GetBodies2(swBodyType_e.swSolidBody, True)
    If IsEmpty(vBodies) Then
        VerifySolidBody = False
        LogResult "FAIL", step, "No solid body present after feature"
    Else
        ' Bounding box read from the solid body itself (IBody2::GetBodyBox) -
        ' ModelDoc2 exposes no whole-model bounding-box call in VBA.
        Dim swBody As SldWorks.Body2
        Dim vBox As Variant
        Set swBody = vBodies(0)
        vBox = swBody.GetBodyBox
        LogResult "PASS", step, "Solid body OK; bbox(drawing units) " & _
            Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
            Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
            Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000")
        VerifySolidBody = True
    End If
End Function

' --- Append a machine-readable result line to ..\\logs\\macro_result.json (JSON Lines) ---
' Every feature-creation outcome is recorded here (feature name -> success/fail)
' so the web UI / FastAPI side can surface the EXACT failing feature instead of a
' generic pipeline exit code.
Sub WriteMacroResult(featureName As String, status As String, detail As String)
    On Error Resume Next
    Dim macroPath As String, p As String, f As Integer, q As String
    q = Chr$(34)
    macroPath = swApp.GetCurrentMacroPathName
    p = Left$(macroPath, InStrRev(macroPath, "\\")) & "..\\logs\\macro_result.json"
    f = FreeFile
    Open p For Append As #f
    Print #f, "{" & q & "feature" & q & ": " & q & featureName & q & ", " & _
        q & "status" & q & ": " & q & status & q & ", " & _
        q & "detail" & q & ": " & q & Replace(Replace(detail, "\\", "/"), q, "'") & q & "}"
    Close #f
    On Error GoTo 0
End Sub

' --- Create a circular pattern with the exact selection contract the API requires ---
' Signature pulled from the INSTALLED SolidWorks type library (sldworks.tlb,
' IFeatureManager::FeatureCircularPattern5, dispid 261; see the local API help
' topic "FeatureCircularPattern5 Method (IFeatureManager)"):
'   FeatureCircularPattern5(Number As Long, Spacing As Double, FlipDirection As Boolean,
'     DName As String, GeometryPattern As Boolean, EqualSpacing As Boolean,
'     VaryInstance As Boolean, SyncSubAssemblies As Boolean, BDir2 As Boolean,
'     BSymmetric As Boolean, Number2 As Long, Spacing2 As Double, DName2 As String,
'     EqualSpacing2 As Boolean)
' Conventions asserted ONCE here, never re-interpreted downstream:
'   * Number (totalInstances) INCLUDES the seed: 6 = seed + 5 copies.
'   * Spacing is the TOTAL angle in RADIANS when EqualSpacing=True.
' Selection contract (a wrong/missing mark = silent Nothing return):
'   pattern axis  -> SelectByID2 ... Mark:=1
'   seed feature  -> SelectByID2 ... Mark:=4 (type "BODYFEATURE", exact tree name)
Function CreateCircularPatternSafe(axisName As String, seedName As String, _
        totalInstances As Integer, totalAngleDeg As Double, reverseDir As Boolean, _
        geometryPattern As Boolean, varySketch As Boolean, newName As String, _
        stepName As String) As Boolean
    Dim swFeat As SldWorks.Feature
    Dim spacingRad As Double
    spacingRad = totalAngleDeg * 4# * Atn(1#) / 180#
    swModel.ClearSelection2 True
    If Not swModel.Extension.SelectByID2(axisName, "AXIS", 0, 0, 0, False, 1, Nothing, 0) Then
        LogResult "FAIL", stepName, "Could not select pattern axis '" & axisName & "' (Mark=1)"
        Exit Function
    End If
    If Not swModel.Extension.SelectByID2(seedName, "BODYFEATURE", 0, 0, 0, True, 4, Nothing, 0) Then
        LogResult "FAIL", stepName, "Could not select seed feature '" & seedName & "' (Mark=4)"
        Exit Function
    End If
    On Error Resume Next
    Set swFeat = swModel.FeatureManager.FeatureCircularPattern5( _
        totalInstances, spacingRad, reverseDir, "NULL", geometryPattern, True, varySketch, _
        False, False, False, 1, spacingRad, "NULL", False)
    On Error GoTo 0
    If swFeat Is Nothing Then
        ' Older-release fallback: FeatureCircularPattern4 (same leading 7 arguments).
        On Error Resume Next
        Set swFeat = swModel.FeatureManager.FeatureCircularPattern4( _
            totalInstances, spacingRad, reverseDir, "NULL", geometryPattern, True, varySketch)
        On Error GoTo 0
    End If
    If swFeat Is Nothing Then Exit Function
    ' Name the pattern feature immediately so downstream selections never depend
    ' on SolidWorks' auto-numbering (CirPattern1 vs CirPattern2 drift).
    swFeat.Name = newName
    CreateCircularPatternSafe = True
End Function

' --- Select a reference plane robustly (plane names vary by template / language) ---
Function SelectRefPlane(planeName As String, planeIndex As Integer) As Boolean
    Dim tries As Variant, i As Integer
    swModel.ClearSelection2 True
    tries = Array(planeName, Replace(planeName, " Plane", ""), "Plane" & planeIndex)
    For i = LBound(tries) To UBound(tries)
        If swModel.Extension.SelectByID2(CStr(tries(i)), "PLANE", 0, 0, 0, False, 0, Nothing, 0) Then
            SelectRefPlane = True
            Exit Function
        End If
    Next i
    ' Fallback: planeIndex-th reference plane in the feature tree (template order).
    Dim feat As SldWorks.Feature, n As Integer
    Set feat = swModel.FirstFeature
    Do While Not feat Is Nothing
        If feat.GetTypeName2 = "RefPlane" Then
            n = n + 1
            If n = planeIndex Then
                swModel.ClearSelection2 True
                SelectRefPlane = feat.Select2(False, 0)
                Exit Function
            End If
        End If
        Set feat = feat.GetNextFeature
    Loop
    SelectRefPlane = False
End Function
"""


def _vba_header(title: str, part_label: str, unit_factor: float, body_uses_doc: bool = True) -> str:
    title = _vba_str(title)
    part_label = _vba_str(part_label)
    doc_lines = (
        """
    Set swModel = swApp.ActiveDoc
    If swModel Is Nothing Then
        MsgBox "No active document. Run 00_setup.vba first.", vbCritical
        LogResult "FAIL", "{title}", "No active document"
        End
    End If"""
        if body_uses_doc
        else ""
    ).replace("{title}", title)

    return f"""' ============================================================
' {title}
' Part: {part_label}
' Generated by the MTI 2D->3D pipeline. Run inside SolidWorks:
'   Tools > Macro > New (or Alt+F11), paste this file's contents,
'   then Run. Run macros strictly in numbered order.
' SolidWorks API works in METERS: every drawing value below is
' written as  value * UNIT_FACTOR.
' ============================================================
Option Explicit

Const UNIT_FACTOR As Double = {unit_factor}

Dim swApp As SldWorks.SldWorks
Dim swModel As SldWorks.ModelDoc2
Dim boolstatus As Boolean
{_HELPERS_VBA}
Sub main()
    Set swApp = Application.SldWorks{doc_lines}
"""


def _vba_footer() -> str:
    return """End Sub
"""


def _fail_block(step: str, message: str, indent: str = "    ") -> str:
    return (
        f'{indent}MsgBox "{message}", vbCritical\n'
        f'{indent}LogResult "FAIL", "{step}", "{message}"\n'
        f'{indent}WriteMacroResult "{step}", "FAIL", "{message}"\n'
        f"{indent}End\n"
    )


def _sketch_open(plane: str, step: str) -> str:
    idx = PLANE_INDEX.get(plane, 1)
    return f"""    ' ---- PLANE SELECTION ({plane}; name auto-detected) ----
    If Not SelectRefPlane("{plane}", {idx}) Then
{_fail_block(step, f"Could not select {plane} (no reference plane found).", "        ")}    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
"""


def _sketch_close_fully_define(step: str) -> str:
    return f"""
    ' ---- FINALIZE SKETCH ----
    ' The feature call below consumes the ACTIVE sketch - this is exactly what
    ' SolidWorks' own macro recorder emits (ClearSelection2 then the feature
    ' call, sketch left open). No closing, no name-based reselection.
    On Error Resume Next
    swModel.SketchManager.FullyDefineSketch True, True, 0, True, 1, Nothing, 1, Nothing, 0, 0
    On Error GoTo 0
    swModel.ClearSelection2 True
    If swModel.SketchManager.ActiveSketch Is Nothing Then
{_fail_block(step, "No active sketch to build the feature from.", "        ")}    End If
"""


def _profile_vba(dims: dict[str, float], cx: float, cy: float, step: str) -> tuple[str, dict[str, float]]:
    """VBA to draw a circle (centered at cx, cy) or rectangle (lower-left corner
    at cx, cy) profile.

    DRAWING FRAME convention: the base plate's lower-left corner sits at the
    sketch origin, so hole/feature positions dimensioned from the part edges
    (the normal drafting practice) can be used as sketch coordinates directly.
    """
    used: dict[str, float] = {}
    diameter = dims.get("diameter") or dims.get("hole_diameter")
    length = dims.get("length") or dims.get("width")
    width = dims.get("width") or dims.get("length")
    if diameter:
        used["diameter"] = diameter
        code = f"""    ' ---- SKETCH: circle dia {_v(diameter)} at ({_v(cx)}, {_v(cy)}) drawing units ----
    swModel.SketchManager.CreateCircleByRadius {_v(cx)} * UNIT_FACTOR, {_v(cy)} * UNIT_FACTOR, 0#, ({_v(diameter)} / 2#) * UNIT_FACTOR
"""
    elif length and width:
        used["length"], used["width"] = length, width
        code = f"""    ' ---- SKETCH: rectangle {_v(length)} x {_v(width)}, lower-left corner at ({_v(cx)}, {_v(cy)}) ----
    ' (Corner at the origin keeps sketch coordinates equal to the drawing's
    '  edge-referenced dimensions, so hole positions land where dimensioned.)
    swModel.SketchManager.CreateCornerRectangle {_v(cx)} * UNIT_FACTOR, {_v(cy)} * UNIT_FACTOR, 0#, _
        ({_v(cx)} + {_v(length)}) * UNIT_FACTOR, ({_v(cy)} + {_v(width)}) * UNIT_FACTOR, 0#
"""
    else:
        raise MacroGenerationError(
            f"{step}: profile needs a diameter or length+width; got {sorted(dims)}"
        )
    return code, used


def _extrusion3(depth_expr: str, blind: bool = True) -> str:
    """FeatureExtrusion3 — signature verified against SolidWorks API examples."""
    end = "swEndConditions_e.swEndCondBlind" if blind else "swEndConditions_e.swEndCondThroughAll"
    return f"""    Dim swFeat As SldWorks.Feature
    Set swFeat = swModel.FeatureManager.FeatureExtrusion3( _
        True, False, False, _
        {end}, swEndConditions_e.swEndCondBlind, _
        {depth_expr}, 0.01, _
        False, False, False, False, 0#, 0#, _
        False, False, False, False, _
        True, True, True, _
        swStartConditions_e.swStartSketchPlane, 0#, False)
"""


def _cut4(depth_expr: str, thru: bool, var: str = "swFeat") -> str:
    """FeatureCut4 — mirrors the verified call in pipeline/solidworks_builder.py.

    Direction-proof: thru cuts use Through All - Both so the cut reaches the
    material regardless of which side of the sketch plane the body sits on;
    if the first attempt still fails (e.g. blind cut aimed at empty space),
    the sketch is reselected and the cut retried with the direction flipped.
    """
    end = "swEndConditions_e.swEndCondThroughAllBoth" if thru else "swEndConditions_e.swEndCondBlind"
    retry_end = "swEndConditions_e.swEndCondThroughAll" if thru else "swEndConditions_e.swEndCondBlind"

    def call(indent: str, dir_flip: str, end_cond: str) -> str:
        return f"""{indent}Set {var} = swModel.FeatureManager.FeatureCut4( _
{indent}    True, False, {dir_flip}, _
{indent}    {end_cond}, swEndConditions_e.swEndCondBlind, _
{indent}    {depth_expr}, 0.01, _
{indent}    False, False, False, False, 0#, 0#, _
{indent}    False, False, False, False, False, _
{indent}    True, True, True, True, False, _
{indent}    swStartConditions_e.swStartSketchPlane, 0#, False, False)
"""

    return (
        f"    Dim {var} As SldWorks.Feature\n"
        + call("    ", "False", end)
        + f"""    If {var} Is Nothing Then
        ' The cut may have missed the material (body on the other side of the
        ' sketch plane) - restore the profile sketch and retry, direction flipped.
        If swModel.SketchManager.ActiveSketch Is Nothing Then
            ' Sketch was consumed/closed by the failed attempt: select the most
            ' recent sketch feature in the tree (type "ProfileFeature") by object,
            ' never by name.
            Dim featR{var} As SldWorks.Feature, lastSk{var} As SldWorks.Feature
            Set featR{var} = swModel.FirstFeature
            Do While Not featR{var} Is Nothing
                If featR{var}.GetTypeName2 = "ProfileFeature" Then Set lastSk{var} = featR{var}
                Set featR{var} = featR{var}.GetNextFeature
            Loop
            swModel.ClearSelection2 True
            If Not lastSk{var} Is Nothing Then lastSk{var}.Select2 False, 0
        End If
"""
        + call("        ", "True", retry_end)
        + "    End If\n"
    )


def _feature_check_and_name(feature_name: str, step: str) -> str:
    return f"""
    If swFeat Is Nothing Then
{_fail_block(step, "Feature creation returned Nothing - check the sketch.", "        ")}    End If
    swFeat.Name = "{feature_name}"
    If Not VerifySolidBody("{step}") Then
{_fail_block(step, "No solid body after this feature.", "        ")}    End If
    LogResult "PASS", "{step}", "Created feature {feature_name}"
    WriteMacroResult "{feature_name}", "PASS", ""
"""


# --------------------------------------------------------------------------- #
# Per-feature macro builders (return VBA text)
# --------------------------------------------------------------------------- #
def _macro_extrude(model: DrawingData, feature: Feature, step: str, is_cut: bool) -> tuple[str, dict[str, float], str]:
    dims = _dims_map(model, feature)
    depth = _depth_of(dims)
    plane = _plane_for(feature)
    if feature.position_known:
        cx, cy = feature.offset_x, feature.offset_y
    elif dims.get("diameter") or dims.get("hole_diameter"):
        # Unplaced circular feature: assume centered on the plate envelope.
        length, width = _envelope(model)
        cx, cy = (length or 0.0) / 2.0, (width or 0.0) / 2.0
    else:
        # Unplaced rectangle: lower-left corner at the origin (drawing frame).
        cx, cy = 0.0, 0.0

    profile, used = _profile_vba(dims, cx, cy, step)
    thru = is_cut and depth is None
    if depth is None and not is_cut:
        raise MacroGenerationError(f"{step}: extrude_boss has no depth/height dimension.")
    if depth is not None:
        used["depth"] = depth
    depth_expr = f"{_v(depth)} * UNIT_FACTOR" if depth is not None else "0#"

    position_note = (
        "Position read from drawing."
        if feature.position_known
        else "POSITION ASSUMED (drawing frame: rect corner at origin / circle at plate center) - verify against the drawing."
    )
    body = _sketch_open(plane, step)
    body += profile
    if not feature.position_known:
        body += f"    ' NOTE: {position_note}\n"
    body += _sketch_close_fully_define(step)
    body += "\n    ' ---- FEATURE ----\n"
    body += _cut4(depth_expr, thru) if is_cut else _extrusion3(depth_expr, blind=True)
    body += _feature_check_and_name(f"{feature.id}_{_vba_name(feature.description)}", step)
    return body, used, position_note


def _macro_holes(model: DrawingData, feature: Feature, step: str) -> tuple[str, dict[str, float], str]:
    """Holes as exact circle sketches + one cut (plus cbore/tap follow-ups)."""
    h = model.hole_callout_for_feature(feature.id)
    dims = _dims_map(model, feature)
    if h is None:
        # No callout — fall back to a plain circular cut from the feature dims.
        return _macro_extrude(model, feature, step, is_cut=True)

    plane = _plane_for(feature)
    positions = _hole_positions(model, h)
    used: dict[str, float] = {"diameter": h.diameter, "qty": float(h.qty)}
    thru = h.thru or h.type == HoleType.THRU
    depth_expr = "0#"
    if not thru:
        if h.depth <= 0:
            raise MacroGenerationError(f"{step}: blind hole {h.id} has no depth.")
        used["depth"] = h.depth
        depth_expr = f"{_v(h.depth)} * UNIT_FACTOR"

    position_note = (
        "Hole positions read from drawing."
        if h.position_known
        else "HOLE POSITIONS ASSUMED (centered on the plate envelope) - verify against the drawing."
    )

    body = _sketch_open(plane, step)
    body += f"    ' ---- SKETCH: {len(positions)} hole(s) dia {_v(h.diameter)} ({h.type.value}) ----\n"
    for x, y in positions:
        body += (
            f"    swModel.SketchManager.CreateCircleByRadius {_v(x)} * UNIT_FACTOR, "
            f"{_v(y)} * UNIT_FACTOR, 0#, ({_v(h.diameter)} / 2#) * UNIT_FACTOR\n"
        )
    body += f"    ' NOTE: {position_note}\n"
    body += _sketch_close_fully_define(step)
    body += "\n    ' ---- CUT ----\n"
    body += _cut4(depth_expr, thru)
    body += _feature_check_and_name(f"{feature.id}_{_vba_name(feature.description)}", step)

    # Counterbore: second concentric blind cut with the larger diameter.
    if h.type == HoleType.COUNTERBORE and h.cbore_diameter > 0 and h.cbore_depth > 0:
        used["cbore_diameter"], used["cbore_depth"] = h.cbore_diameter, h.cbore_depth
        body += f"""
    ' ---- COUNTERBORE: concentric blind cut dia {_v(h.cbore_diameter)} x {_v(h.cbore_depth)} deep ----
"""
        body += _sketch_open(plane, step + "_cbore")
        for x, y in positions:
            body += (
                f"    swModel.SketchManager.CreateCircleByRadius {_v(x)} * UNIT_FACTOR, "
                f"{_v(y)} * UNIT_FACTOR, 0#, ({_v(h.cbore_diameter)} / 2#) * UNIT_FACTOR\n"
            )
        body += _sketch_close_fully_define(step + "_cbore")
        body += "\n"
        body += _cut4(f"{_v(h.cbore_depth)} * UNIT_FACTOR", thru=False, var="swFeatCb")
        body += f"""
    If swFeatCb Is Nothing Then
{_fail_block(step, "Counterbore cut failed.", "        ")}    End If
    swFeatCb.Name = "{feature.id}_cbore"
    LogResult "PASS", "{step}", "Counterbore created"
"""

    # Countersink: flag for manual chamfer on the hole edge (selection is visual).
    if h.type == HoleType.COUNTERSINK and h.csink_diameter > 0:
        used["csink_diameter"], used["csink_angle"] = h.csink_diameter, h.csink_angle or 90.0
        body += f"""
    ' TODO: VERIFY API CALL - countersink
    ' Apply a chamfer of dia {_v(h.csink_diameter)} at {_v(h.csink_angle or 90.0)} deg included angle
    ' to the hole rim edge(s). Edge selection by coordinate is unreliable in a
    ' generated macro: select the hole edge(s) manually, then use Insert >
    ' Features > Chamfer with the values above.
    LogResult "WARN", "{step}", "Countersink requires manual chamfer - see macro comments"
"""

    # Tapped: cosmetic thread only, marked for verification.
    if h.type == HoleType.TAPPED and h.thread_spec:
        spec = _vba_str(h.thread_spec, 60)
        body += f"""
    ' TODO: VERIFY API CALL - cosmetic thread for "{spec}"
    ' Real (helical) threads are prohibited. Apply a cosmetic thread:
    ' select the hole's circular edge, then Insert > Annotations > Cosmetic Thread,
    ' spec "{spec}". (InsertCosmeticThread3 exists but its argument
    ' shape was not verified against a documented example, so it is not scripted.)
    LogResult "WARN", "{step}", "Apply cosmetic thread {spec} manually - see macro comments"
"""
    return body, used, position_note


def _model_radius_fallback(model: DrawingData) -> tuple[float, str]:
    """First fillet/corner-radius dimension anywhere in the model (drawing units).

    Used when a fillet feature was extracted but its radius dimension was not
    linked via related_dimensions — so an extracted fillet is never silently
    dropped from the macro. ``fillet_radius`` is preferred over a bare ``radius``
    (which could be a bolt-circle radius). Returns ``(value, dim_id)`` or
    ``(0.0, "")`` when nothing usable exists."""
    for token in ("fillet_radius", "radius"):
        for d in model.dimensions:
            if d.canonical_applies_to == token and d.value > 0:
                return d.value, d.id
    return 0.0, ""


def _model_chamfer_fallback(model: DrawingData) -> tuple[float, str]:
    """First chamfer-distance dimension anywhere in the model (drawing units)."""
    for d in model.dimensions:
        if d.canonical_applies_to == "chamfer" and d.value > 0:
            return d.value, d.id
    return 0.0, ""


def _macro_fillet_chamfer(model: DrawingData, features: list[Feature], step: str) -> tuple[str, dict[str, float]]:
    """One combined macro: user pre-selects edges, macro applies values.

    Edge selection by coordinates in a generated macro is the single most
    fragile SolidWorks operation, so the reliable contract is human-in-the-loop:
    the drawing shows WHERE, the macro applies the exact extracted VALUE.
    """
    used: dict[str, float] = {}
    body = """    ' This macro applies fillets/chamfers to the edges YOU have selected.
    ' For each block below: select the edge(s) in the graphics area first,
    ' then press F5 (run). Blocks for values you've already applied can be
    ' skipped by commenting them out.

    Dim swSelMgr As SldWorks.SelectionMgr
    Set swSelMgr = swModel.SelectionManager
"""
    for f in features:
        dims = _dims_map(model, f)
        if f.type == FeatureType.FILLET:
            radius = dims.get("fillet_radius") or dims.get("radius") or next(iter(dims.values()), 0.0)
            if radius <= 0:
                # The fillet was extracted but its radius wasn't linked to the
                # feature — recover it from any fillet/corner-radius dimension on
                # the drawing rather than dropping the fillet entirely.
                radius, src_id = _model_radius_fallback(model)
                if radius <= 0:
                    body += f"\n    ' {f.id}: SKIPPED - no radius dimension found.\n"
                    continue
                body += f"\n    ' {f.id}: radius not linked to the feature; using {src_id}=R{_v(radius)} from the drawing - VERIFY.\n"
            used[f"{f.id}_radius"] = radius
            body += f"""
    ' ---- {f.id}: FILLET R{_v(radius)} ({f.description}) ----
    If swSelMgr.GetSelectedObjectCount2(-1) = 0 Then
        MsgBox "Select the edge(s) for fillet {f.id} (R{_v(radius)}), then run again.", vbExclamation
        LogResult "WARN", "{step}", "{f.id} fillet skipped - no edges selected"
    Else
        Dim swFeat{f.id} As SldWorks.Feature
        Set swFeat{f.id} = swModel.FeatureManager.FeatureFillet3( _
            swFeatureFilletOptions_e.swFeatureFilletPropagate, _
            {_v(radius)} * UNIT_FACTOR, 0#, 0#, 0, 0, 0, _
            Nothing, Nothing, Nothing, Nothing, Nothing, Nothing, Nothing)
        If swFeat{f.id} Is Nothing Then
            LogResult "WARN", "{step}", "{f.id} fillet failed (continuing - fillets are non-fatal)"
        Else
            swFeat{f.id}.Name = "{f.id}_{_vba_name(f.description)}"
            LogResult "PASS", "{step}", "{f.id} fillet R{_v(radius)} applied"
        End If
        swModel.ClearSelection2 True
    End If
"""
        else:  # chamfer
            distance = dims.get("chamfer") or dims.get("length") or next(iter(dims.values()), 0.0)
            angle = dims.get("angle", 45.0)
            if distance <= 0:
                distance, src_id = _model_chamfer_fallback(model)
                if distance <= 0:
                    body += f"\n    ' {f.id}: SKIPPED - no distance dimension found.\n"
                    continue
                body += f"\n    ' {f.id}: distance not linked to the feature; using {src_id}={_v(distance)} from the drawing - VERIFY.\n"
            used[f"{f.id}_distance"] = distance
            used[f"{f.id}_angle_deg"] = angle
            body += f"""
    ' ---- {f.id}: CHAMFER {_v(distance)} x {_v(angle)}deg ({f.description}) ----
    If swSelMgr.GetSelectedObjectCount2(-1) = 0 Then
        MsgBox "Select the edge(s) for chamfer {f.id} ({_v(distance)} x {_v(angle)}deg), then run again.", vbExclamation
        LogResult "WARN", "{step}", "{f.id} chamfer skipped - no edges selected"
    Else
        Dim swFeatC{f.id} As SldWorks.Feature
        Set swFeatC{f.id} = swModel.FeatureManager.InsertFeatureChamfer( _
            4, 1, {_v(distance)} * UNIT_FACTOR, ({_v(angle)} * 3.14159265358979 / 180#), 0#, 0#, 0#, 0#)
        If swFeatC{f.id} Is Nothing Then
            LogResult "WARN", "{step}", "{f.id} chamfer failed (continuing - chamfers are non-fatal)"
        Else
            swFeatC{f.id}.Name = "{f.id}_{_vba_name(f.description)}"
            LogResult "PASS", "{step}", "{f.id} chamfer applied"
        End If
        swModel.ClearSelection2 True
    End If
"""
    return body, used


def _macro_revolve_skeleton(feature: Feature, step: str) -> str:
    return f"""    ' TODO: VERIFY API CALL — revolve {feature.id}
    ' A revolve needs a profile sketch + a centerline axis read from the drawing
    ' geometry, which cannot be reliably synthesized from dimensions alone.
    ' Build manually: sketch the half-profile on {_plane_for(feature)}, add a
    ' centerline on the revolve axis, then Insert > Boss/Base > Revolve (360 deg).
    ' Extracted description: {feature.description}
    MsgBox "Feature {feature.id} (revolve) requires manual modeling - see macro comments.", vbInformation
    LogResult "WARN", "{step}", "{feature.id} revolve requires manual modeling"
"""


def _macro_revolve(model: DrawingData, feature: Feature, step: str) -> Optional[tuple[str, dict[str, float], str]]:
    """Real revolve macro from the extracted half-profile, or None when no profile
    was extracted (the caller then falls back to the manual skeleton).

    Draws a horizontal centerline (the revolve axis) plus the closed profile
    polyline, then revolves 360°. Profile points are [axial, radial] in drawing
    units; the region is closed back to the axis by ``revolve_sketch_points``.
    """
    profile = list(feature.revolve_profile or [])
    if len(profile) < 2:
        return None
    closed, (x_min, x_max) = revolve_sketch_points(profile)
    plane = _plane_for(feature)
    max_r = max((p[1] for p in profile), default=0.0)

    body = _sketch_open(plane, step)
    body += f"    ' ---- REVOLVE PROFILE: {len(closed)} pts about a horizontal axis (drawing units) ----\n"
    body += "    Dim axisSeg As SldWorks.SketchSegment\n"
    body += (
        f"    Set axisSeg = swModel.SketchManager.CreateCenterLine("
        f"{_v(x_min)} * UNIT_FACTOR, 0#, 0#, {_v(x_max)} * UNIT_FACTOR, 0#, 0#)\n"
    )
    for (x1, y1), (x2, y2) in zip(closed, closed[1:] + closed[:1]):
        body += (
            f"    swModel.SketchManager.CreateLine "
            f"{_v(x1)} * UNIT_FACTOR, {_v(y1)} * UNIT_FACTOR, 0#, "
            f"{_v(x2)} * UNIT_FACTOR, {_v(y2)} * UNIT_FACTOR, 0#\n"
        )
    body += "    On Error Resume Next\n"
    body += "    swModel.SketchManager.FullyDefineSketch True, True, 0, True, 1, Nothing, 1, Nothing, 0, 0\n"
    body += "    On Error GoTo 0\n"
    body += "    swModel.SketchManager.InsertSketch True   ' close the profile sketch\n"
    body += "    swModel.ClearSelection2 True\n"
    body += "    If Not axisSeg Is Nothing Then axisSeg.Select4 False, Nothing\n"
    body += "\n    ' ---- REVOLVE 360 deg ----\n"
    body += "    Dim swFeat As SldWorks.Feature\n"
    body += (
        "    Set swFeat = swModel.FeatureManager.FeatureRevolve2( _\n"
        "        True, True, False, False, False, False, _\n"
        "        0, 0, (2 * 3.14159265358979), 0#, False, False, 0#, 0#, _\n"
        "        0, 0#, 0#, True, True, True)\n"
    )
    body += _feature_check_and_name(f"{feature.id}_{_vba_name(feature.description)}", step)
    used = {"axial_length": round(x_max - x_min, 6), "max_radius": round(max_r, 6),
            "profile_points": float(len(profile))}
    return body, used, "Revolved 360 deg from the extracted half-profile."


def _macro_mirror(model: DrawingData, feature: Feature, step: str) -> Optional[tuple[str, dict[str, float], str]]:
    """Mirror the host feature about a plane, or None when the seed isn't known
    (the caller falls back to a manual skeleton)."""
    parent = model.feature_by_id(feature.parent_feature) if feature.parent_feature else None
    if parent is None:
        return None
    plane = PLANE_NAMES.get(
        (feature.mirror_plane or feature.sketch_plane or "front").lower().strip(), "Front Plane"
    )
    idx = PLANE_INDEX.get(plane, 1)
    seed_name = f"{parent.id}_{_vba_name(parent.description)}"
    body = f"""    ' ---- MIRROR {feature.id}: mirror {parent.id} about {plane} ----
    If Not SelectRefPlane("{plane}", {idx}) Then
{_fail_block(step, f"Could not select mirror plane {plane}.", "        ")}    End If
    ' Append the feature to mirror (mark 4); select by feature name (BODYFEATURE,
    ' not SKETCH — name reselection is only unreliable for sketches).
    boolstatus = swModel.Extension.SelectByID2("{seed_name}", "BODYFEATURE", 0, 0, 0, True, 4, Nothing, 0)
    If Not boolstatus Then
        MsgBox "Select the feature to mirror ({parent.id}) in the tree, then run again.", vbExclamation
        LogResult "WARN", "{step}", "{feature.id} mirror: seed {parent.id} not found by name"
    Else
        Dim swFeat As SldWorks.Feature
        ' InsertMirrorFeature2(BodyFeatureScope, GeomPattern, Merge, KnitSurface)
        Set swFeat = swModel.FeatureManager.InsertMirrorFeature2(False, False, True, False)
        If swFeat Is Nothing Then
            LogResult "WARN", "{step}", "{feature.id} mirror failed (continuing - verify in tree)"
        Else
            swFeat.Name = "{feature.id}_{_vba_name(feature.description)}"
            LogResult "PASS", "{step}", "{feature.id} mirrored {parent.id} about {plane}"
        End If
        swModel.ClearSelection2 True
    End If
"""
    return body, {}, f"Mirror {parent.id} about {plane} — verify against the drawing."


def _pattern_covered_by(model: DrawingData, feature: Feature) -> Optional[tuple[str, int]]:
    """If the pattern's instances were already emitted as multiple circles in
    the parent hole feature's cut, return (parent_id, qty) — the pattern macro
    becomes a verified no-op instead of a manual step."""
    if not feature.parent_feature:
        return None
    parent = model.feature_by_id(feature.parent_feature)
    if parent is None:
        return None
    h = model.hole_callout_for_feature(parent.id)
    if h is not None and h.qty >= max(feature.quantity, 2):
        return parent.id, h.qty
    return None


def _macro_pattern_covered(parent_id: str, qty: int, feature: Feature, step: str) -> str:
    return f"""    ' Pattern {feature.id} is ALREADY SATISFIED: feature {parent_id} cut all
    ' {qty} instance(s) as separate circles in one sketch, so there is nothing
    ' left to pattern. This macro just records that and moves on.
    LogResult "PASS", "{step}", "{feature.id} pattern already realized by {parent_id} ({qty} instances) - no action needed"
"""


def _macro_pattern_skeleton(model: DrawingData, feature: Feature, step: str) -> str:
    dims = _dims_map(model, feature)
    spacing = dims.get("spacing") or next(iter(dims.values()), 0.0)
    return f"""    ' TODO: VERIFY API CALL — linear pattern {feature.id}
    ' Pattern parameters from the drawing: qty={feature.quantity}, spacing={_v(spacing)} drawing units.
    ' FeatureLinearPattern requires a pre-selected seed feature AND a direction
    ' edge, which cannot be chosen reliably from extracted data. Either:
    '  (a) the holes were already emitted as multiple circles in one cut (preferred), or
    '  (b) select the seed feature + a direction edge, then use
    '      Insert > Pattern/Mirror > Linear Pattern with the values above.
    MsgBox "Feature {feature.id} (pattern): apply manually if not already covered - see comments.", vbInformation
    LogResult "WARN", "{step}", "{feature.id} pattern left for manual application"
"""


# --------------------------------------------------------------------------- #
# Circular-pattern reliability layer (must-meet Part 2)
# --------------------------------------------------------------------------- #
# Canonical circular-pattern schema: every field below must be non-null in
# build_plan.json or generation REFUSES (MacroGenerationError). The convention
# "total_instances INCLUDES the seed" is asserted here once and never
# re-interpreted downstream (VBA helper + COM builder + CadQuery prevalidation
# all consume this dict verbatim).
CIRCULAR_PATTERN_REQUIRED = (
    "feature_type", "seed_feature_name", "pattern_axis", "total_instances",
    "equal_spacing", "total_angle_deg", "reverse_direction", "instances_to_skip",
    "geometry_pattern", "vary_sketch", "bolt_circle_radius_in", "seed_angle_deg",
)


def route_to_circular_pattern(model: DrawingData, h: Optional[HoleCallout]) -> bool:
    """Part 2c routing rule: a hole group builds as a real FeatureCircularPattern
    ONLY when the callout is marked circular (set by the must-meet spec in Stage
    2.6, or by polar-style drawing dimensioning at extraction) AND the bolt
    circle is grounded. Anything else keeps the baked-circles path."""
    return (
        h is not None
        and h.pattern == PatternKind.CIRCULAR
        and h.qty >= 2
        and h.bolt_circle_diameter > 0
    )


def _model_thickness(model: DrawingData) -> float:
    """Base-solid thickness in drawing units (0.0 when unknown)."""
    for f in model.features:
        if f.type == FeatureType.EXTRUDE_BOSS:
            d = _depth_of(_dims_map(model, f))
            if d:
                return d
    for d in model.dimensions:
        if d.canonical_applies_to in ("depth", "thickness") and d.value > 0:
            return d.value
    return 0.0


def _pattern_center(model: DrawingData, h: HoleCallout) -> tuple[float, float]:
    """Bolt-circle center in the drawing (corner) frame."""
    if len(h.bolt_circle_center) == 2:
        return float(h.bolt_circle_center[0]), float(h.bolt_circle_center[1])
    length, width = _envelope(model)
    r = h.bolt_circle_diameter / 2.0
    return ((length / 2.0) if length else r, (width / 2.0) if width else r)


def _bore_axis_probe(model: DrawingData, h: HoleCallout) -> Optional[dict]:
    """Find the center bore whose cylindrical face derives the pattern axis.

    Deterministic: the pipeline itself generates the bore geometry, so a point
    on its wall is exactly (cx + r_bore, cy) at mid-thickness. Returns
    ``{cx, cy, bore_radius, thickness}`` in drawing units, or None when no
    concentric bore exists (the caller then falls back to baked circles)."""
    cx, cy = _pattern_center(model, h)
    length, width = _envelope(model)
    length, width = length or 0.0, width or 0.0
    tol = max(0.05, 0.02 * max(length, width, 1.0))
    best: Optional[tuple[float, float, float]] = None  # (radius, bx, by)
    for other in model.hole_callouts:
        if other.id == h.id or other.diameter <= h.diameter * 1.05:
            continue
        if other.instance_positions and len(other.instance_positions[0]) == 2:
            bx, by = float(other.instance_positions[0][0]), float(other.instance_positions[0][1])
        elif other.position_known:
            bx, by = other.x_position, other.y_position
        else:
            bx, by = (length / 2.0) if length else cx, (width / 2.0) if width else cy
        if abs(bx - cx) <= tol and abs(by - cy) <= tol:
            r = other.diameter / 2.0
            if best is None or r > best[0]:
                best = (r, bx, by)
    if best is None:
        return None
    thickness = _model_thickness(model)
    return {"cx": best[1], "cy": best[2], "bore_radius": best[0],
            "thickness": thickness if thickness > 0 else 0.25}


def canonical_circular_pattern(model: DrawingData, feature: Feature,
                               h: HoleCallout, axis_name: str,
                               derivation: str) -> dict:
    """The Part-2a canonical dict for build_plan.json. Raises
    :class:`MacroGenerationError` if ANY required field would be null —
    the generator must refuse to emit VBA from an incomplete pattern spec."""
    spec = {
        "feature_type": "circular_pattern",
        "seed_feature_name": f"{feature.id}_SeedHoleCut",
        "pattern_axis": {
            "strategy": "reference_axis",
            "axis_name": axis_name,
            "derivation": derivation,
        },
        # INCLUDES the seed: qty 6 = seed + 5 patterned copies.
        "total_instances": int(h.qty),
        "equal_spacing": True,
        "total_angle_deg": 360.0,
        "reverse_direction": False,
        "instances_to_skip": [],
        "geometry_pattern": False,
        "vary_sketch": False,
        # Required because the pipeline creates the seed itself (they position
        # the seed sketch; the pattern generates the rest).
        "bolt_circle_radius_in": (h.bolt_circle_diameter / 2.0
                                  if h.bolt_circle_diameter > 0 else None),
        "seed_angle_deg": float(h.start_angle),
    }
    missing = [k for k in CIRCULAR_PATTERN_REQUIRED if spec.get(k) is None]
    if missing:
        raise MacroGenerationError(
            f"{feature.id}: circular_pattern spec is incomplete — null field(s) "
            f"{', '.join(missing)}; refusing to emit VBA."
        )
    return spec


def _seed_position(model: DrawingData, h: HoleCallout) -> tuple[float, float]:
    """Seed-hole center (drawing units): bolt center + radius at seed_angle."""
    cx, cy = _pattern_center(model, h)
    r = h.bolt_circle_diameter / 2.0
    a = math.radians(h.start_angle)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _m(value: float, unit_factor: float) -> float:
    """Drawing units -> meters, for auditability comments."""
    return round(value * unit_factor, 6)


def _macro_seed_hole(model: DrawingData, feature: Feature, h: HoleCallout,
                     step: str, unit_factor: float) -> tuple[str, dict[str, float]]:
    """Seed hole: ONE circle at the seed position + cut, deterministically named
    ``{fid}_SeedHoleCut`` so the pattern macro can select it by exact name."""
    plane = _plane_for(feature)
    sx, sy = _seed_position(model, h)
    thru = h.thru or h.type == HoleType.THRU
    depth_expr = "0#"
    used: dict[str, float] = {"diameter": h.diameter, "qty": float(h.qty)}
    if not thru:
        if h.depth <= 0:
            raise MacroGenerationError(f"{step}: blind seed hole {h.id} has no depth.")
        used["depth"] = h.depth
        depth_expr = f"{_v(h.depth)} * UNIT_FACTOR"
    seed_name = f"{feature.id}_SeedHoleCut"
    body = _sketch_open(plane, step)
    body += (
        f"    ' ---- SKETCH: SEED hole dia {_v(h.diameter)} at ({_v(sx)}, {_v(sy)}) drawing units ----\n"
        f"    ' {_v(h.diameter)} in dia -> radius {_m(h.diameter / 2.0, unit_factor)} m ; "
        f"seed center -> ({_m(sx, unit_factor)}, {_m(sy, unit_factor)}) m\n"
        f"    swModel.SketchManager.CreateCircleByRadius {_v(sx)} * UNIT_FACTOR, "
        f"{_v(sy)} * UNIT_FACTOR, 0#, ({_v(h.diameter)} / 2#) * UNIT_FACTOR\n"
    )
    body += _sketch_close_fully_define(step)
    body += "\n    ' ---- SEED CUT ----\n"
    body += _cut4(depth_expr, thru)
    body += _feature_check_and_name(seed_name, step)
    return body, used


def _macro_reference_axis(probe: dict, axis_name: str, step: str,
                          unit_factor: float) -> str:
    """Named reference axis through the center bore's cylindrical face.

    Face selection uses EXACT generated coordinates (the pipeline created the
    bore, so the wall point is known); z is tried on both sides of the sketch
    plane because the base extrude direction is template-dependent. The new
    axis is renamed immediately — downstream SelectByID2 is name-deterministic."""
    px = probe["cx"] + probe["bore_radius"]
    py = probe["cy"]
    bore_r_m = _m(probe["bore_radius"], unit_factor)
    cx_m = _m(probe["cx"], unit_factor)
    cy_m = _m(probe["cy"], unit_factor)
    t_m = _m(probe["thickness"], unit_factor)
    z1, z2 = -t_m / 2.0, t_m / 2.0
    fail_sel = _fail_block(step, f"Could not create reference axis {axis_name} from the bore face.", "        ")
    fail_find = _fail_block(step, "InsertAxis2 succeeded but no RefAxis feature found.", "        ")
    return f"""    ' ---- REFERENCE AXIS "{axis_name}" through the center bore's cylindrical face ----
    ' Bore: radius {_v(probe['bore_radius'])} in -> {bore_r_m} m, center ({_v(probe['cx'])}, {_v(probe['cy'])}) in -> ({cx_m}, {cy_m}) m
    ' Named references are deterministic; the axis is created ONCE here and
    ' selected by name ("{axis_name}") from then on. The bore face is found
    ' GEOMETRICALLY (cylinder radius + axis location), with an exact-coordinate
    ' probe as fallback — never a blind coordinate pick.
    Dim axOk As Boolean
    Dim swPartAx As SldWorks.PartDoc, vBodiesAx As Variant, swBodyAx As SldWorks.Body2
    Dim vFacesAx As Variant, iF As Integer
    Dim swFaceAx As SldWorks.Face2, swSurfAx As SldWorks.Surface, vParamsAx As Variant
    Set swPartAx = swModel
    vBodiesAx = swPartAx.GetBodies2(swBodyType_e.swSolidBody, True)
    If Not IsEmpty(vBodiesAx) Then
        Set swBodyAx = vBodiesAx(0)
        vFacesAx = swBodyAx.GetFaces
        For iF = LBound(vFacesAx) To UBound(vFacesAx)
            Set swFaceAx = vFacesAx(iF)
            Set swSurfAx = swFaceAx.GetSurface
            If swSurfAx.IsCylinder Then
                ' CylinderParams: (origin x,y,z, axis x,y,z, radius) in meters.
                vParamsAx = swSurfAx.CylinderParams
                If Abs(vParamsAx(6) - {bore_r_m}) < 0.00002 And _
                   Sqr((vParamsAx(0) - {cx_m}) ^ 2 + (vParamsAx(1) - {cy_m}) ^ 2) < 0.0005 Then
                    swModel.ClearSelection2 True
                    If swFaceAx.Select4(False, Nothing) Then
                        If swModel.InsertAxis2(True) Then
                            axOk = True
                            Exit For
                        End If
                    End If
                End If
            End If
        Next iF
    End If
    If Not axOk Then
        ' Fallback: exact generated wall point ({_v(px)}, {_v(py)}) drawing units.
        Dim zTry As Variant, iAx As Integer
        zTry = Array({z1}, {z2}, 0#)
        For iAx = LBound(zTry) To UBound(zTry)
            swModel.ClearSelection2 True
            If swModel.Extension.SelectByID2("", "FACE", {_v(px)} * UNIT_FACTOR, {_v(py)} * UNIT_FACTOR, CDbl(zTry(iAx)), False, 0, Nothing, 0) Then
                If swModel.InsertAxis2(True) Then
                    axOk = True
                    Exit For
                End If
            End If
        Next iAx
    End If
    If Not axOk Then
{fail_sel}    End If
    ' Rename the newest RefAxis feature to the deterministic name.
    Dim featAx As SldWorks.Feature, lastAx As SldWorks.Feature
    Set featAx = swModel.FirstFeature
    Do While Not featAx Is Nothing
        If featAx.GetTypeName2 = "RefAxis" Then Set lastAx = featAx
        Set featAx = featAx.GetNextFeature
    Loop
    If lastAx Is Nothing Then
{fail_find}    End If
    lastAx.Name = "{axis_name}"
    swModel.ClearSelection2 True
    LogResult "PASS", "{step}", "Reference axis {axis_name} created from the bore cylindrical face"
    WriteMacroResult "{axis_name}", "PASS", ""
"""


def _macro_circular_pattern(spec: dict, feature: Feature, step: str,
                            constraint_id: str = "") -> str:
    """The pattern feature itself, via the shared CreateCircularPatternSafe
    helper (single place holding the version-pinned API call)."""
    pat_name = f"{feature.id}_CircularPattern"
    axis = spec["pattern_axis"]["axis_name"]
    seed = spec["seed_feature_name"]
    n = spec["total_instances"]
    label = constraint_id or feature.id
    return f"""    ' ---- CIRCULAR PATTERN {feature.id}: {n} instances (n INCLUDES the seed = seed + {n - 1} copies) ----
    ' Bolt circle radius {_v(spec['bolt_circle_radius_in'])} drawing units, seed at {_v(spec['seed_angle_deg'])} deg,
    ' equal spacing over {_v(spec['total_angle_deg'])} deg about axis "{axis}".
    If Not CreateCircularPatternSafe("{axis}", "{seed}", {n}, {_v(spec['total_angle_deg'])}, {'True' if spec['reverse_direction'] else 'False'}, {'True' if spec['geometry_pattern'] else 'False'}, {'True' if spec['vary_sketch'] else 'False'}, "{pat_name}", "{step}") Then
        WriteMacroResult "{pat_name}", "FAIL", "FeatureCircularPattern returned Nothing - check marks/axis"
        LogResult "FAIL", "{step}", "FeatureCircularPattern returned Nothing - check marks/axis"
        swApp.SendMsgToUser2 "PATTERN FAILED at {label} ({feature.id})", swMessageBoxIcon_e.swMbStop, swMessageBoxBtn_e.swMbOk
        End
    End If
    If Not VerifySolidBody("{step}") Then
{_fail_block(step, "No solid body after the circular pattern.", "        ")}    End If
    LogResult "PASS", "{step}", "Circular pattern {pat_name} created ({n} instances)"
    WriteMacroResult "{pat_name}", "PASS", "{n} instances about {axis}"
"""


def _emit_circular_pattern_trio(model: DrawingData, feature: Feature,
                                h: HoleCallout, seq: int, macros_dir: Path,
                                unit_factor: float, run_all_subs: list,
                                pkg: "MacroPackage", resolution) -> Optional[int]:
    """Emit seed hole -> reference axis -> circular pattern as three numbered
    macros (the Part-2b build-order contract). Returns the new seq, or None when
    the axis cannot be derived (caller falls back to baked circles)."""
    probe = _bore_axis_probe(model, h)
    plane = _plane_for(feature)
    if probe is None or plane != "Front Plane":
        return None  # no concentric bore / non-front plane: baked circles are safer

    axis_no = 1 + sum(1 for s in pkg.steps if s.feature_type == "circular_pattern")
    axis_name = f"PatternAxis{axis_no}"
    derivation = "explicit reference axis from the center bore cylindrical face (InsertAxis2)"
    spec = canonical_circular_pattern(model, feature, h, axis_name, derivation)
    step_flags = _collect_step_flags(model, feature, resolution)
    positions = _hole_positions(model, h)

    # 1) Seed hole cut.
    seq += 1
    step_name = f"{seq:02d}_{feature.id}"
    fname = f"{seq:02d}_{feature.id}_SeedHoleCut.vba"
    body, used = _macro_seed_hole(model, feature, h, step_name, unit_factor)
    body = _flag_vba_block(step_name, step_flags) + body
    header = _vba_header(f"{step_name} - seed hole for circular pattern: {feature.description}",
                         model.display_name, unit_factor)
    (macros_dir / fname).write_text(header + body + _vba_footer(), encoding="utf-8")
    run_all_subs.append((f"Step{seq:02d}_{_vba_identifier(feature.id)}_Seed", body))
    step = BuildStep(seq, fname, feature.id, "hole",
                     f"Seed hole for circular pattern ({feature.description})",
                     "generated", dimensions=used,
                     notes=f"Seed of {spec['total_instances']}-instance circular pattern "
                           f"(feature name {spec['seed_feature_name']}).")
    _enrich_feature_step(step, model, feature, resolution, step_flags)
    step.positions_xy = [[round(_seed_position(model, h)[0], 6),
                          round(_seed_position(model, h)[1], 6)]]
    step.positions_xy_meters = [[_to_meters(step.positions_xy[0][0], model.units),
                                 _to_meters(step.positions_xy[0][1], model.units)]]
    pkg.steps.append(step)

    # 2) Reference axis.
    seq += 1
    step_name = f"{seq:02d}_{feature.id}_axis"
    fname = f"{seq:02d}_{feature.id}_reference_axis.vba"
    body = _macro_reference_axis(probe, axis_name, step_name, unit_factor)
    header = _vba_header(f"{step_name} - reference axis {axis_name} for the circular pattern",
                         model.display_name, unit_factor)
    (macros_dir / fname).write_text(header + body + _vba_footer(), encoding="utf-8")
    run_all_subs.append((f"Step{seq:02d}_{_vba_identifier(feature.id)}_Axis", body))
    step = BuildStep(seq, fname, feature.id, "reference_axis",
                     f"Named reference axis {axis_name} through the bore centerline",
                     "generated",
                     notes=f"Derivation: {derivation}.")
    step.sketch_plane = "front"
    pkg.steps.append(step)

    # 3) Circular pattern.
    seq += 1
    step_name = f"{seq:02d}_{feature.id}_pattern"
    fname = f"{seq:02d}_{feature.id}_circular_pattern.vba"
    body = _macro_circular_pattern(spec, feature, step_name)
    header = _vba_header(f"{step_name} - circular pattern ({spec['total_instances']} instances)",
                         model.display_name, unit_factor)
    (macros_dir / fname).write_text(header + body + _vba_footer(), encoding="utf-8")
    run_all_subs.append((f"Step{seq:02d}_{_vba_identifier(feature.id)}_Pattern", body))
    step = BuildStep(seq, fname, feature.id, "circular_pattern",
                     f"Circular pattern: {spec['total_instances']} instances, equal spacing",
                     "generated", dimensions={"qty": float(spec["total_instances"])},
                     notes="total_instances INCLUDES the seed.")
    _enrich_feature_step(step, model, feature, resolution, step_flags)
    step.positions_xy = [[round(x, 6), round(y, 6)] for x, y in positions]
    step.positions_xy_meters = [[_to_meters(x, model.units), _to_meters(y, model.units)]
                                for x, y in positions]
    step.circular_pattern = spec
    pkg.steps.append(step)
    return seq


# --------------------------------------------------------------------------- #
# Setup / final-verify macros
# --------------------------------------------------------------------------- #
_FIND_TEMPLATE_VBA = """' --- Find a Part template (.prtdot): configured folders first, then standard locations ---
Function FindPartTemplate(app As SldWorks.SldWorks) As String
    Dim dirs As String, parts() As String, i As Integer, p As String, hit As String
    ' Configured document-template folders (semicolon-separated), then common defaults.
    dirs = app.GetUserPreferenceStringValue(swUserPreferenceStringValue_e.swFileLocationsDocumentTemplates)
    dirs = dirs & ";C:\\ProgramData\\SOLIDWORKS\\SOLIDWORKS 2024\\templates" & _
                  ";C:\\ProgramData\\SolidWorks\\SOLIDWORKS 2024\\templates" & _
                  ";C:\\ProgramData\\SOLIDWORKS\\SOLIDWORKS 2025\\templates" & _
                  ";C:\\ProgramData\\SOLIDWORKS\\SOLIDWORKS 2023\\templates"
    parts = Split(dirs, ";")
    For i = LBound(parts) To UBound(parts)
        p = Trim$(parts(i))
        If Len(p) > 0 Then
            If Right$(p, 1) <> "\\" Then p = p & "\\"
            If Dir(p & "Part.prtdot") <> "" Then
                FindPartTemplate = p & "Part.prtdot"
                Exit Function
            End If
            hit = Dir(p & "*.prtdot")
            If hit <> "" Then
                FindPartTemplate = p & hit
                Exit Function
            End If
        End If
    Next i
    FindPartTemplate = ""
End Function

"""


def _setup_body(model: DrawingData, unit_factor: float) -> str:
    """Body of the setup step (create part, set units, save) — header-free so it
    can be wrapped either as a standalone macro or as a Sub inside RUN_ALL."""
    unit_enum = UNIT_SYSTEM_ENUM[model.units]
    part_file = _safe_name(model.display_name) + ".sldprt"
    return f"""
    ' ---- CREATE NEW PART from a part template ----
    ' Prefer the configured default; if unset (common on fresh installs / VDI),
    ' auto-discover a Part.prtdot from the template folders.
    Dim templatePath As String
    templatePath = swApp.GetUserPreferenceStringValue(swUserPreferenceStringValue_e.swDefaultTemplatePart)
    If Len(templatePath) = 0 Or Dir(templatePath) = "" Then
        templatePath = FindPartTemplate(swApp)
    End If
    If Len(templatePath) = 0 Then
{_fail_block("00_setup", "No part template found - set Tools > Options > Default Templates > Parts.", "        ")}    End If
    Set swModel = swApp.NewDocument(templatePath, 0, 0, 0)
    If swModel Is Nothing Then
{_fail_block("00_setup", "NewDocument failed.", "        ")}    End If

    ' ---- UNITS: must be set BEFORE any geometry ----
    boolstatus = swModel.Extension.SetUserPreferenceInteger( _
        swUserPreferenceIntegerValue_e.swUnitSystem, _
        swUserPreferenceOption_e.swDetailingNoOptionSpecified, {unit_enum})
    LogResult "PASS", "00_setup", "New part created; units set ({model.units.value})"

    ' ---- SAVE AS {part_file} (next to the macros folder) ----
    Dim macroPath As String, savePath As String
    Dim saveErrs As Long, saveWarns As Long
    macroPath = swApp.GetCurrentMacroPathName
    savePath = Left$(macroPath, InStrRev(macroPath, "\\")) & "..\\{part_file}"
    boolstatus = swModel.Extension.SaveAs(savePath, 0, _
        swSaveAsOptions_e.swSaveAsOptions_Silent, Nothing, saveErrs, saveWarns)
    If Not boolstatus Then
        LogResult "WARN", "00_setup", "Initial SaveAs failed (errs=" & saveErrs & ") - save manually"
    Else
        LogResult "PASS", "00_setup", "Saved " & savePath
    End If
"""


def _setup_macro(model: DrawingData, unit_factor: float) -> str:
    """Standalone 00_setup.vba (header + FindPartTemplate + setup body)."""
    header = _vba_header(
        "00_setup - new part, units, save-as", model.display_name, unit_factor, body_uses_doc=False
    )
    header = header.replace("Sub main()", _FIND_TEMPLATE_VBA + "Sub main()")
    return header + _setup_body(model, unit_factor) + _vba_footer()


def _final_verify_body(model: DrawingData, unit_factor: float, n_features: int) -> str:
    """Body of the final-verify step (header-free; reused by RUN_ALL)."""
    envelope_dims = [d for d in model.dimensions if d.is_envelope]
    expectations = (
        "; ".join(f"{d.canonical_applies_to}={_v(d.value)}" for d in envelope_dims)
        or "none extracted"
    )
    return f"""
    ' ---- FORCE REBUILD ----
    boolstatus = swModel.ForceRebuild3(False)
    If Not boolstatus Then
        LogResult "WARN", "ZZ_final_verify", "ForceRebuild3 reported failure - check the feature tree"
    End If

    ' ---- MASS PROPERTIES (proves a solid body exists) ----
    Dim vMass As Variant
    Dim mpStatus As Long
    vMass = swModel.Extension.GetMassProperties2(1, mpStatus, False)
    If IsEmpty(vMass) Then
{_fail_block("ZZ_final_verify", "GetMassProperties2 returned nothing - no solid body?", "        ")}    End If
    ' vMass: 0-2 = CoM x,y,z ; 3 = volume (m^3) ; 4 = surface area (m^2) ; 5 = mass
    If vMass(3) <= 0 Then
{_fail_block("ZZ_final_verify", "Part has zero volume.", "        ")}    End If
    LogResult "PASS", "ZZ_final_verify", "Volume(mm3)=" & Format$(vMass(3) * 1000000000#, "0.0") & _
        "  CoM(drawing units)=(" & Format$(vMass(0) / UNIT_FACTOR, "0.000") & ", " & _
        Format$(vMass(1) / UNIT_FACTOR, "0.000") & ", " & Format$(vMass(2) / UNIT_FACTOR, "0.000") & ")"

    ' ---- BOUNDING BOX vs DRAWING ENVELOPE ----
    ' Expected from the drawing: {expectations}
    ' Box read from the solid body (IBody2::GetBodyBox) - ModelDoc2 exposes
    ' no whole-model bounding-box call in VBA.
    Dim swPart As SldWorks.PartDoc
    Dim vBodies As Variant
    Dim swBody As SldWorks.Body2
    Dim vBox As Variant
    Set swPart = swModel
    vBodies = swPart.GetBodies2(swBodyType_e.swSolidBody, True)
    If IsEmpty(vBodies) Then
{_fail_block("ZZ_final_verify", "No solid body to measure.", "        ")}    End If
    Set swBody = vBodies(0)
    vBox = swBody.GetBodyBox
    MsgBox "Bounding box (drawing units): " & _
        Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000") & vbCrLf & _
        "Drawing envelope: {expectations}" & vbCrLf & _
        "Expected feature count: {n_features}", vbInformation
    LogResult "PASS", "ZZ_final_verify", "bbox(drawing units) " & _
        Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000")

    ' ---- SAVE ----
    Dim saveErrs As Long, saveWarns As Long
    boolstatus = swModel.Save3(swSaveAsOptions_e.swSaveAsOptions_Silent, saveErrs, saveWarns)
    LogResult IIf(boolstatus, "PASS", "WARN"), "ZZ_final_verify", "Save3 errs=" & saveErrs
"""


def _final_verify_macro(model: DrawingData, unit_factor: float, n_features: int) -> str:
    """Standalone ZZ_final_verify.vba (header + final-verify body)."""
    header = _vba_header(
        "ZZ_final_verify - rebuild, mass props, bbox, save", model.display_name, unit_factor
    )
    return header + _final_verify_body(model, unit_factor, n_features) + _vba_footer()


def _export_stl_body(model: DrawingData) -> str:
    """Body of the STL-export step (header-free; reused by RUN_ALL).

    Exports ``<part>.stl`` next to the saved ``.sldprt`` by swapping the active
    document's extension — so the STL filename matches the part name and the web
    UI's 3D viewer can locate it automatically. Uses SolidWorks' default STL
    export options (SaveAs3 is extension-driven)."""
    return """
    ' ---- EXPORT STL (beside the .sldprt, same base name) ----
    Dim stlPath As String
    stlPath = swModel.GetPathName
    If stlPath = "" Then
        MsgBox "Part has not been saved yet - run 00_setup / ZZ_final_verify first.", vbCritical
        LogResult "FAIL", "ZZZ_export_stl", "No saved path - cannot derive STL name"
        End
    End If
    Dim dotPos As Long
    dotPos = InStrRev(stlPath, ".")
    If dotPos > 0 Then stlPath = Left$(stlPath, dotPos - 1)
    stlPath = stlPath & ".stl"
    boolstatus = swModel.SaveAs3(stlPath, 0, 0)
    LogResult IIf(boolstatus, "PASS", "WARN"), "ZZZ_export_stl", "STL -> " & stlPath
"""


def _export_stl_macro(model: DrawingData, unit_factor: float) -> str:
    """Standalone ZZZ_export_stl.vba (header + STL-export body). Sorts AFTER
    ZZ_final_verify so it runs last in the numbered sequence."""
    header = _vba_header(
        "ZZZ_export_stl - export the part as STL beside the .sldprt",
        model.display_name, unit_factor,
    )
    return header + _export_stl_body(model) + _vba_footer()


def _vba_identifier(text: str) -> str:
    """A unique-ish, VBA-safe Sub identifier fragment."""
    frag = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    if frag and frag[0].isdigit():
        frag = "S" + frag
    return frag or "Step"


def _build_run_all(
    model: DrawingData,
    unit_factor: float,
    feature_subs: list[tuple[str, str]],
) -> str:
    """Assemble RUN_ALL.vba: one self-contained macro that runs every step in
    order in a single F5, with the same per-step logging and stop-on-first-failure
    (a failing step calls End, halting the run). No Python or installs needed on
    the SolidWorks machine.

    ``feature_subs`` is the ordered list of (sub_name, body) for the feature
    macros between setup and final-verify.
    """
    part_label = _vba_str(model.display_name)
    n_solid = len(feature_subs)
    lines = [
        "' ============================================================",
        "' RUN_ALL - build the entire part in one run (ordered)",
        f"' Part: {part_label}",
        "' Paste this whole file into a new SolidWorks macro (Alt+F11) and press F5",
        "' ONCE. It runs every step in build order; a failing step stops the run",
        "' and reports which step failed (see ..\\logs\\build_log.txt).",
        "' SolidWorks API works in METERS: values are written as value * UNIT_FACTOR.",
        "' ============================================================",
        "Option Explicit",
        "",
        f"Const UNIT_FACTOR As Double = {unit_factor}",
        "",
        "Dim swApp As SldWorks.SldWorks",
        "Dim swModel As SldWorks.ModelDoc2",
        "Dim boolstatus As Boolean",
        _HELPERS_VBA.rstrip("\n"),
        "",
        _FIND_TEMPLATE_VBA.rstrip("\n"),
        "",
        "Sub Step00_Setup()" + _setup_body(model, unit_factor) + "End Sub",
        "",
    ]
    for sub_name, body in feature_subs:
        lines.append(f"Sub {sub_name}()")
        lines.append(body.rstrip("\n"))
        lines.append("End Sub")
        lines.append("")
    lines.append("Sub StepZZ_FinalVerify()" + _final_verify_body(model, unit_factor, n_solid) + "End Sub")
    lines.append("")
    lines.append("Sub StepZZZ_ExportStl()" + _export_stl_body(model) + "End Sub")
    lines.append("")
    # The orchestrator: set up the app once, then run each step in order.
    lines.append("Sub main()")
    lines.append("    Set swApp = Application.SldWorks")
    lines.append('    LogResult "INFO", "RUN_ALL", "Starting full build"')
    lines.append("    Step00_Setup")
    for sub_name, _ in feature_subs:
        lines.append(f"    {sub_name}")
    lines.append("    StepZZ_FinalVerify")
    lines.append("    StepZZZ_ExportStl")
    lines.append('    LogResult "PASS", "RUN_ALL", "All steps completed"')
    lines.append('    MsgBox "RUN_ALL finished. See ..\\logs\\build_log.txt for the per-step log.", vbInformation')
    lines.append("End Sub")
    lines.append("")
    return "\n".join(lines)


_MACROS_README = """# Running these macros on the SolidWorks machine

These macros build the part **in order**. No Python needed — just SolidWorks.

## Fastest: one-click `RUN_ALL.vba`

For a single-run build, paste **`RUN_ALL.vba`** into a new macro (Alt+F11) and
press **F5 once**. It runs every step in build order with the same per-step
PASS/FAIL logging to `../logs/build_log.txt`; a failing step stops the run and
reports which step failed. Fillets/chamfers (if any) still need the interactive
edge-selection step afterwards — see step 6 below. If anything fails, fall back to
the numbered macros to isolate the step.

## Step-by-step (numbered macros)

1. Copy this whole `{folder}` folder (with `macros/` and `logs/`) to the machine.
2. Open SolidWorks 2024.
3. Tools > Macro > New… (give it any temp name) — the VBA editor opens.
4. Paste the contents of `00_setup.vba`, press **F5** (Run). It creates the part,
   sets units, and saves it next to this folder.
5. Repeat for each numbered macro **in order** (01_, 02_, …).
   - Each macro logs PASS/FAIL to `../logs/build_log.txt` and stops on failure.
   - **Stop on the first failure** — do not run later macros on a broken state.
6. `NN_fillets_chamfers.vba` (if present) is interactive: select the edge(s) in
   the graphics area first, then run the macro; it applies the exact radius /
   chamfer values from the drawing.
7. Run `ZZ_final_verify.vba` — rebuild, mass properties, bounding-box
   check against the drawing envelope, save.
8. Finish with `ZZZ_export_stl.vba` — exports `<part>.stl` next to the saved
   `.sldprt` (same base name) so the web UI's 3D viewer can load it. (RUN_ALL
   does this automatically as its last step.)

Notes
- Macros marked `TODO: VERIFY API CALL` describe a step to do manually
  (cosmetic threads, countersinks, revolves) — values are in the comments.
- If a feature's position was not readable from the drawing, the macro says
  `POSITION ASSUMED` — verify against the drawing before trusting the model.
- Check `{name}_build_plan.json` for the full step list, including anything
  skipped as prohibited (lofts/sweeps/shells are never generated).
"""


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def generate_macro_package(
    model: DrawingData,
    raw_extraction: dict[str, Any],
    verification_text: str,
    output_dir: Path | str,
    resolution: Any = None,
) -> MacroPackage:
    """Generate the complete macro package for a verified drawing.

    Args:
        model: verified DrawingData (caller must have confirmed READY status).
        raw_extraction: the extraction dict (saved verbatim for traceability).
        verification_text: the formatted verification report text.
        output_dir: base output directory (package goes in a subfolder).
        resolution: optional :class:`pipeline.resolver.ResolutionResult` from
            Stage 2.5. When provided, each feature macro emits the appropriate
            assumption-flag behavior (NOTE/MsgBox/confirmation) and the
            ``build_plan.json`` is the fully self-contained schema (drawing +
            meters dims, positions_xy, flags[], edge-selection strategy, and a
            resolution summary). When None, behavior is unchanged from v2.

    Returns:
        A :class:`MacroPackage` describing everything written.
    """
    name = _safe_name(model.display_name)
    root = Path(output_dir) / name
    macros_dir = root / "macros"
    logs_dir = root / "logs"
    macros_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    unit_factor = UNIT_FACTORS[model.units]
    pkg = MacroPackage(
        root=root,
        macros_dir=macros_dir,
        extraction_json=root / f"{name}_extraction.json",
        verification_report=root / f"{name}_verification_report.txt",
        build_plan_json=root / f"{name}_build_plan.json",
    )

    # --- Traceability artifacts ---
    pkg.extraction_json.write_text(json.dumps(raw_extraction, indent=2), encoding="utf-8")
    pkg.verification_report.write_text(verification_text, encoding="utf-8")
    (logs_dir / ".gitkeep").write_text("", encoding="utf-8")
    # Stage 2.5 resolved extraction (every dimension carries resolved_value + flags).
    if resolution is not None:
        pkg.resolved_extraction_json = root / f"{name}_resolved_extraction.json"
        pkg.resolved_extraction_json.write_text(
            json.dumps(resolution.resolved_extraction, indent=2), encoding="utf-8"
        )

    # --- 00 setup ---
    (macros_dir / "00_setup.vba").write_text(_setup_macro(model, unit_factor), encoding="utf-8")
    pkg.steps.append(
        BuildStep(0, "00_setup.vba", "-", "setup", "New part, units, save-as", "generated")
    )

    # --- Feature macros in build order; fillets/chamfers deferred to the end ---
    deferred: list[Feature] = []
    run_all_subs: list[tuple[str, str]] = []  # (sub_name, body) for RUN_ALL.vba
    seq = 0
    for fid in model.build_order:
        feature = model.feature_by_id(fid)
        if feature is None:
            continue  # validator already flagged it

        if feature.type in PROHIBITED or feature.type not in SUPPORTED:
            # NEVER silently dropped: the feature gets a numbered MANUAL-step
            # macro carrying its dimensions and instructions, and is flagged
            # CRITICAL in the engineering review. The macro creates no geometry.
            seq += 1
            step_name = f"{seq:02d}_{feature.id}"
            fname = f"{seq:02d}_{feature.id}_MANUAL_{_vba_name(feature.type.value)}.vba"
            dims = _dims_map(model, feature)
            dim_lines = "".join(
                f"    '   {k} = {_v(v)} (drawing units)\n" for k, v in dims.items()
            ) or "    '   (no linked dimensions extracted)\n"
            desc = _vba_str(feature.description, 200)
            body = f"""    ' ============ MANUAL STEP - NO GEOMETRY IS CREATED HERE ============
    ' Feature {feature.id} ({feature.type.value}) cannot be scripted reliably.
    ' Build it by hand in SolidWorks using the drawing and these values:
{dim_lines}    ' Description: {desc}
    ' When done, re-run this macro so the build log records the step.
    MsgBox "MANUAL STEP {step_name}: build feature {feature.id} ({feature.type.value}) by hand." & vbCrLf & _
        "{desc}" & vbCrLf & "See this macro's comments for the extracted values.", _
        vbExclamation, "Manual step required - {feature.id}"
    LogResult "WARN", "{step_name}", "{feature.id} ({feature.type.value}) requires MANUAL modeling - no geometry created"
"""
            header = _vba_header(
                f"{step_name} - MANUAL STEP - {feature.type.value}: {feature.description}",
                model.display_name, unit_factor,
            )
            (macros_dir / fname).write_text(header + body + _vba_footer(), encoding="utf-8")
            run_all_subs.append((f"Step{seq:02d}_{_vba_identifier(feature.id)}_Manual", body))
            step = BuildStep(
                seq, fname, feature.id, feature.type.value, feature.description,
                "skipped_prohibited",
                notes=f"FEATURE {feature.id} SKIPPED by automation: {feature.type.value} is "
                      f"prohibited/unsupported. A numbered MANUAL step macro ({fname}) "
                      "was generated instead.",
            )
            _enrich_feature_step(step, model, feature, resolution,
                                 _collect_step_flags(model, feature, resolution))
            step.requires_input = True
            pkg.skipped.append(step)
            pkg.steps.append(step)
            log.warning("%s", step.notes)
            continue

        if feature.type in (FeatureType.FILLET, FeatureType.CHAMFER):
            deferred.append(feature)
            continue

        # Must-meet circular-pattern route: seed hole -> named reference axis ->
        # FeatureCircularPattern (three numbered macros). Falls through to the
        # baked-circles path when the axis cannot be derived deterministically.
        if feature.type == FeatureType.HOLE:
            h_route = model.hole_callout_for_feature(feature.id)
            if route_to_circular_pattern(model, h_route):
                new_seq = _emit_circular_pattern_trio(
                    model, feature, h_route, seq, macros_dir, unit_factor,
                    run_all_subs, pkg, resolution,
                )
                if new_seq is not None:
                    seq = new_seq
                    continue
                log.info("%s: circular pattern requested but no concentric bore "
                         "face to derive the axis — using baked-circle instances.",
                         feature.id)

        seq += 1
        step_name = f"{seq:02d}_{feature.id}"
        fname = f"{seq:02d}_{feature.id}_{_vba_name(feature.description)}.vba"
        status, notes, used = "generated", "", {}

        header = _vba_header(
            f"{step_name} - {feature.type.value}: {feature.description}",
            model.display_name, unit_factor,
        )
        try:
            if feature.type == FeatureType.EXTRUDE_BOSS:
                body, used, notes = _macro_extrude(model, feature, step_name, is_cut=False)
            elif feature.type == FeatureType.EXTRUDE_CUT:
                body, used, notes = _macro_extrude(model, feature, step_name, is_cut=True)
            elif feature.type == FeatureType.HOLE:
                body, used, notes = _macro_holes(model, feature, step_name)
            elif feature.type == FeatureType.THREAD:
                body = _macro_holes(model, feature, step_name)[0] if model.hole_callout_for_feature(feature.id) else ""
                if not body:
                    body = f"""    ' TODO: VERIFY API CALL — cosmetic thread for {feature.id}
    ' Apply via Insert > Annotations > Cosmetic Thread. {feature.description}
    LogResult "WARN", "{step_name}", "{feature.id} cosmetic thread - apply manually"
"""
                status, notes = "needs_review", "Cosmetic thread step requires manual verification."
            elif feature.type == FeatureType.REVOLVE:
                real = _macro_revolve(model, feature, step_name)
                if real is not None:
                    body, used, notes = real
                else:
                    body = _macro_revolve_skeleton(feature, step_name)
                    status, notes = "needs_review", "Revolve has no extracted profile — manual modeling (see macro)."
            elif feature.type == FeatureType.MIRROR:
                real = _macro_mirror(model, feature, step_name)
                if real is not None:
                    body, used, notes = real
                else:
                    body = (
                        f'    MsgBox "Feature {feature.id} (mirror): set parent_feature to the '
                        f'feature to mirror, then build manually.", vbExclamation\n'
                        f'    LogResult "WARN", "{step_name}", "{feature.id} mirror: no seed feature"\n'
                    )
                    status, notes = "needs_review", "Mirror has no seed feature — manual modeling (see macro)."
            elif feature.type == FeatureType.PATTERN:
                covered = _pattern_covered_by(model, feature)
                if covered is not None:
                    parent_id, qty = covered
                    body = _macro_pattern_covered(parent_id, qty, feature, step_name)
                    notes = f"Pattern already realized by {parent_id}'s hole cut ({qty} instances)."
                else:
                    body = _macro_pattern_skeleton(model, feature, step_name)
                    status, notes = "needs_review", "Pattern left for manual application (see macro)."
            else:  # pragma: no cover — guarded by SUPPORTED above
                raise MacroGenerationError(f"No builder for {feature.type.value}")
        except MacroGenerationError as e:
            status, notes = "needs_review", str(e)
            msg = _vba_str(str(e))
            body = f"""    ' GENERATION ISSUE: {msg}
    ' This feature could not be scripted from the extracted data - build manually.
    MsgBox "Feature {feature.id}: {msg}", vbExclamation
    LogResult "WARN", "{step_name}", "Not scripted: {msg}"
"""

        # Stage 2.5: emit assumption-flag behavior (NOTE/MsgBox/confirmation) at
        # the top of the macro body, then the feature body itself.
        step_flags = _collect_step_flags(model, feature, resolution)
        body = _flag_vba_block(step_name, step_flags) + body

        (macros_dir / fname).write_text(header + body + _vba_footer(), encoding="utf-8")
        run_all_subs.append((f"Step{seq:02d}_{_vba_identifier(feature.id)}", body))
        step = BuildStep(seq, fname, feature.id, feature.type.value, feature.description,
                         status, dimensions=used, notes=notes)
        _enrich_feature_step(step, model, feature, resolution, step_flags)
        pkg.steps.append(step)
        if status == "needs_review":
            pkg.needs_review.append(step)

    # --- Deferred fillets/chamfers (always last) ---
    if deferred:
        seq += 1
        fname = f"{seq:02d}_fillets_chamfers.vba"
        header = _vba_header(f"{seq:02d}_fillets_chamfers - applied LAST", model.display_name, unit_factor)
        body, used = _macro_fillet_chamfer(model, deferred, f"{seq:02d}_fillets_chamfers")
        fc_flags = []
        for f in deferred:
            fc_flags.extend(_collect_step_flags(model, f, resolution))
        body = _flag_vba_block(f"{seq:02d}_fillets_chamfers", fc_flags) + body
        (macros_dir / fname).write_text(header + body + _vba_footer(), encoding="utf-8")
        run_all_subs.append((f"Step{seq:02d}_FilletsChamfers", body))
        step = BuildStep(
            seq, fname, ",".join(f.id for f in deferred), "fillet/chamfer",
            "Interactive: select edges, run, repeat", "generated", dimensions=used,
            notes="Run last. Interactive edge selection (values from the drawing are baked in).",
        )
        # Self-contained edge-selection contract for the macro generator/consumer.
        step.dimensions_meters = _dims_in_meters(used, model.units)
        step.requires_input = True
        step.auto_select_strategy = "parent_feature_sketch_edges"
        step.parent_feature_id = next((f.parent_feature for f in deferred if f.parent_feature), "")
        step.expected_edge_count = 0  # unknown from a 2D drawing; selection is interactive
        step.edge_selection_note = (
            "Select the edge(s) for each fillet/chamfer in the graphics area, then run. "
            "The macro applies the exact radius/chamfer values baked in from the drawing; "
            "if no edges are selected it prompts and skips that value."
        )
        step.flags = fc_flags
        pkg.steps.append(step)

    # --- Final verify ---
    n_solid = sum(1 for s in pkg.steps if s.status == "generated" and s.seq > 0)
    (macros_dir / "ZZ_final_verify.vba").write_text(
        _final_verify_macro(model, unit_factor, n_solid), encoding="utf-8"
    )
    pkg.steps.append(BuildStep(999, "ZZ_final_verify.vba", "-", "verify",
                               "Rebuild, mass properties, bounding box, save", "generated"))

    # --- Export STL (runs last; sorts after ZZ_final_verify) ---
    (macros_dir / "ZZZ_export_stl.vba").write_text(
        _export_stl_macro(model, unit_factor), encoding="utf-8"
    )
    pkg.steps.append(BuildStep(1001, "ZZZ_export_stl.vba", "-", "export",
                               "Export the part as an STL beside the .sldprt (same base name)",
                               "generated"))

    # --- RUN_ALL.vba: one-click, in-order build (no installs on the SW machine) ---
    (macros_dir / "RUN_ALL.vba").write_text(
        _build_run_all(model, unit_factor, run_all_subs), encoding="utf-8"
    )
    pkg.steps.append(BuildStep(1000, "RUN_ALL.vba", "-", "run_all",
                               "One macro that runs every step in order (paste once, F5)",
                               "generated",
                               notes="Single-run alternative to the numbered macros. "
                                     "Fillets/chamfers still need interactive edge selection."))

    # --- README + build plan ---
    (macros_dir / "README.md").write_text(
        _MACROS_README.format(folder=name, name=name), encoding="utf-8"
    )
    # --- Static self-validation of the emitted macros (Phase 7 + Phase 10) ---
    # Every E0xx lesson is enforced here over the WHOLE package, not just on test
    # fixtures. Hard errors (banned/nonexistent APIs, unbalanced blocks) mean a
    # generator regression — fail loudly so the bad macro can never ship.
    audit = audit_package(macros_dir)
    write_audit_report(audit, root / f"{name}_audit_report.json")
    if not audit.ok:
        detail = "; ".join(f"[{f.rule_id}] {f.file}: {f.message}" for f in audit.errors)
        raise MacroGenerationError(
            f"Generated macros failed static self-validation: {detail}"
        )
    for w in audit.warnings:
        log.warning("macro audit [%s] %s: %s", w.rule_id, w.file, w.message)

    plan = _build_plan_dict(model, pkg, unit_factor, audit, resolution)
    pkg.build_plan_json.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    # --- Severity-ranked engineering review (first-class human-facing output) ---
    # Written now from the resolver + macro data; the batch driver rewrites it
    # after the .sldprt build to fold in any COM-skipped features/caveats.
    from pipeline.engineering_review import write_review

    write_review(root, name, plan["engineering_review"], resolution=resolution)

    log.info(
        "Macro package written to %s (%d macros, %d skipped, %d need review)",
        root, sum(1 for s in pkg.steps if s.macro_file.endswith(".vba")),
        len(pkg.skipped), len(pkg.needs_review),
    )
    return pkg
