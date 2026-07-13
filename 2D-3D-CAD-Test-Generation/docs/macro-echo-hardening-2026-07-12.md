# Stage 7 Hardening — Macro Echo Check, Templates & Emission Invariants (2026-07-12)

Stage 7 (`build_plan.json` → VBA) is the concentration point for coordinate,
dimensioning, and scoping errors. Evidence from part **158-C** motivated five
hardening measures, each of which turns a build-time mystery on the SolidWorks
machine into an instant, named **generation-time** failure.

## Task 1 — Macro echo check (round-trip verification)

`pipeline/macro_echo.py`. After the macros are generated, every emitted geometry
literal is parsed back out of the VBA and proven equal to the build-plan value
for the SAME feature that emitted it, after unit conversion. Parsing is
**anchored to the known call signatures** the templates emit
(`CreateCircleByRadius`, `CreateCornerRectangle`, slot `CreateLine`, slot-fillet
`Array()` + `rMeters`), never a scan of arbitrary VBA — so structural constants
(`0#`, `2#`, `0.01`) are never mistaken for data. Three failure classes are
caught, per feature:

* **cross_contamination** — a coordinate/dimension literal matching a DIFFERENT
  feature's plan value and not this feature's own (the 158-C "corner-array
  coordinates in the wrong macro" class);
* **orphan_literal** — a geometry literal mapping to no plan value anywhere;
* **missing_value** — a planned position/corner that never became a literal.

Wired into `generate_macro_package` right after the static audit
(`assert_macro_echo`); any discrepancy raises `MacroEchoError` naming the
feature id, field, expected, and found. Tolerances: 1e-3 drawing units, 5e-6 m.
Regression test (`tests/test_macro_echo.py::TestCrossContamination`): a macro
doctored to carry another feature's center fails with a cross-contamination
error naming both features.

## Task 2 — Template-based emission

`pipeline/macro_template_engine.py` + `pipeline/macro_templates/*.vba.tmpl`.
Per-feature geometry construction is filled from a named template
(`%%NAME` placeholders — `%%` because VBA itself uses `$` and `@`) via `fill()`,
which receives **exactly one feature's record**. The fill is strict in both
directions: a missing placeholder OR an unused record key raises
`TemplateFillError`. Because `fill()` sees only one feature's values, it is
structurally impossible for a template to reference another feature's data — the
cross-contamination class dies at the API boundary, and the echo check then
proves the round-trip. The circle and rectangle primitives (used by holes,
bosses, profile cuts, counterbores, and the pattern seed) are routed through
templates; output is byte-faithful, so the golden set is unchanged by the
routing itself. Templates cite the `METHODS.md` recipe they implement and pass
the static macro audit once (`tests/test_macro_echo.py::TestTemplateEngine`).

## Task 3 — Fully-defined sketch gate (not a fixer)

A read-only `ReportSketchStatus` VBA helper (added to `_HELPERS_VBA`) queries the
documented `ISketch::GetConstrainedStatus` after `FullyDefineSketch` and logs
`PASS` (fully defined) or `WARN` (still under-defined → template
under-specification). This makes sketch definition **observable** — a gate, not
silent acceptance — using only a verified read-only API. (Emitting
`AddDimension2`/`SketchAddConstraints` smart-dimension sequences by default was
deliberately NOT done: those call shapes are not yet verified on this machine's
SolidWorks 2024, and the pipeline's discipline is verified-call-shapes-only. The
gate surfaces under-definition today; smart dimensioning can be promoted to the
default path once verified live, exactly as `HoleWizard5` is gated.)

## Task 4 — Emission invariants

* **(a) Falsy basis** (`build_sequencer.py`): the basis check now rejects
  empty/whitespace strings, not just `None`. An assumption whose basis went
  missing reads as `BUILT_WITH_DERIVED_VALUE` ("unspecified_basis" /
  "position:unspecified"), never as directly-extracted. `_EXPLICIT_BASES` and
  `_READ_POSITIONS` no longer contain `""`.
* **(b) Open-edge overshoot** (`slot_cut.EDGE_OVERSHOOT_EPS = 0.050`): an open
  notch's open side crosses the part edge by ε (closed sides stay exact); the
  interior corner fillets are unaffected (always the closed-side pair). The
  emission invariant `_assert_open_edge_overshoot` refuses to generate an
  open-edge cut whose open-axis span equals the depth (a coincident-with-edge
  termination — the 158-C enclosed-window bug).
* **(c) Label/payload agreement** (`_assert_label_payload_agreement`): a step's
  description may name only its own feature id, its parent, or (for a compound
  step like `F002_fillets`) the base feature. A foreign feature id in a
  description refuses generation — the label was assembled from a different
  record than the values it carries.
* **(d) Golden macro snapshots**: `tests/test_golden_macros.py` continues to
  freeze the full generated package; regenerated for the `ReportSketchStatus`
  gate (the only intended output change), reviewed as a diff.

## Task 5 — Iterate to green

Full suite: **678 passing** (663 prior + 15 new in `tests/test_macro_echo.py`),
golden snapshot regenerated and re-verified. The echo check runs on every golden
generation and confirms clean round-trip. CadQuery headless parity: 158-C builds
a valid solid with the overshoot notch (open through the top edge) and both hole
groups. A live SolidWorks COM batch was not run here (no SolidWorks install in
this environment) — the headless CadQuery build is the geometry parity signal;
live confirmation remains the standard final check on a SolidWorks machine.

### Latent defects the new checks would have caught

The echo check + invariants are a standing guard against the 158-C error
classes: a coordinate literal emitted into the wrong feature's macro
(cross-contamination), a sketch that terminates exactly at an open edge (no
overshoot), and an assumption shipped with a blank basis reading as
directly-extracted. On the current golden set these all pass — the value is that
any future regression reintroducing one of them fails loudly at generation time
with the feature named, instead of surfacing as a wrong `.sldprt` on the shop
machine.
