# AUDIT — Dimensioning & Coordinate Handling in Macro Generation (2026-07-17)

Phase 2 of the dimensioning-architecture overhaul (research:
`docs/research/DIMENSIONING_ARCHITECTURE_NOTES.md`). Files read:
`pipeline/resolver.py`, `pipeline/macro_generator.py`,
`pipeline/solidworks_builder.py`, `pipeline/build_sequencer.py`,
`pipeline/coordinate_normalize.py`, `pipeline/slot_cut.py`,
`pipeline/macro_audit.py`, `pipeline/macro_echo.py`, `pipeline/schema.py`,
plus real outputs (158-C, 127-C, M_121-B fixture sets under
`tests/fixtures/summary/`, the golden bracket macros, and the 164-C flywheel
run of 2026-07-17).

## Severity-ranked findings

| # | Sev | Finding | Where |
|---|-----|---------|-------|
| A1 | CRITICAL | **Anchoring dies in the resolver.** `_feature_positional_xy` (resolver.py:1378) correctly gathers positional evidence *with its dimension ids* — then `resolve` writes only bare floats: `feat["offset_x"], feat["offset_y"] = round(px,6), round(py,6)` (resolver.py:786). The dim ids survive only inside a human-readable note. From this point on, nothing downstream knows a position came from "D002 = 1.56 from the left edge" vs "half the envelope" vs "committed conservative". | resolver.py:783–786, 832, 1000; schema.py:456 (`Feature.offset_x` carries no provenance) |
| A2 | CRITICAL | **The 164-C class: polar intent flattened to identical XY.** A bolt-circle callout carries radius+angle intent, but `_hole_positions` (macro_generator.py:328) emits absolute centers; when the center estimate is shared and per-hole angles are unknown, distinct groups collapse onto one point. The overlapping-holes invariant (macro_generator.py:2401) correctly *refused* the 164-C build — the refusal is the symptom; the missing polar anchor is the cause. | macro_generator.py:298 (`_circular_positions`), 328, 1561 (`_pattern_center`); run log `webapp/parts/54f2d190644b/.../ui_console.log` |
| A3 | HIGH | **Anchor choice is implicit, decided at generation time.** `_macro_extrude` (macro_generator.py:1148): `position_known` → offsets; else circle → envelope center (`length/2, width/2`, :1154–1157); else rectangle → `(0, 0)` corner (:1158–1160). Three different anchor semantics (explicit position / center-datum assumption / corner-baseline assumption) produce indistinguishable floats; only a free-text `position_note` records which ran. | macro_generator.py:1148–1183, `_profile_vba`:1038 (frame convention lives in a comment) |
| A4 | HIGH | **Silent frame re-interpretation.** `_corner_frame_shift` (macro_generator.py:276) re-origins positions by half the envelope when any coordinate is negative — a center-vs-corner FRAME decision made per-generation, recorded nowhere, and applied only on the VBA path. The COM path has its own, different recentering (solidworks_builder.py:1227–1241 re-centers a whole layout on the actual body when `position_known` is false). Two code paths can disagree about the frame of the same part. | macro_generator.py:276–295; solidworks_builder.py:1227–1241 |
| A5 | HIGH | **Positions are computed in two places (double-computation drift).** The VBA path bakes literals from `_hole_positions`/`_macro_extrude` at generation time; the COM builder **re-derives** the same positions at build time from the model (`build_hole` → `_hole_positions` again + its own recentering, solidworks_builder.py:1162–1241; `build_extrude_cut`:856–886 recomputes cx/cy from dims). The echo check (macro_echo.py) pins VBA↔plan, but nothing pins COM↔plan; the recentering divergence in A4 is live drift. | solidworks_builder.py:512, 856, 1162–1241; macro_generator.py:328, 1148 |
| A6 | MEDIUM | **Anchoring metadata already exists — as dead display data.** `_classify_hole_groups` (resolver.py:1573) builds a real per-instance datum chain (`position_basis: [{anchor: left_edge\|top_edge\|hole_center\|origin, dim, value, axis}]`, resolver.py:1638) and `BuildStep.position_basis` carries it into the plan — but **no position computation consumes it**. It feeds the UI table and nothing else. The new anchor schema is this structure, promoted to the single input of a solver instead of a side-channel. | resolver.py:1573–1655; macro_generator.py:144 (BuildStep field); summary_view.py (display only) |
| A7 | MEDIUM | **Notch/slot anchoring is the one solved case — but a parallel system.** `coordinate_normalize.Anchor` (coordinate_normalize.py:48) + `resolve_notch_anchor` (:125) is exactly the right idea (semantic anchor → global frame in ONE place, with the `y = parent_height − depth` math and a generation-time orientation guard). It covers edge notches and point anchors only, is not tied to dimension ids, and other feature types bypass it entirely. The new architecture generalizes it rather than replacing it. | coordinate_normalize.py:48–178; slot_cut.corner_array delegates correctly |
| A8 | MEDIUM | **Correction propagation is wholesale, not tracked.** A corrected dimension → full re-resolution → all offsets recomputed → macros regenerated from scratch. Correct features are rewritten identically (fine), but nothing reports WHICH features moved because of the change, and the reconciliation checklist (reconciliation.py) verifies feature *presence/instance counts*, `feature_verify` checks *absolute* position — neither can catch "right hole, measured from the wrong edge" (a compensating-error class: absolute XY can be right today and wrong after the next correction). | reconciliation.py (checklist), feature_verify.py (absolute compare) |
| A9 | LOW | **Chain math exists on the resolve side only.** The resolver's arithmetic-chain machinery discovers `D007 = D008 + D009` closures for *value* resolution, but the build side never consumes chains for *position* accumulation — chain-dimensioned features are positioned by whatever single dim happened to be `applies_to`-positional. | resolver.py (chain resolution), macro_generator.py:328 (linear spacing accumulates from a start estimate, not from the chain structure) |

