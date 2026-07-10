# Closed-loop geometric accuracy layer — 2026-07-10

Adds the "last mile" of the accuracy guarantee: the pipeline already verified
that every *extracted* feature has a status (Stage 10.5 reconciliation); it now
verifies that every *built* feature has the right geometry, and iterates to fix
mismatches.

## Phase A — per-feature geometric verification (`pipeline/feature_verify.py`)

Measures **every** planned feature against the built STL + `build_plan.json`,
not just the operator MM constraints (`constraint_verify.py` still does those):

- Holes — position, diameter, through/blind (cross-section circle fit).
- Profile cuts / notches / edge cutouts — material-absence probe at the expected
  extent (point-in-polygon against the mid-thickness section; no ray/containment
  backend needed, so it runs in the pinned env).
- Slots — obround footprint recognised (not a phantom hole).
- Base — envelope L×W×thickness; plus a COM-vs-CadQuery volume cross-check.

Output `<Part>_feature_verification.json`, one verdict per feature:
`OK / MISSING / MISPLACED / WRONG_SIZE / EXTRA / UNMEASURABLE` — the last always
with a stated reason (never a silent skip). The STL is already in the
lower-left-origin drawing frame (empirically confirmed on a real build:
11.0×5.25 plate, holes at their drawn coords to ~0.004").

Verified against the real live-built **A001341E (157-C)** `.STL`: base + all four
Ø0.218 THRU holes + the Ø3.062 edge cutout → all `OK`.

## Phase B — geometric correction loop (`reconciliation.geometric_correction_loop`)

Bounded build→measure→correct→rebuild (cap 3), same discipline as the checklist
reconciler. `classify_transform` detects a **systematic** error (origin offset /
axis swap / uniform scale) only when ≥2 features share one consistent delta, and
pre-compensates it once across all steps; a one-off `MISPLACED` re-emits with the
resolver-derived position (drawing is truth, never the measured value);
`MISSING`/`EXTRA`/unresolvable `WRONG_SIZE` are flagged, never fabricated.
Terminates on all-PASS, the cap, no-applicable-correction, or **oscillation**
(a previously-PASS feature regressing → stop immediately). Writes
`<Part>_geometric_loop_report.json`; each iteration appends to
`lessons_learned.jsonl`. The COM build is injected, so the whole loop is
unit-tested headlessly.

## Phase C — proven construction methods

- Holes: `sketch_circle_cut` stays the verified default; `HoleWizard5` remains
  opt-in (`MTI_ENABLE_HOLE_WIZARD=1`) — it returned `None` on SolidWorks 2024.
- Slots: CadQuery `slot2D` obround mirror added to `cq_prevalidate` (keyed on a
  `profile: "slot"` step); SolidWorks `CreateSketchSlot` documented as the paired
  method. Verified headless.
- Cuts: confirmed already origin-anchored (each cut sketch dimensioned to the
  part origin, never chained) so an upstream correction never cascades.

## Phase D — method library

`pipeline/METHODS.md` (evidence-backed, human) + `methods_config.py` (machine
dispatch: defaults ← `methods.json` ← `MTI_METHOD_<CLASS>` env) +
`construction_experiment.py` (build a scratch base+feature with each candidate
method, Phase-A-verify, record the winner). `macro_generator` stamps each step's
`construction_method`. Seeded this session: hole→`sketch_circle_cut`,
slot→`slot2d` (both verified via the experiment harness).

## Protected invariants (unchanged / pinned)

Single-point unit conversion (`assert_meters`); candidates-only resolution;
unconditional per-feature dispositions; complete-approximate-model-never-
incomplete; READY/NOT-READY contract + webapp banner regex (new nuance lives in
report JSONs, no new top-level status enum); no paid re-extraction in any loop.

Tests: `tests/test_feature_verify.py` (9), `tests/test_geometric_loop.py` (13),
`tests/test_methods_config.py` (10). Full suite: 478 passing, zero regressions.
