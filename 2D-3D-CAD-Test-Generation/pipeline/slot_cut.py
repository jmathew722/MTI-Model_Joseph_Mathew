"""Canonical slot / U-notch decomposition (2026-07-11).

THE RULE: a U-shaped cutout, open notch, or slot is NEVER built as a single
arc-bearing sketch. It is decomposed into exactly two features, in order:

  A. slot_rect_cut     — rectangular through-cut at the exact dimensioned
                         position. MANDATORY, must_complete: it carries the
                         slot's position + size truth. A rectangle (4 lines,
                         4 dims) is near-unfailable in the API.
  B. slot_corner_fillet — constant-radius fillets on the rectangle's interior
                         corners. Cosmetic-geometric; defer-on-failure (the slot
                         is already correct from A). The R TYP callout is a
                         corner treatment on the rectangle, not a profile arc.

This module owns the slot geometry (corner array — the ONE source of truth the
rectangle sketch AND the fillet edge-selection both derive from), the resolver
validation rules, and the legacy extrude_cut+fillet -> slot_cut normalizer.

Public: :func:`corner_array`, :func:`interior_corners`, :func:`validate_slot`,
:func:`normalize_legacy_slots`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

_NOTCH_KEYWORDS = ("notch", "slot", "u-cut", "u cut", "u-shape", "u shape", "cutout",
                   "keyway", "channel")

# Open-edge overshoot (drawing units). A cut that opens through an outer edge
# must NEVER sketch exactly to that edge — a coincident-with-edge line is
# numerically fragile and the observed 158-C defect was an enclosed WINDOW
# (material standing at the drawn-open y=6.25 edge) instead of an open notch.
# The open side's corners are pushed PAST the edge by this margin; the closed
# sides stay exact, and interior_corners()/the corner fillets are unaffected
# because they are always the closed-side pair. Emission refuses to generate an
# open-edge cut whose sketch terminates within the edge (macro_generator's
# overshoot invariant), so this constant is the single knob.
EDGE_OVERSHOOT_EPS = 0.050


def _envelope(model) -> tuple[float, float]:
    """(horizontal_extent, vertical_extent) of the part in drawing units, from
    the length/width dimensions (best available)."""
    length = width = 0.0
    for d in getattr(model, "dimensions", []) or []:
        a = (getattr(d, "applies_to", "") or "").lower()
        if a == "length":
            length = max(length, float(d.value))
        elif a in ("width", "height"):
            width = max(width, float(d.value))
    return length, width


def corner_array(slot, model) -> list[list[float]]:
    """The rectangle's 4 corners in drawing units, lower-left-origin frame
    (+X right, +Y up) — the SINGLE source of truth for both the sketch and the
    fillet edge-selection, so the fillet can never target a different location
    than the rectangle that was cut. Order: the two NEAR/interior corners first
    (the ones that get filleted on an open notch), then the two edge corners.

    For an open notch the depth runs inward from the broken edge and the OPEN
    side's corners overshoot the part edge by EDGE_OVERSHOOT_EPS (closed sides
    stay exact — see the constant's docstring); for a closed slot the rectangle
    sits fully interior anchored at (anchor_offset, ...)."""
    length, height = _envelope(model)
    a = float(slot.anchor_offset)
    w = float(slot.width)
    d = float(slot.depth)
    edge = (slot.open_edge or "").lower()
    eps = EDGE_OVERSHOOT_EPS

    # anchor_semantics: a centerline-referenced position is to the slot CENTER,
    # so the near edge is half a width inboard.
    if slot.anchor_semantics == "edge_to_centerline":
        a = a - w / 2.0

    if edge == "top":
        top = height or (a + d)      # part top; falls back if height unknown
        bot = top - d                # closed end (interior) — exact
        open_y = top + eps           # open end — crosses the top edge
        return [[a, bot], [a + w, bot], [a + w, open_y], [a, open_y]]
    if edge == "bottom":
        open_y = -eps
        return [[a, d], [a + w, d], [a + w, open_y], [a, open_y]]
    if edge == "left":
        open_x = -eps
        return [[d, a], [d, a + w], [open_x, a + w], [open_x, a]]
    if edge == "right":
        right = length or (a + d)
        open_x = right + eps
        return [[right - d, a], [right - d, a + w], [open_x, a + w], [open_x, a]]
    # Closed slot: a fully-interior rectangle anchored at (anchor_offset) along
    # the horizontal, depth = slot length vertically. No overshoot — no open edge.
    return [[a, 0.0], [a + w, 0.0], [a + w, d], [a, d]]


def interior_corners(slot, corners: list[list[float]]) -> list[list[float]]:
    """The corners that get filleted: 2 for an open notch (the interior pair,
    listed first in corner_array), all 4 for a closed slot / obround."""
    kind = (slot.slot_kind or "open_notch").lower()
    if kind == "open_notch":
        return corners[:2]
    return list(corners)  # closed_slot + obround fillet all 4


def expected_corner_count(slot) -> int:
    return 2 if (slot.slot_kind or "open_notch").lower() == "open_notch" else 4


# --------------------------------------------------------------------------- #
# Resolver validation (Stage 2.5) — run before the slot reaches the build plan
# --------------------------------------------------------------------------- #
def validate_slot(slot, model) -> list[dict[str, Any]]:
    """Slot-specific validation. Returns flag dicts (empty = clean). A geometry
    violation is CRITICAL + a ready-made clarification-gate question; an
    ambiguous convention is MEDIUM. Never mutates fabricated values in."""
    flags: list[dict[str, Any]] = []
    length, height = _envelope(model)
    a, w, d, r = (float(slot.anchor_offset), float(slot.width), float(slot.depth),
                  float(slot.corner_radius))

    def _flag(tier: str, note: str, gate: str = "") -> None:
        f = {"feature_id": slot.id, "dimension_id": slot.anchor_dimension_id or slot.id,
             "flag_tier": tier, "human_note": note, "source": "slot_cut"}
        if gate:
            f["gate_question"] = gate
        flags.append(f)

    # Fit check: anchor_offset + width <= part dimension along the anchor axis.
    axis_extent = length if (slot.open_edge or "").lower() in ("top", "bottom", "") else height
    if axis_extent and (a + w) > axis_extent + 1e-6:
        _flag("CRITICAL",
              f"Slot {slot.id} does not fit: anchor {a:g} + width {w:g} = {a + w:g} exceeds the "
              f"part extent {axis_extent:g} along that axis — a dimension was likely misread.",
              gate=f"Slot {slot.id}: does it start {a:g} from the {slot.anchor_edge} edge, or is "
                   f"one of {a:g}/{w:g} misread? {a:g}+{w:g} overruns the {axis_extent:g} part.")

    # Radius check: 2R <= width AND R <= depth.
    if r > 0:
        if 2.0 * r > w + 1e-6:
            _flag("CRITICAL",
                  f"Slot {slot.id} corner radius {r:g} too large: 2R = {2 * r:g} exceeds width "
                  f"{w:g}. Radius is never clamped silently.",
                  gate=f"Slot {slot.id}: corner radius {r:g} vs width {w:g} — 2R exceeds the width. "
                       "Confirm the radius and the width.")
        if r > d + 1e-6:
            _flag("CRITICAL",
                  f"Slot {slot.id} corner radius {r:g} exceeds depth {d:g}.",
                  gate=f"Slot {slot.id}: corner radius {r:g} exceeds depth {d:g}. Confirm both.")
        # 2R == width on a closed slot -> it is really an obround (full-radius ends).
        if abs(2.0 * r - w) <= 1e-6 and (slot.slot_kind or "").lower() == "closed_slot":
            slot.slot_kind = "obround"
            _flag("MEDIUM", f"Slot {slot.id}: 2R equals width -> reclassified as an obround "
                            "(full-radius ends).")

    # Anchor semantics: default edge_to_near_edge; if unknown, MEDIUM (the two
    # interpretations differ by width/2 — a classic silent-misplacement bug).
    if slot.anchor_semantics not in ("edge_to_near_edge", "edge_to_centerline"):
        _flag("MEDIUM",
              f"Slot {slot.id}: position convention unclear (edge-to-near-edge vs "
              f"edge-to-centerline differ by width/2 = {w / 2:g}). Assuming near-edge.",
              gate=f"Slot {slot.id}: is the {a:g} dimension to the slot's NEAR EDGE or to its "
                   "CENTERLINE? (They differ by half the width.)")
        slot.anchor_semantics = "edge_to_near_edge"

    # Through-all inference (single-view rule): only 1 orthographic view + no
    # depth-conflicting callout -> thru=True, single_view_default, MEDIUM + gate.
    n_ortho = sum(1 for v in (getattr(model, "views", []) or [])
                  if (getattr(v, "view_type", "") or "").lower() in
                  ("front", "top", "right", "side", "bottom", "left", "back"))
    if slot.thru_basis == "single_view_default" and n_ortho <= 1:
        _flag("MEDIUM",
              f"Slot {slot.id}: only one orthographic view — assumed a THROUGH cut "
              "(single-view default).",
              gate=f"Only one view provided — confirm the {w:g}x{d:g} slot at {a:g} from the "
                   f"{slot.anchor_edge} edge is a THROUGH-cut, not a blind pocket.")
    return flags


# --------------------------------------------------------------------------- #
# Legacy normalization: extrude_cut + child fillet  ->  one slot_cut
# --------------------------------------------------------------------------- #
def normalize_legacy_slots(resolved: dict, add_flag) -> int:
    """Convert the legacy pattern (an extrude_cut whose description reads
    notch/slot/U-shape, with a child fillet referencing it) into one canonical
    slot_cut. Keeps the extrude_cut FEATURE (so it stays in build_order and gets
    sequenced into Stage 3); drops the loose fillet feature. Returns the count
    converted; every conversion gets a MEDIUM flag via ``add_flag``."""
    feats = resolved.get("features", []) or []
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    existing_slot_ids = {s.get("id") for s in resolved.get("slot_cuts", []) or []}
    converted = 0

    for feat in feats:
        if (feat.get("type") or "").lower() != "extrude_cut":
            continue
        desc = (feat.get("description") or "").lower()
        if not any(k in desc for k in _NOTCH_KEYWORDS):
            continue
        fid = feat.get("id")
        if fid in existing_slot_ids:
            continue
        # Find a child fillet referencing this cut (parent_feature) with a radius.
        child = next((f for f in feats
                      if (f.get("type") or "").lower() == "fillet"
                      and f.get("parent_feature") == fid), None)
        # Pull width/depth from the cut's related dims; radius from the fillet's.
        rel = feat.get("related_dimensions", []) or []
        width = depth = 0.0
        wdim = ddim = ""
        for rid in rel:
            d = dims_by_id.get(rid) or {}
            a = (d.get("applies_to") or "").lower()
            v = float(d.get("value") or 0)
            if a in ("width", "length") and v > 0 and not width:
                width, wdim = v, rid
            elif a in ("depth", "height") and v > 0 and not depth:
                depth, ddim = v, rid
        radius = 0.0
        rdim = ""
        if child:
            for rid in (child.get("related_dimensions", []) or []):
                d = dims_by_id.get(rid) or {}
                if (d.get("applies_to") or "").lower() in ("fillet_radius", "radius") \
                        and float(d.get("value") or 0) > 0:
                    radius, rdim = float(d["value"]), rid
                    break
        if not (width and depth):
            continue  # not enough to form a canonical slot; leave as-is

        slot = {
            "id": fid, "slot_kind": "open_notch",
            "open_edge": "top", "anchor_edge": "left", "anchor_offset": 0.0,
            "anchor_dimension_id": "", "anchor_semantics": "edge_to_near_edge",
            "width": width, "width_dimension_id": wdim,
            "depth": depth, "depth_dimension_id": ddim,
            "corner_radius": radius, "corner_radius_dimension_id": rdim,
            "thru": True, "thru_basis": "single_view_default",
        }
        resolved.setdefault("slot_cuts", []).append(slot)
        converted += 1
        # Drop the loose fillet FEATURE (its role is now the slot's corner
        # fillet, emitted by the slot decomposition) from features + build_order.
        if child:
            cid = child.get("id")
            resolved["features"] = [f for f in resolved.get("features", []) if f.get("id") != cid]
            if cid in (resolved.get("build_order") or []):
                resolved["build_order"] = [x for x in resolved["build_order"] if x != cid]
        add_flag({
            "feature_id": fid, "flag_tier": "MEDIUM", "source": "slot_cut",
            "human_note": (f"Converted legacy extrude_cut+fillet ({fid}) into a canonical "
                           f"slot_cut (rectangle {width:g}x{depth:g}, R{radius:g}) — the slot "
                           "now builds as a mandatory rectangle plus deferred corner fillets."),
        })
    return converted
