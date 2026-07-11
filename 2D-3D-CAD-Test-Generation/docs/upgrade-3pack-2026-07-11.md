# Upgrade 3-Pack — 2026-07-11

Three upgrades implemented in dependency order (3 → 1 → 2 build order; 2 improves
input to both). Each is committed separately.

## Workstream 3 — reference-geometry datum skeleton (`reference_geometry.py`)

Builds the drawing's datum structure FIRST as named SolidWorks reference
geometry (`01a_reference_geometry.vba`, before any feature), then records each
feature's datum handle — how a human engineer models. `REF_DATUM_A` always,
plus `REF_DATUM_B/C` (GD&T/dimension `datum_ref`), `REF_SYM_X/Y` (symmetry),
`REF_AXIS_*` (concentric/circular), `REF_PT_<fid>` (pattern origins). `build_plan.json`
gains `reference_geometry[]` + per-step `positioned_from`. **Additive** — the
proven absolute-coordinate build stays as the audit trail + fallback; the
skeleton gives human landmarks and gives Workstream 1 stable named selection
handles. macro_audit is a blacklist so `InsertRefPlane`/`InsertAxis2`/
`SketchUseEdge3` pass by default. codestack cloned to `third_party/` (gitignored,
mined for idioms not vendored); SW 2024 signatures in `docs/sw_api_reference/`.
Tests: `tests/test_reference_geometry.py` (12). Golden macros regenerated (01a).

## Workstream 1 — deferred feature retry (`deferred_retry.py`)

One bad feature no longer stalls the part. A hard non-strict COM failure
quarantines the feature and the build CONTINUES; after the rest of the solid is
complete, deferred features are retried (cap 3) with the completed-solid
topology as context (target faces/parents now exist), using an escalating
taxonomy playbook (selection / sketch-over-under-defined / zero-thickness-
geometry / missing-parent / com-timeout / param-out-of-range — each attempt
changes strategy, never repeats, last resort = clarification gate). Recovered →
`built`; still-open → `deferred_open` + a ready-to-answer clarification question
in the assist queue. Ledger: `_deferred_log.json`. Orchestration is injectable →
unit-tested without SolidWorks. Live-validated: a clean part builds unchanged
(no regression), no spurious deferred log. Tests: `tests/test_deferred_retry.py`
(10).

## Workstream 2 — tiled high-res extraction (`utils/tiled_extraction.py`)

An ESCALATION for large-format sheets whose thin line work dilutes to sub-pixel
at the raster cap ("image appears nearly blank"). `should_tile()` fires on the
blank heuristic / ink density < 0.5% / confidence < 0.6 / > 25% unclear dims /
C-size+ page at the cap. Then `adaptive_render()` re-renders the vector PDF at
300→600→900 DPI until median line width ≥ 2.5px (lossless zoom), a cheap global
pass maps views/datums, `make_tiles()` cuts ~1500px tiles at 22% overlap, each
content tile is extracted in SHEET coordinates (blank margins skipped),
`stitch()` merges by anchor+value (conflicts → candidate `possible_values` for
Stage 2.5), `datum_anchor()` re-expresses positions from the datum. Tile cost
logs as `extraction_tiled`; tiles cache by (page hash, DPI, grid). VLM calls are
injected → the machinery is unit-tested with a real golden-PDF rasterization and
NO paid API call. Clean small drawings keep the single-shot path. Tests:
`tests/test_tiled_extraction.py` (19).

## Invariants preserved (all three)

Never block (the rest of the part always completes; a pending question/deferred
feature ships its best value); never fabricate (candidates only); every feature
ends built / built_with_flag / skipped_prohibited / deferred_open (+question);
READY contract + webapp regex unchanged (new nuance in report JSONs + additive
overlays, no new top-level status enum); no paid re-extraction in any loop;
single-point unit conversion. Full suite: 536 passing, zero regressions.
