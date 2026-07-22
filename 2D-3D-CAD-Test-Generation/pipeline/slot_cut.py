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
    sits fully interior anchored at (anchor_offset, ...).

    The edge→global math (e.g. a TOP-edge notch's y = parent_height - depth) is
    delegated to :func:`pipeline.coordinate_normalize.resolve_notch_anchor` — the
    ONE place semantic edge anchors become global CAD coordinates, so this and
    the UI/build-plan can never disagree. This function then applies the
    open-side overshoot and the near/interior-corners-first ordering the fillet
    step depends on."""
    from pipeline.coordinate_normalize import (
        anchor_from_open_edge, resolve_notch_anchor,
    )

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

    anchor = anchor_from_open_edge(edge)
    if anchor is None:
        # Closed slot: fully-interior rectangle anchored at (anchor_offset) along
        # the horizontal, depth = slot length vertically. No open edge, no overshoot.
        return [[a, 0.0], [a + w, 0.0], [a + w, d], [a, d]]

    # Resolve the exact (non-overshot) global bounds through the ONE resolver.
    # Envelope falls back to (anchor + depth) when the part extent is unknown, so
    # a top/right notch still lands relative to its own near edge.
    pw = length or (a + d)
    ph = height or (a + d)
    b = resolve_notch_anchor(anchor, offset_x=a, offset_y=a, width=w, depth=d,
                             height=w, parent_width=pw, parent_height=ph)

    # Near/interior corners FIRST (the pair the fillet targets on an open notch),
    # then the two open-side corners pushed past the edge by eps.
    if edge == "top":
        return [[b.x_min, b.y_min], [b.x_max, b.y_min],
                [b.x_max, b.y_max + eps], [b.x_min, b.y_max + eps]]
    if edge == "bottom":
        return [[b.x_min, b.y_max], [b.x_max, b.y_max],
                [b.x_max, b.y_min - eps], [b.x_min, b.y_min - eps]]
    if edge == "left":
        return [[b.x_max, b.y_min], [b.x_max, b.y_max],
                [b.x_min - eps, b.y_max], [b.x_min - eps, b.y_min]]
    # right
    return [[b.x_min, b.y_min], [b.x_min, b.y_max],
            [b.x_max + eps, b.y_max], [b.x_max + eps, b.y_min]]


def interior_corners(slot, corners: list[list[float]]) -> list[list[float]]:
    """The corners that get filleted: 2 for an open notch (the interior pair,
    listed first in corner_array), all 4 for a closed slot / obround."""
    kind = (slot.slot_kind or "open_notch").lower()
    if kind == "open_notch":
        return corners[:2]
    return list(corners)  # closed_slot + obround fillet all 4


def expected_corner_count(slot) -> int:
    return 2 if (slot.slot_kind or "open_notch").lower() == "open_notch" else 4


def rounded_profile_points(slot, corners: list[list[float]],
                           segments: int = 8) -> list[list[float]]:
    """The slot's FINAL closed cross-section as an ordered point loop (drawing
    units) — the rectangle with its interior corners replaced by tangent arcs of
    ``slot.corner_radius``. This is the single geometric proof of the
    rectangle+corner-radius shape: each rounded corner's arc center sits exactly
    ``(r, r)`` inset from the sharp corner along the two incident edges, so the
    arc is tangent to both walls (a filleted corner, by construction). Used by
    the CadQuery pre-validator (and available to the COM path) to cut the exact
    one-shot profile instead of a rectangle-then-3D-fillet, and by tests that
    assert the ``(r, r)`` inset.

    ``corners`` is :func:`corner_array` output (near/interior pair first). For an
    open notch only the 2 interior corners are rounded; a closed slot / obround
    rounds all 4. Winding follows ``corners`` order.
    """
    return rounded_profile_from_corners(
        corners, float(slot.corner_radius or 0.0),
        (slot.slot_kind or "open_notch").lower(), segments=segments)


def rounded_profile_from_corners(corners: list[list[float]], radius: float,
                                 slot_kind: str = "open_notch",
                                 segments: int = 8) -> list[list[float]]:
    """Primitives-only core of :func:`rounded_profile_points` (no schema object),
    so the CadQuery pre-validator and tests can build the exact rounded slot
    profile from a corner array + radius alone. Same math: each rounded corner's
    arc center is ``(r, r)`` inset from the sharp corner along both edges."""
    import math

    r = float(radius or 0.0)
    n = len(corners)
    rounded_idx = set(range(2)) if (slot_kind or "open_notch").lower() == "open_notch" \
        else set(range(n))
    if r <= 0:
        return [list(c) for c in corners]

    pts: list[list[float]] = []
    for i in range(n):
        cx, cy = corners[i]
        if i not in rounded_idx:
            pts.append([cx, cy])
            continue
        prev = corners[(i - 1) % n]
        nxt = corners[(i + 1) % n]
        # Unit vectors from this corner toward its two neighbours.
        vpx, vpy = prev[0] - cx, prev[1] - cy
        vnx, vny = nxt[0] - cx, nxt[1] - cy
        lp = math.hypot(vpx, vpy) or 1.0
        ln = math.hypot(vnx, vny) or 1.0
        upx, upy = vpx / lp, vpy / lp
        unx, uny = vnx / ln, vny / ln
        # Tangent points: r along each incident edge from the sharp corner.
        tp = [cx + upx * r, cy + upy * r]
        tn = [cx + unx * r, cy + uny * r]
        # Arc center: r inset along BOTH edges (the (r, r) inset — tangent to
        # both walls). For an axis-aligned rectangle corner this is exactly
        # (cx ± r, cy ± r).
        acx = cx + (upx + unx) * r
        acy = cy + (upy + uny) * r
        a_start = math.atan2(tp[1] - acy, tp[0] - acx)
        a_end = math.atan2(tn[1] - acy, tn[0] - acx)
        # Sweep the short way from the prev-edge tangent to the next-edge tangent.
        da = a_end - a_start
        while da > math.pi:
            da -= 2 * math.pi
        while da < -math.pi:
            da += 2 * math.pi
        for s in range(segments + 1):
            ang = a_start + da * (s / segments)
            pts.append([acx + r * math.cos(ang), acy + r * math.sin(ang)])
    return pts


def arc_centers(slot, corners: list[list[float]]) -> list[list[float]]:
    """The corner-arc CENTERS (drawing units) — each exactly ``(r, r)`` inset
    from its sharp corner along both incident edges. Returned for the interior
    (filleted) corners only. Empty when there is no radius. This is the explicit
    'circle of that radius on the edge of the rectangle' the corner treatment
    resolves to, exposed for verification/testing."""
    r = float(slot.corner_radius or 0.0)
    if r <= 0:
        return []
    filleted = interior_corners(slot, corners)
    n = len(corners)
    idx_of = {tuple(c): i for i, c in enumerate(corners)}
    centers: list[list[float]] = []
    for c in filleted:
        i = idx_of[tuple(c)]
        cx, cy = corners[i]
        prev = corners[(i - 1) % n]
        nxt = corners[(i + 1) % n]
        import math
        vpx, vpy = prev[0] - cx, prev[1] - cy
        vnx, vny = nxt[0] - cx, nxt[1] - cy
        lp = math.hypot(vpx, vpy) or 1.0
        ln = math.hypot(vnx, vny) or 1.0
        centers.append([cx + (vpx / lp + vnx / ln) * r,
                        cy + (vpy / lp + vny / ln) * r])
    return centers


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

        # Derive the open edge + anchor from the cut's own positional evidence
        # rather than hardcoding top/left/0 (which silently misplaced every
        # converted legacy slot). offset_x/offset_y on the feature, or a
        # positional related dimension, name where the slot actually sits.
        open_edge, anchor_edge, anchor_offset, semantics = _infer_legacy_anchor(
            feat, dims_by_id)
        slot = {
            "id": fid, "slot_kind": "open_notch",
            "open_edge": open_edge, "anchor_edge": anchor_edge,
            "anchor_offset": anchor_offset,
            "anchor_dimension_id": "", "anchor_semantics": semantics,
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
                           f"slot_cut (rectangle {width:g}x{depth:g}, R{radius:g}, open "
                           f"{open_edge} edge at {anchor_offset:g} from {anchor_edge}) — the "
                           "slot now builds as a mandatory rectangle plus deferred corner "
                           "fillets."),
        })
    return converted


def _infer_legacy_anchor(feat: dict, dims_by_id: dict) -> tuple[str, str, float, str]:
    """Best-effort (open_edge, anchor_edge, anchor_offset, semantics) for a
    legacy extrude_cut being promoted to a slot, from the feature's own
    positional evidence. Falls back to top/left/0 ONLY when nothing positional
    exists — but never silently: the caller flags the conversion regardless.

    Convention (lower-left origin): a nonzero offset_y with offset_x≈0 reads as a
    notch broken through the LEFT edge sitting offset_y up; a nonzero offset_x
    with offset_y≈0 reads as a notch through the BOTTOM edge at offset_x across.
    A positional related dimension supplies the offset when the feature stored
    none."""
    ox = float(feat.get("offset_x") or 0.0)
    oy = float(feat.get("offset_y") or 0.0)
    if not ox and not oy:
        for rid in feat.get("related_dimensions", []) or []:
            d = dims_by_id.get(rid) or {}
            a = (d.get("applies_to") or "").lower()
            v = float(d.get("value") or 0)
            if v <= 0:
                continue
            if "x" in a or "horizontal" in a or a in ("position", "offset"):
                ox = v
            elif "y" in a or "vertical" in a:
                oy = v
    if oy and not ox:
        return "left", "left", oy, "edge_to_near_edge"
    if ox and not oy:
        return "bottom", "bottom", ox, "edge_to_near_edge"
    if ox and oy:
        # Both offsets: treat as a bottom-edge notch positioned across by ox.
        return "bottom", "left", ox, "edge_to_near_edge"
    return "top", "left", 0.0, "edge_to_near_edge"


def expand_slot_patterns(resolved: dict, add_flag) -> int:
    """Expand a pattern whose seed is a slot into explicit per-instance
    ``slot_cut`` records (2026-07-21). A U-notch decomposition (rectangle +
    corner fillets) cannot be reliably feature-patterned in SolidWorks
    ("Parameter not optional" on the COM path; a manual step on the VBA path),
    and a patterned fillet is exactly the collapsed/failed-instance class. So a
    ``pattern`` feature that references a slot as its seed is realized as N-1
    ADDITIONAL independent slots at the patterned offsets — each an unfailable
    rectangle+fillet — and the pattern feature is dropped. This is the same
    'individual instances beat a fragile pattern' bias the hole path already
    uses (A001271E).

    Handles the common case where the pattern shifts along the slot's own
    along-edge axis (left/right slots ↔ Y pattern, top/bottom slots ↔ X). A
    pattern along the depth axis is left for manual handling and flagged.
    Returns the number of extra slots created."""
    feats = resolved.get("features", []) or []
    slots = {s.get("id"): s for s in resolved.get("slot_cuts", []) or []}
    if not slots:
        return 0
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    created = 0

    for feat in list(feats):
        if (feat.get("type") or "").lower() != "pattern":
            continue
        seed_id = feat.get("parent_feature") or ""
        seed = slots.get(seed_id)
        if seed is None:
            continue
        qty = int(feat.get("quantity") or 0)
        if qty < 2:
            continue
        spacing, axis = _pattern_spacing_axis(feat, dims_by_id)
        if not spacing:
            add_flag({"feature_id": feat.get("id"), "flag_tier": "HIGH", "source": "slot_cut",
                      "human_note": f"Pattern {feat.get('id')} seeds slot {seed_id} but no "
                      "spacing could be grounded — the extra slot(s) were not generated; "
                      "verify the pattern spacing on the drawing."})
            continue
        along = "y" if (seed.get("open_edge") or "").lower() in ("left", "right") else "x"
        if axis and axis != along:
            add_flag({"feature_id": feat.get("id"), "flag_tier": "MEDIUM", "source": "slot_cut",
                      "human_note": f"Pattern {feat.get('id')} on slot {seed_id} runs along "
                      f"{axis} but the slot's along-edge axis is {along}; left for manual "
                      "handling rather than guessing the transform."})
            continue

        base = float(seed.get("anchor_offset") or 0.0)
        new_ids: list[str] = []
        for i in range(1, qty):
            new_slot = dict(seed)
            new_id = f"{seed_id}_P{i}"
            new_slot["id"] = new_id
            new_slot["anchor_offset"] = base + i * spacing
            resolved.setdefault("slot_cuts", []).append(new_slot)
            # Backing extrude_cut feature so the sequencer + macro/COM builders
            # emit its slot decomposition exactly like the seed's.
            # Carry the seed slot's own dimension ids so the validator (which
            # doesn't special-case slot-backed cuts) sees a dimensioned cut, not
            # a bare 0-dimension feature. The geometry still comes from the slot.
            seed_dims = [d for d in (seed.get("width_dimension_id"),
                                     seed.get("depth_dimension_id"),
                                     seed.get("corner_radius_dimension_id")) if d]
            resolved.setdefault("features", []).append({
                "id": new_id, "type": "extrude_cut",
                "description": f"Patterned notch (instance {i + 1} of {qty}) from slot "
                               f"{seed_id}, offset {i * spacing:g} along {along}.",
                "related_dimensions": seed_dims,
                "sketch_plane": (feat.get("sketch_plane") or "front"),
                "parent_feature": "", "offset_x": 0.0, "offset_y": 0.0,
                "position_known": True, "anchors": [], "quantity": 1,
            })
            bo = resolved.setdefault("build_order", [])
            if new_id not in bo:
                bo.append(new_id)
            new_ids.append(new_id)
            created += 1

        # Drop the pattern feature (its instances are now explicit slots).
        pid = feat.get("id")
        resolved["features"] = [f for f in resolved.get("features", []) if f.get("id") != pid]
        if pid in (resolved.get("build_order") or []):
            resolved["build_order"] = [x for x in resolved["build_order"] if x != pid]
        # Record the absorption so reconciliation (which checks the RAW
        # extraction) treats this pattern id as justified, not missing — its
        # instances ARE built, under the new slot ids.
        resolved.setdefault("resolved_away", {})[pid] = (
            f"pattern absorbed into explicit slot(s) {', '.join(new_ids)} "
            f"(a U-notch cannot be reliably feature-patterned)")
        add_flag({"feature_id": pid, "flag_tier": "MEDIUM", "source": "slot_cut",
                  "human_note": (f"Pattern {pid} (seed slot {seed_id}, qty {qty}) expanded into "
                                 f"{len(new_ids)} explicit slot(s) {', '.join(new_ids)} at "
                                 f"{spacing:g} spacing — each builds as its own unfailable "
                                 "rectangle+corner-fillet rather than a fragile feature pattern.")})
    return created


def _pattern_spacing_axis(feat: dict, dims_by_id: dict) -> tuple[float, str]:
    """(spacing, axis) for a slot pattern, from the feature's chain anchor
    (preferred — carries axis + value), else a spacing-like related dimension.
    axis is 'x'/'y'/'' ; spacing 0.0 when none is grounded."""
    for a in feat.get("anchors", []) or []:
        ax = (a.get("axis") or "").lower()
        v = float(a.get("value") or 0)
        if ax in ("x", "y") and v > 0:
            return v, ax
    for rid in feat.get("related_dimensions", []) or []:
        d = dims_by_id.get(rid) or {}
        a = (d.get("applies_to") or "").lower()
        v = float(d.get("value") or 0)
        if v > 0 and ("spacing" in a or "pitch" in a or "center" in a):
            return v, ""
    return 0.0, ""
