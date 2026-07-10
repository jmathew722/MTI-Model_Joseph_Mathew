# Build-order & feature-build redesign — 2026-07-10

Replaces the implicit build-order pass (extractor order + fillet/chamfer
deferral scattered in `macro_generator`) with one explicit, deterministic,
completeness-based sequencer: `pipeline/build_sequencer.py`.

## Motivating failures

- **Blanket hole/pattern omission by category** — the old mental model of
  "omit what we're not confident about" risked dropping whole feature classes.
  A hole with a resolved diameter and an X/Y position is fully autobuildable and
  must be built. Omission is now decided per feature by the *completeness gate*
  (missing driving dimension → excluded with the parameter named), never by type.
- **Fillet-before-cut ordering** — edge treatments could interleave with cuts,
  producing unpredictable geometry / broken edge references. Chamfers then
  fillets are now always the last geometric stage.
- **`FeatureCut4` sketch-circle hole failures** — an open/empty sketch or a cut
  aimed at the wrong side returned a confusing `Nothing`. Holes now attempt a
  real `HoleWizard5` feature (carrying thread/cbore/csk callout data) first, with
  the exact prior sketch-circle cut kept as a guaranteed fallback.

## The seven-stage sequence (`build_sequencer.py`)

    0 reference geometry   (origin/planes/datum axes — no solid)
    1 base solid           (largest closed outer profile; exactly one)
    2 additive features    (secondary bosses / coaxial bodies; largest-first)
    3 profile subtractions (notches/steps/slots — change outer topology)
    4 holes                (plain thru → counterbore/countersink → tapped)
    5 patterns             (reference a Stage-4 seed; after the seed by stage)
    6 edge treatments      (chamfers, then fillets — always last)
    7 non-geometric        (cosmetic threads, finish notes)

Within every stage a stable sort on explicit keys (ending in the feature id)
guarantees a **byte-identical `build_order` across runs** on the same extraction.
Base vs. additive is decided by area (largest base wins), not by extractor order.

## Three-state disposition table

Every extracted feature ends in exactly one state, recorded in
`<Part>_build_dispositions.json` (and in `build_plan.json` under `dispositions`):

- `BUILT` — built from read values.
- `BUILT_WITH_DERIVED_VALUE` — built using a constraint-graph / TYP / standard-
  size value (the resolver flagged it inferred).
- `EXCLUDED_INCOMPLETE` — excluded by `resolver._completeness_gate` because a
  driving dimension could not be resolved; the specific missing parameter is
  named in the flag.

Human-readable lines are still emitted (`SequenceResult.human_lines`) for
backward compatibility with the learning-loop logs.

## Backends

- **CadQuery** (`cq_prevalidate.py`): the origin-frame → workplane-local
  transform now lives in ONE unit-tested place, `to_workplane_local(x, y, k)`.
  The build plan's `positions_xy` are already rebaselined to the bottom-left
  origin and the base is extruded from global XY, so the transform is a pure
  drawing-unit → mm scale (documented invariant). CadQuery pre-validation still
  builds from the same `build_plan.json` (single source of truth) and now follows
  the staged seq order.
- **SolidWorks** (`solidworks_builder.py`): `build_hole` attempts
  `IFeatureManager::HoleWizard5` (`_try_hole_wizard`) — generic-hole-type derived
  from the callout (drill / counterbore / countersink / tap), placement points at
  the resolved centers — then falls back to the exact `_circular_cut_at`
  sketch-cut on ANY failure, so the working build path never regresses. Set
  `MTI_DISABLE_HOLE_WIZARD=1` to force the legacy path. The full HoleWizard5
  parameter tuple and fastener/size strings are SolidWorks-version/locale
  specific and must be validated on a live SolidWorks machine (cannot be
  exercised headlessly in CI).

## Integration point

`generate_macro_package` calls `sequence_build_order(model, resolution)` once,
sets `model.build_order`, writes the disposition JSON, and stashes the table on
the package. Because the same `model` object drives macros, `build_plan.json`,
CadQuery pre-validation, and the COM build, all four inherit the single staged
order.

## Tests

`tests/test_build_sequencer.py` covers stage classification, the largest-base
rule, full stage ordering, pattern-after-seed, edges-after-all-cuts,
chamfer-before-fillet, plain-before-tapped, byte-identical determinism, the
three-state disposition table, the no-base hard-failure, and the
origin→workplane-local transform. Golden macros regenerated (pure seq-number
reorder; hole/base macro bodies byte-identical). Full suite: 430 passing.

## Not weakened

The completeness gate and the cut/body intersection sanity check are unchanged —
the redesign reorders and reflects state; it does not raise build counts by
loosening either guard.
