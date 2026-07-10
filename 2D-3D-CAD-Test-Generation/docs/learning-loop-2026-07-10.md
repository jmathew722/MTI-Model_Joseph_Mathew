# Learning-loop fix cycle — 2026-07-10

Generalizing fixes for the failures captured in `Learning Loop/*__2026-07-09_*`.
Each item is a *class* fix, not a per-part patch. Deterministic behavior is
covered by `tests/test_learning_fixes_2.py` (40 cases). The live-run acceptance
checks (re-running the named parts through the SolidWorks COM build and the
Claude Vision extraction) must be validated on a Windows machine with SolidWorks
2024 + an API key — they cannot run headlessly in CI.

## Regressions (root cause stated first, as required)

### P1 — universal missing-dimension completeness gate
**Why the prior fix didn't hold.** Fix 2.1 (`_dimensionless_feature_flags`) and
Fix 2.4 (`_incomplete_cut_profile_flags`) only *appended a CRITICAL flag*. The
resolver's stated invariant was *"Every feature gets `build_status == "build"`.
No skip/defer/omit."* — so the offending feature kept `build_status="build"` and
stayed in `build_order`. `macro_generator` (`for fid in model.build_order`),
`solidworks_builder` (same loop), and the CadQuery build plan therefore all still
emitted/attempted it. A001821M's chamfer F005 skipped/failed on **six** runs
across two cycles (fingerprint `6f67a202ef30c362`); A001211E's dimensionless
hole/pattern reached the build and failed there. **The flag never gated.**

**New implementation.** `pipeline/resolver.py::_completeness_gate` runs after TYP
propagation + buildable-base synthesis and, for every buildable feature, checks
the driving dimension(s) for its type (fillet→radius, chamfer→distance,
hole→diameter — incl. the linked hole callout, pattern→spacing+count,
cut→closed profile, extrude_boss→depth [synthesized upstream]). Resolution order:
(1) constraint-graph derivation (`_derive_from_chain`, reuses the closure
machinery), (2) TYP propagation (already run), (3) standard-size substitution
(thread→major diameter, `_thread_major_diameter`). If still missing, the feature
is **removed from `build_order`** — the single choke point all three build
consumers iterate — so it can never be emitted as a build/macro step, and is
surfaced as a Tab-3 model-derived assumption. Base bodies are never excluded (an
excluded base = no model at all). A "circular_pattern"/"linear_pattern" feature
type is now aliased to `pattern` so it no longer fails schema validation and
silently takes the value-only fallback (which skipped the gate).

### P4 — SolidWorks fragile-call diagnostics on every path
**Why the prior fix didn't hold.** The precondition/diagnosis layer (Fix 1.2c:
`_assert_cut_intersects_body`, `_verify_sketch_fully_defined`) existed but was
applied on *some* paths only. The **rectangular** `extrude_cut` path called
`_do_cut` directly, skipping the intersection pre-check, so an off-solid
rectangular cut returned a bare `FeatureCut4 returned None` (A001581E F003). The
fillet/chamfer builders raised bare `returned None` with no precondition context
(A001581E F004, A001591E F003).

**New implementation.** The rectangular cut path now runs the same
`_assert_cut_intersects_body` pre-check. Every `FeatureCut4`/`FeatureFillet3`/
`InsertFeatureChamfer` call is invoked from exactly one wrapper (enforced by
`TestBuilderCallDiagnostics`), and each `returned None` is impossible to reach
without a message enumerating the verified preconditions (radius/distance in
meters, edge count + scope, sketch closed & fully defined, profile overlaps the
body). A 0-edge fillet/chamfer now names the failed precondition and excludes,
rather than applying to nothing.

## Other fixes

- **P2 — gauge-callout thickness (`pipeline/gauge.py`).** `"12 GA. (.105)"` now
  reconciles as `.105` (the parenthetical decimal), never `12`. Gauge-only with a
  known material converts via Manufacturers' Standard (ferrous) / Brown & Sharpe
  (non-ferrous); unknown material is flagged, not guessed. Fixes the four false
  12.0-vs-.105 mismatches. The extractor prompt now records the gauge as metadata
  and never stores the gauge number as the thickness value.
- **P3 — position review.** The parent-center last-resort guess is gone: a
  position-unresolved cut/hole is excluded (Tab-3 assumption), not built at a
  guessed center that can land off the solid (A001211E F004). Bolt-circle / multi-
  instance patterns are recognized as positioned and are NOT routed to review. No
  flag text references the removed Tab-1 markup tool; the inference routes
  attempted before declaring UNRESOLVED are logged.
- **P5 — callout typing (`pipeline/callout_qty.py::classify_callout`).** A
  callout is typed (radius / hole / threaded_hole / compound_hole) before
  counting; only hole-type callouts enter hole-count reconciliation, killing the
  A001591E `.12 R. TYP.`-vs-8-holes false CRITICAL.
- **P6/P10(a) — decimal plausibility (`_decimal_plausibility`).** An ambiguous
  numeral ("312" → .312/3.12/31.2) is resolved by sheet magnitude/formatting +
  standard drill/stock tiebreaks, NOT by picking the smallest ("conservative")
  candidate. A genuine tie stays CRITICAL and the completeness gate excludes the
  dependent feature.
- **P7 — inspection balloons.** Circled-numeral / "N.NN IN." balloon references
  are recognized and downgraded from a HIGH dimension conflict to a LOW
  informational note (A001561E 11.00-vs-10.00), excluded from dimension-conflict
  reconciliation.
- **P8 — thickness-view constraint.** A thickness candidate an order of magnitude
  off the build (A001551E 21.0 vs .50) is rejected as an out-of-band capture
  instead of a false HIGH conflict; the extractor prompt constrains the thickness
  value to the thin section of the edge view.
- **P9 — fillet scope (`plan_fillet_scope`).** Fillet scope is derived from the
  callout (corner-radius TYP → N corners; slot-end → 2 arcs; named host →
  feature; else the *flagged* all-edges fallback). The builder states the intended
  scope and flags when the applied edge count disagrees — all-edges is never the
  silent default (A001551E F004). NOTE: the geometric corner-edge selection in COM
  and CadQuery fillet parity are scaffolded here but must be validated live.
- **P10(b) — STOCK TOL.** A `STOCK`/`(STOCK TOL.)` dimension is treated as the
  finished stock envelope and exempt from tight-tolerance ambiguity routing
  (A001621E 3.50, .50).
