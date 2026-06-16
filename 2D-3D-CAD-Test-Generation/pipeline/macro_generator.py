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
PLANE_NAMES = {"front": "Front Plane", "top": "Top Plane", "right": "Right Plane"}
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
    FeatureType.THREAD,   # cosmetic thread only (TODO-marked)
    FeatureType.REVOLVE,  # skeleton + needs_review
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


@dataclass
class MacroPackage:
    root: Path
    macros_dir: Path
    extraction_json: Path
    verification_report: Path
    build_plan_json: Path
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


def _hole_positions(model: DrawingData, h: HoleCallout) -> list[tuple[float, float]]:
    """Instance centers in the DRAWING FRAME (base plate lower-left corner at origin).

    Known positions are used as-is — drawings dimension hole centers from the
    part edges, which is exactly this frame. When positions are unknown but a
    spacing can be GROUNDED in extracted data (the callout's pattern_spacing or a
    structured equal_spacing relationship), instances are laid out as a centered
    row about the plate envelope. With no such evidence, a single instance is
    placed at the envelope center and the macro flags POSITION ASSUMED — positions
    are never invented from free text.
    """
    length, width = _envelope(model)
    ecx = (length / 2.0) if length else 0.0
    ecy = (width / 2.0) if width else 0.0
    spacing, qty = _effective_spacing(model, h)
    # A grounded spacing lays out a centered row for linear/unspecified patterns.
    # Circular patterns lack a bolt-circle radius in the schema, so they are left
    # to the single-instance fallback rather than guessed.
    linear_like = h.pattern in (PatternKind.LINEAR, PatternKind.NONE)
    if linear_like and qty > 1 and spacing > 0:
        if h.position_known:
            x0, y0 = h.x_position, h.y_position
        else:
            span = (qty - 1) * spacing
            x0, y0 = ecx - span / 2.0, ecy
        return [(x0 + i * spacing, y0) for i in range(qty)]
    # Single position (or qty>1 with no grounded spacing — macro comments flag it).
    if h.position_known:
        return [(h.x_position, h.y_position)]
    return [(ecx, ecy)]


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
                body += f"\n    ' {f.id}: SKIPPED - no radius dimension found.\n"
                continue
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
                body += f"\n    ' {f.id}: SKIPPED - no distance dimension found.\n"
                continue
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
    # The orchestrator: set up the app once, then run each step in order.
    lines.append("Sub main()")
    lines.append("    Set swApp = Application.SldWorks")
    lines.append('    LogResult "INFO", "RUN_ALL", "Starting full build"')
    lines.append("    Step00_Setup")
    for sub_name, _ in feature_subs:
        lines.append(f"    {sub_name}")
    lines.append("    StepZZ_FinalVerify")
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
7. Finish with `ZZ_final_verify.vba` — rebuild, mass properties, bounding-box
   check against the drawing envelope, save.

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
) -> MacroPackage:
    """Generate the complete macro package for a verified drawing.

    Args:
        model: verified DrawingData (caller must have confirmed READY status).
        raw_extraction: the extraction dict (saved verbatim for traceability).
        verification_text: the formatted verification report text.
        output_dir: base output directory (package goes in a subfolder).

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
    pkg.extraction_json.write_text(json.dumps(raw_extraction, indent=2))
    pkg.verification_report.write_text(verification_text)
    (logs_dir / ".gitkeep").write_text("")

    # --- 00 setup ---
    (macros_dir / "00_setup.vba").write_text(_setup_macro(model, unit_factor))
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
            step = BuildStep(
                -1, "-", feature.id, feature.type.value, feature.description,
                "skipped_prohibited",
                notes=f"FEATURE {feature.id} SKIPPED: {feature.type.value} is prohibited/unsupported. "
                      "Manual modeling required.",
            )
            pkg.skipped.append(step)
            pkg.steps.append(step)
            log.warning("%s", step.notes)
            continue

        if feature.type in (FeatureType.FILLET, FeatureType.CHAMFER):
            deferred.append(feature)
            continue

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
                body = _macro_revolve_skeleton(feature, step_name)
                status, notes = "needs_review", "Revolve requires manual modeling (see macro)."
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

        (macros_dir / fname).write_text(header + body + _vba_footer())
        run_all_subs.append((f"Step{seq:02d}_{_vba_identifier(feature.id)}", body))
        step = BuildStep(seq, fname, feature.id, feature.type.value, feature.description,
                         status, dimensions=used, notes=notes)
        pkg.steps.append(step)
        if status == "needs_review":
            pkg.needs_review.append(step)

    # --- Deferred fillets/chamfers (always last) ---
    if deferred:
        seq += 1
        fname = f"{seq:02d}_fillets_chamfers.vba"
        header = _vba_header(f"{seq:02d}_fillets_chamfers - applied LAST", model.display_name, unit_factor)
        body, used = _macro_fillet_chamfer(model, deferred, f"{seq:02d}_fillets_chamfers")
        (macros_dir / fname).write_text(header + body + _vba_footer())
        run_all_subs.append((f"Step{seq:02d}_FilletsChamfers", body))
        step = BuildStep(
            seq, fname, ",".join(f.id for f in deferred), "fillet/chamfer",
            "Interactive: select edges, run, repeat", "generated", dimensions=used,
            notes="Run last. Interactive edge selection (values from the drawing are baked in).",
        )
        pkg.steps.append(step)

    # --- Final verify ---
    n_solid = sum(1 for s in pkg.steps if s.status == "generated" and s.seq > 0)
    (macros_dir / "ZZ_final_verify.vba").write_text(
        _final_verify_macro(model, unit_factor, n_solid)
    )
    pkg.steps.append(BuildStep(999, "ZZ_final_verify.vba", "-", "verify",
                               "Rebuild, mass properties, bounding box, save", "generated"))

    # --- RUN_ALL.vba: one-click, in-order build (no installs on the SW machine) ---
    (macros_dir / "RUN_ALL.vba").write_text(
        _build_run_all(model, unit_factor, run_all_subs)
    )
    pkg.steps.append(BuildStep(1000, "RUN_ALL.vba", "-", "run_all",
                               "One macro that runs every step in order (paste once, F5)",
                               "generated",
                               notes="Single-run alternative to the numbered macros. "
                                     "Fillets/chamfers still need interactive edge selection."))

    # --- README + build plan ---
    (macros_dir / "README.md").write_text(
        _MACROS_README.format(folder=name, name=name)
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

    plan = {
        "part": model.display_name,
        "units": model.units.value,
        "unit_factor_to_meters": unit_factor,
        "confidence": model.confidence,
        "audit": audit.to_dict(),
        "steps": [
            {
                "seq": s.seq, "macro_file": s.macro_file, "feature_id": s.feature_id,
                "type": s.feature_type, "description": s.description, "status": s.status,
                "dimensions_drawing_units": s.dimensions, "notes": s.notes,
            }
            for s in pkg.steps
        ],
        "skipped_prohibited": [s.feature_id for s in pkg.skipped],
        "needs_review": [s.feature_id for s in pkg.needs_review],
    }
    pkg.build_plan_json.write_text(json.dumps(plan, indent=2))

    log.info(
        "Macro package written to %s (%d macros, %d skipped, %d need review)",
        root, sum(1 for s in pkg.steps if s.macro_file.endswith(".vba")),
        len(pkg.skipped), len(pkg.needs_review),
    )
    return pkg
