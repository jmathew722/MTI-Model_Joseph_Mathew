# Extraction Is Truth — 2026-07-12

Three defect classes, all evidenced by part **158-C** (11.00 × 6.25 × .105 plate,
U-notch open through the top edge, six .218 thru holes), fixed downstream of a
CORRECT extraction — the extraction read the drawing right; the resolver/build
stages degraded it.

## Defect 1 — open-edge cut built as an enclosed window

The notch's sketch rectangle stopped EXACTLY at the plate's top edge instead of
overshooting it — numerically fragile, and the built model showed a fully
enclosed window (material standing where the drawing is open) instead of a
notch open through the edge. Fix: `slot_cut.EDGE_OVERSHOOT_EPS` (0.050 drawing
units) is added past the open edge only (closed sides stay exact); `open_edges`
recorded on the build-plan step; `cq_prevalidate.py` now cuts the identical
stored polygon (previously it silently skipped slot cuts entirely); a new
`feature_verify.EDGE_NOT_BROKEN` classification cross-sections AT the drawn
edge and is wired into the geometric correction loop.

## Defect 2 — non-deterministic confidence on a directly-read value

F002's dimension (1.56, read straight off the drawing) carried
`assumption_confidence: 0.92` instead of 1.0 — the resolver's own no-ambiguity
branch hardcoded that score regardless of how clean the read was. Fix: a sole,
non-flagged reading now always resolves at `confidence: 1.0,
basis: extracted_verbatim`; sub-1.0 confidence is reserved for dimensions where
≥2 genuine candidates existed and always carries the deciding rule. Extraction
calls are pinned to `temperature=0`. `resolve_extraction` was already a pure
function; `pipeline/resolution_cache.py` makes that provable — same
(extraction, resolver version) key always reproduces a byte-identical result,
and a mismatch is a loud, logged determinism violation, never silent drift.

## Defect 3 — a phantom feature stuck the part at READY_WITH_OPEN_ITEMS

F004, a "pattern" feature (`parent_feature: F003`, `quantity: 6`), duplicated
the SAME 6 holes F003 already builds from its own callout (`.218 DR THRU
6-HLS`). The sheet's hole accounting was already fully satisfied; F004
corresponded to nothing new, yet the completeness gate EXCLUDED it (missing
spacing+count) and it permanently flagged the part `READY_WITH_OPEN_ITEMS`.
Fix: `_reconcile_phantom_duplicate` runs BEFORE any exclusion — a feature whose
parent already accounts for the same callout quantity (or whose own
description reads as a BOM/balloon/applied-item note, a description-text
backstop) is reclassified to the new `PHANTOM_RECLASSIFIED` disposition state:
removed from `build_order`, LOW-tier informational flag naming the duplicate,
never CRITICAL, never gating. `reconciliation.py` represents this explicitly
(`phantom_reclassified[]`, `accounted_total`) rather than counting it as a
checklist miss. Also closed as part of the same sweep: **no feature type may
reach EXCLUDED_INCOMPLETE any more** — a fillet/chamfer with no size anywhere
commits a small shop-typical edge-break, a pattern with no count+spacing
commits a conservative spacing.

## Result on 158-C

`final_status: READY`, zero unresolved items, checklist 5/5 accounted
(4 built + 1 phantom-reclassified), the notch opens through the top edge
(`EDGE_NOT_BROKEN` check green), six holes, every sole-reading dimension
`extracted_verbatim`/1.0. Full suite: 684 passing, all prior invariants
(commit-to-extraction, per-instance hole placement, slot decomposition, golden
macros) unchanged.
