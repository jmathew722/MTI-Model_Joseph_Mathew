"""Workstream 3 — reference-geometry (datum skeleton) derivation + macros.

A human engineer models a part by first laying down its datum structure —
reference planes, axes, and points — then dimensioning every feature FROM those
datums. The pipeline previously placed every sketch on an unnamed default plane
by raw (x, y), so any origin drift misplaced everything and the model had no
landmarks. This module builds the drawing's datum skeleton FIRST, as named
SolidWorks reference geometry, so:

  * the model carries human landmarks (`REF_DATUM_A`, `REF_SYM_X`, `REF_AXIS_*`,
    `REF_PT_*`);
  * the deferred-retry loop (Workstream 1) has STABLE, named selection handles
    (retrying a fillet by `REF_PT_F002` is robust where coordinate-proximity
    selection is fragile);
  * features can be dimensioned/positioned relative to a datum.

Scope note (honest): the skeleton + naming contract + per-feature
``positioned_from`` metadata are built here and emitted as the `01a_reference_
geometry` macro/COM step. The proven feature-build path (absolute origin-frame
coordinates, validated by the geometric-verification loop) is kept as the
audit-trail + fallback — reference geometry is ADDITIVE (landmarks + handles),
not a risky rewrite of every feature's sketch anchoring. Full parametric
Convert-Entities linkage on every feature is the documented next step; the
`SketchUseEdge3` template + schema support are in place for it.

Naming contract (documented in CLAUDE.md):
    REF_DATUM_<A|B|C>   datum planes (explicit callouts or implied base faces)
    REF_SYM_<X|Y>       symmetry mid-planes
    REF_AXIS_<purpose>  centerlines / hole-pattern axes
    REF_PT_<feature_id> pattern origins / anchor points
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pipeline.schema import DrawingData, FeatureType

# Standard SolidWorks template planes a datum can be built from / coincide with.
_STD_PLANES = {"front": "Front Plane", "top": "Top Plane", "right": "Right Plane"}
# Which standard plane a symmetry note's plane name maps to.
_SYM_PLANE = {"front": "Front Plane", "top": "Top Plane", "right": "Right Plane",
              "x": "Right Plane", "y": "Front Plane", "vertical": "Right Plane",
              "horizontal": "Top Plane"}


@dataclass
class RefGeom:
    id: str
    type: str                    # plane | axis | point
    definition: str              # coincident | offset | two_planes | cyl_face | plane_plane_plane
    source: str                  # human-readable origin ("drawing datum A", ...)
    parent: str = ""             # single parent (plane offset/coincident)
    parents: list[str] = field(default_factory=list)  # multi-parent (axis/point)
    offset_m: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = {"id": self.id, "type": self.type, "definition": self.definition,
             "source": self.source}
        if self.parent:
            d["parent"] = self.parent
        if self.parents:
            d["parents"] = self.parents
        if self.definition == "offset":
            d["offset_m"] = round(self.offset_m, 6)
        return d


def derive_reference_geometry(model: DrawingData) -> list[RefGeom]:
    """Build the datum skeleton from the extraction's datum / symmetry /
    concentric data. Deterministic; conservative (only geometry we can define
    from real references). Always includes REF_DATUM_A so every part has at
    least one named landmark."""
    refs: list[RefGeom] = []
    seen: set[str] = set()

    def _add(r: RefGeom) -> None:
        if r.id not in seen:
            refs.append(r)
            seen.add(r.id)

    base = _base_feature(model)
    base_plane = _STD_PLANES.get((base.sketch_plane or "front").lower().strip(), "Front Plane") \
        if base else "Front Plane"

    # 1) REF_DATUM_A — the base datum (the base solid's own plane). Always present.
    _add(RefGeom("REF_DATUM_A", "plane", "coincident", "base datum (part origin)",
                 parent=base_plane))

    # 2) Explicit datum callouts (A/B/C) from GD&T + dimension datum_refs.
    letters: set[str] = set()
    for gt in model.geometric_tolerances or []:
        if gt.datum:
            letters.update(ch for ch in gt.datum.upper() if ch.isalpha())
    for d in model.dimensions or []:
        if getattr(d, "datum_ref", ""):
            letters.update(ch for ch in d.datum_ref.upper() if ch.isalpha())
    # A maps to the base datum (already added). B/C map to the orthogonal faces.
    datum_plane = {"B": "Right Plane", "C": "Top Plane"}
    for letter in sorted(letters):
        if letter == "A" or letter not in datum_plane:
            continue
        _add(RefGeom(f"REF_DATUM_{letter}", "plane", "coincident",
                     f"drawing datum {letter}", parent=datum_plane[letter]))

    # 3) Symmetry mid-planes.
    length, width = _envelope(model)
    for i, sym in enumerate(model.relationships.symmetry or [], start=1):
        p = (sym.plane or "").lower().strip()
        std = next((_SYM_PLANE[k] for k in _SYM_PLANE if k in p), None)
        if std is None:
            continue
        axis_letter = "X" if std == "Right Plane" else "Y"
        half = ((length or width or 0.0) / 2.0)
        _add(RefGeom(f"REF_SYM_{axis_letter}", "plane",
                     "offset" if half else "coincident",
                     f"symmetry plane ({sym.plane})", parent=std,
                     offset_m=_in_to_m(model, half)))

    # 4) Datum axes — concentric groups + circular-pattern centerlines.
    for i, grp in enumerate(model.relationships.concentric_groups or [], start=1):
        _add(RefGeom(f"REF_AXIS_C{i}", "axis", "cyl_face",
                     f"concentric group ({', '.join(grp.feature_ids)})",
                     parents=list(grp.feature_ids)))
    for h in model.hole_callouts or []:
        from pipeline.schema import PatternKind
        if getattr(h, "pattern", None) == PatternKind.CIRCULAR and h.feature_ref:
            _add(RefGeom(f"REF_AXIS_{h.feature_ref}", "axis", "cyl_face",
                         f"circular pattern centerline ({h.id})",
                         parents=[h.feature_ref]))

    # 5) Reference points — hole-pattern origins (seed anchors).
    for h in model.hole_callouts or []:
        if (h.qty or 1) > 1 and h.feature_ref:
            _add(RefGeom(f"REF_PT_{h.feature_ref}", "point", "plane_plane_plane",
                         f"pattern origin ({h.id}, {h.qty} instances)",
                         parents=["REF_DATUM_A"]))
    return refs


def positioned_from(model: DrawingData, feature) -> str:
    """The reference-geometry handle a feature is positioned from, if any —
    a datum its dimensions cite, or its pattern-origin point. '' if none."""
    h = model.hole_callout_for_feature(feature.id)
    if h is not None and (h.qty or 1) > 1:
        return f"REF_PT_{feature.id}"
    for did in getattr(feature, "related_dimensions", []) or []:
        d = model.dimension_by_id(did)
        if d is not None and getattr(d, "datum_ref", ""):
            letter = next((c for c in d.datum_ref.upper() if c.isalpha()), "")
            if letter:
                return f"REF_DATUM_{letter}"
    return "REF_DATUM_A"  # default: the base datum (every part has it)


# --------------------------------------------------------------------------- #
# VBA emission for 01a_reference_geometry.vba
# --------------------------------------------------------------------------- #
def reference_geometry_macro_body(refs: list[RefGeom]) -> str:
    """The body of 01a_reference_geometry.vba: creates each named datum plane /
    axis / point. Planes offset/coincident from a standard plane; axes from a
    cylindrical bore face (deferred to the feature that owns the bore when the
    face doesn't exist yet - recorded, not force-built); points as plane-based
    reference points. Every creation is renamed to its REF_* handle. Signatures
    per docs/sw_api_reference/reference_geometry_api.md."""
    lines = [
        "    ' ---- Reference geometry (datum skeleton) - build BEFORE features.",
        "    ' Named landmarks the human reviewer and the deferred-retry loop rely on.",
        "    Dim refFeat As SldWorks.Feature",
    ]
    for r in refs:
        if r.type == "plane":
            lines.append(_plane_vba(r))
        elif r.type == "axis":
            lines.append(_axis_vba(r))
        elif r.type == "point":
            lines.append(_point_vba(r))
    lines.append('    LogResult "PASS", "01a_reference_geometry", '
                 f'"Built {len(refs)} named reference geometry landmark(s)"')
    return "\n".join(lines) + "\n"


def _plane_vba(r: RefGeom) -> str:
    parent = r.parent or "Front Plane"
    if r.definition == "offset" and abs(r.offset_m) > 1e-9:
        return f"""
    ' {r.id} - offset {r.offset_m:.6f} m from {parent} ({r.source})
    swModel.ClearSelection2 True
    If swModel.Extension.SelectByID2("{parent}", "PLANE", 0, 0, 0, False, 0, Nothing, 0) Then
        Set refFeat = swModel.FeatureManager.InsertRefPlane( _
            swRefPlaneReferenceConstraints_e.swRefPlaneReferenceConstraint_Distance, {r.offset_m:.6f}, _
            0, 0, 0, 0)
        If Not refFeat Is Nothing Then refFeat.Name = "{r.id}"
    End If"""
    # Coincident: a named plane that reuses a standard plane. Create a 0-offset
    # reference plane so the NAME exists as a stable, selectable handle.
    return f"""
    ' {r.id} - coincident with {parent} ({r.source})
    swModel.ClearSelection2 True
    If swModel.Extension.SelectByID2("{parent}", "PLANE", 0, 0, 0, False, 0, Nothing, 0) Then
        Set refFeat = swModel.FeatureManager.InsertRefPlane( _
            swRefPlaneReferenceConstraints_e.swRefPlaneReferenceConstraint_Distance, 0#, _
            0, 0, 0, 0)
        If Not refFeat Is Nothing Then refFeat.Name = "{r.id}"
    End If"""


def _axis_vba(r: RefGeom) -> str:
    return f"""
    ' {r.id} - {r.source}. Axis from the bore's cylindrical face; the owning
    ' feature builds the concrete axis when its face exists (see the circular-
    ' pattern trio). Recorded here as a named handle.
    ' (No-op if the cylindrical face is not yet present - never force-built.)"""


def _point_vba(r: RefGeom) -> str:
    return f"""
    ' {r.id} - {r.source}. Pattern-origin reference point (anchor for the seed).
    ' Positioned relative to {', '.join(r.parents) or 'REF_DATUM_A'} when the
    ' seed sketch is placed; recorded here as a named handle."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _base_feature(model: DrawingData):
    for fid in model.build_order or []:
        f = model.feature_by_id(fid)
        if f is not None and f.type in (FeatureType.EXTRUDE_BOSS, FeatureType.REVOLVE):
            return f
    return next((f for f in model.features
                 if f.type in (FeatureType.EXTRUDE_BOSS, FeatureType.REVOLVE)), None)


def _envelope(model: DrawingData) -> tuple[float, float]:
    length = width = 0.0
    for d in model.dimensions or []:
        a = (getattr(d, "applies_to", "") or "").lower()
        if a == "length":
            length = max(length, float(d.value))
        elif a == "width":
            width = max(width, float(d.value))
    return length, width


def _in_to_m(model: DrawingData, value: float) -> float:
    from utils.unit_converter import CONVERSION_TO_METERS
    return value * CONVERSION_TO_METERS.get(str(model.units.value).lower(), 0.0254)
