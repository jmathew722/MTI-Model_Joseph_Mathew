# Fillets always land on the inside edges of extrude cuts (2026-07-22)

## The problem

A fillet on an extrude cut (a slot/notch/pocket corner) must round the cut's
**interior corner edges** — the concave, through-thickness edges where two cut
walls meet. The old selection did not reliably do this:

- **Slot corner fillet (COM)** used `_select_vertical_edges_near`: for each
  computed corner (x, y) it picked the nearest *vertical* body edge within 2 mm.
  On 16247 it selected **0 edges** and deferred (sharp corners), because
  coordinate-matching against the live body (after the open-side overshoot and
  the body-min alignment) is fragile.
- **General fillet with a host feature (COM)** used `_select_feature_edges`,
  which selects **every edge of every face the feature created** — the walls'
  vertical corners *and* the top/bottom rim edges. That over-fillets (rounds the
  mouth/rim, not just the interior corners).
- **`FILLET_EDGE_MODE`/all** rounds every body edge — the flagged last resort.

None of these expresses "the inside edges of the cut."

## How fillet edges are selected (today, after this change)

`build_fillet` → `_select_fillet_edges` chooses edges by scope, then
`_feature_fillet3` applies the single verified `FeatureFillet3` call to whatever
is selected. `build_slot` selects the slot's corner edges directly. The change
adds one robust selector both use.

## The robust rule — identity-free interior-edge detection

Given the **cut feature object** (`FeatureCut4` result), select the edges that
are the cut's interior corners:

1. `faces = cut_feat.GetFaces()` — the faces the cut *created* (its walls, plus a
   bottom face if blind). The part's pre-existing outer walls and the top face
   are **not** in this set.
2. Fingerprint every edge of those faces by its two endpoints (rounded
   `(start_xyz, end_xyz)`, order-independent) — endpoint geometry is reliable
   across COM wrappers; face identity and `IFace2.GetBox` are **not** (the API
   docs say `GetBox` is approximate, not for comparison).
3. **Count how many of the cut's own faces each edge appears in.** An edge shared
   by *two* cut walls (a concave interior corner) appears **twice**; a mouth edge
   (cut wall ↔ the part's outer wall) or a rim edge (cut wall ↔ the top face)
   appears **once**, because the other face is not one of the cut's created
   faces.
4. Keep the edges that appear **≥ 2 times AND are vertical** (through-thickness:
   the Z span dominates). Those are exactly the interior vertical corner edges —
   all 4 for a closed pocket, the 2 closed-end corners for an open notch (the
   mouth is correctly excluded).

This needs no concavity computation, no coordinate matching, and no fragile face
identity — only edge-endpoint fingerprints over the cut's own topology. It is
the direct implementation of "fillets are always on the inside edges of the
cut."

## Where it is applied

- `pipeline/solidworks_builder.py`
  - `_cut_interior_vertical_edges(sw_doc, cut_feat)` — returns the interior edge
    objects (the rule above); `_select_cut_interior_edges` selects them and
    returns the count.
  - `build_slot` — uses it on the just-built rectangle cut instead of
    coordinate-proximity; falls back to the old proximity selector only if the
    topological selector finds nothing.
  - `build_fillet` — when the fillet's `parent_feature` is a built
    **extrude_cut**, scopes to that cut's interior edges (before the generic
    feature/all fallback).
- `pipeline/macro_generator.py`
  - `_macro_slot_fillet` — the VBA mirror: iterate `swSlot.GetFaces`, fingerprint
    edges by endpoints, count occurrences across the cut's faces, fillet the
    vertical edges seen ≥ 2 times. Falls back to the prior vertex-proximity scan
    if the topological pass finds none. Deferred-safe as before (a wrong count
    never destroys the already-correct rectangle).

## Guarantees preserved

- **Deferred-safe:** the slot rectangle is always built first; a fillet that
  finds no interior edge defers with a warning, never fails the build.
- **Golden macros byte-identical:** the general-fillet VBA path only changes
  behavior when the host is an extrude_cut; the golden bracket's fillet host is
  not a cut, so its emitted macro is unchanged. The slot-fillet macro
  (`_macro_slot_fillet`) is not exercised by the golden part.
- **One `FeatureFillet3` call site** (the P4 hygiene invariant) is unchanged.

## Verification

- Unit tests for `_cut_interior_vertical_edges` against a synthetic face/edge
  topology (a stubbed cut: 4 walls sharing 4 vertical corner edges + rim edges)
  — asserts exactly the 4 (closed) / 2 (open) interior verticals are chosen and
  the mouth/rim edges are rejected.
- Live check on **16247** (two open U-notches): the corner fillets now apply on
  the COM build instead of deferring at 0 edges.