## Per-feature-type position math (current state)

| Feature type | Sketch-coordinate math today | Baked-in assumptions |
|---|---|---|
| hole (individual) | `_hole_positions`: callout `instance_positions` → else per-feature `offset_x/y` → else linear spacing from envelope-centered start (`_effective_spacing` + centering), then `_corner_frame_shift` | lower-left origin, +Y up; negative coord ⇒ center-referenced; spacing rows horizontal |
| hole (bolt circle) | `_circular_positions`: center = envelope/2 or bore center probe; radius = BSC/2; angles = equal spacing from `start_angle` | center at envelope midpoint when no bore; seed angle 0° default |
| extrude boss/cut | `_macro_extrude`: `position_known` → offsets; circle → envelope center; rect → (0,0) corner | corner-at-origin frame equals edge-referenced dims; near-edge semantics |
| slot / U-notch | `slot_cut.corner_array` → `resolve_notch_anchor` (the ONE correct anchor path) + overshoot | anchor_semantics edge_to_near_edge vs edge_to_centerline handled explicitly ✅ |
| pattern (linear covered) | parent hole cut already placed instances; pattern step is a no-op reference | inherits every hole assumption |
| fillet/chamfer | interactive edge selection; no position math | n/a |
| COM rebuild (all types) | independent re-derivation + body recentering | may disagree with VBA literals (A4/A5) |

## What must change (consumed by Phase 3)

1. **One solver.** Absolute coordinates become DERIVED output of
   `pipeline/position_solver.py`, computed from explicit `PositionAnchor`
   records; legacy bare positions are wrapped as the degenerate
   `coordinate` scheme so current behavior (and the goldens) are the base
   case, not a casualty.
2. **Anchors in the plan.** Every positioned build step carries its anchors +
   a human-readable derivation trace; the coordinate frame chosen for the part
   (corner / ordinate zero edges / datum-hole pair / center) is recorded once
   in the build-plan header.
3. **Anchor-aware verification.** Feature verification gains an
   anchor-relative re-measurement so wrong-edge errors are caught even when
   absolute XY happens to pass.
4. **Datum detection.** Extraction asks for the dimension origin and datum
   holes; Stage 2.5 selects the canonical frame from them and records the
   choice.
