# MTI 2D→3D Pipeline — What Changed Since PIPELINE_SECTION2SECTION.md

This is a **delta** doc. It records everything that changed in the pipeline
since `PIPELINE_SECTION2SECTION.md` was written (commit `b217f24`). Read the
original for the full stage-by-stage walkthrough; read this for what's new.

Two commits landed after the original:

- `175883f` — Tab-3 Visual Summary (a presentation layer over existing artifacts)
- `3052706` — Stage-7 hardening (macro echo check, templates, emission invariants)

Nothing in the stage *order* changed. The changes are: new guards inside
Stage 7, one new READ-only view-model surface, and a couple of cross-cutting
correctness sweeps.

---

## Stage 6.5 — Build sequencer: falsy-basis sweep

**Module:** `pipeline/build_sequencer.py`

`_EXPLICIT_BASES` and `_READ_POSITIONS` no longer contain the empty string `""`.
Previously an ASSUMED value whose `assumption_basis` came back blank/whitespace
would read as directly-extracted (`BUILT`). Now `_derivation_source` classifies
a blank-basis assumption as `"unspecified_basis"` (and a blank position
assumption as `"position:unspecified"`) → `BUILT_WITH_DERIVED_VALUE`. An
assumption with a missing basis can no longer masquerade as a clean read. This
was the 158-C `derivation_source: ""` evidence.

---

## Stage 7 — Macro generation: five new generation-time guards

**Modules:** `pipeline/macro_generator.py`, **new** `pipeline/macro_echo.py`,
**new** `pipeline/macro_template_engine.py`, **new**
`pipeline/macro_templates/*.vba.tmpl`, `pipeline/slot_cut.py`.

Stage 7 (the `build_plan.json` → VBA translation) gained a hardening layer that
turns coordinate/dimensioning/scoping bugs into **named generation failures**
before the macro ever leaves the machine. Full detail in
`MACRO_GENERATION_STAGE7_New.md`; the summary:

1. **Macro echo check** (`macro_echo.py`, wired into `generate_macro_package`
   right after the static audit). Every emitted geometry literal is parsed back
   out of the VBA and must round-trip to the build-plan value for the SAME
   feature. Catches cross-contamination (a literal belonging to a different
   feature), orphan literals (belonging to none), and missing values (a planned
   position never emitted). Raises `MacroEchoError`.
2. **Template-based emission** (`macro_template_engine.py` + `macro_templates/`).
   Circle/rectangle primitives are filled from exactly ONE feature's record;
   `fill()` is strict both ways, so a template structurally cannot reference
   another feature's data. Byte-faithful output.
3. **Fully-defined gate** — the `ReportSketchStatus` VBA helper reports (via
   `ISketch::GetConstrainedStatus`) whether each sketch ended fully defined:
   PASS/WARN, observable, not silently accepted.
4. **Open-edge overshoot** — `slot_cut.EDGE_OVERSHOOT_EPS = 0.050`; an open
   notch's open side crosses the part edge (closed sides + fillets exact).
   `_assert_open_edge_overshoot` refuses a span-equals-depth termination (the
   158-C enclosed-window bug). Notch corners for 158-C are now `6.30`, not
   `6.25`.
5. **Label/payload agreement** — `_assert_label_payload_agreement` refuses a
   step whose description names a foreign feature id.

**Effect on outputs:** every generated macro now carries the `ReportSketchStatus`
helper, and the two sketch-bearing macros call it after `FullyDefineSketch`.
`macros/` content is otherwise unchanged (the template routing is byte-faithful).
The golden snapshot (`tests/golden/bracket/macros/`) was regenerated for the
gate — an additive diff only.

---

## New READ-only surface — Tab-3 Visual Summary view-model

**Module:** **new** `pipeline/summary_view.py`; endpoint in `webapp/app.py`.

Not a pipeline stage — a pure presentation layer over artifacts the pipeline
already wrote. `build_summary(output_dir)` assembles a single view-model (part
header + a features/dimensions table + a build-plan table) consumed by the
webapp's Tab-3 summary band. All number formatting lives in this one module
(drawing-style numbers, `⌀` diameters, `(x, y)` positions, meters never
surfaced, `—` for absent). It reads the same per-part artifacts documented in
the original doc's output listing; it adds no new artifact of its own. Exposed
as `GET /api/parts/{session}/{part}/summary`. See
`docs/visual-summary-tables-2026-07-12.md`.

---

## New / changed files since the original

```
pipeline/macro_echo.py              NEW  — Stage-7 round-trip echo check
pipeline/macro_template_engine.py   NEW  — single-record VBA template fill
pipeline/macro_templates/*.vba.tmpl NEW  — audited circle/rectangle templates
pipeline/summary_view.py            NEW  — Tab-3 view-model builder (read-only)
pipeline/macro_generator.py         CHG  — echo check + 2 emission invariants + gate
pipeline/slot_cut.py                CHG  — EDGE_OVERSHOOT_EPS open-edge overshoot
pipeline/build_sequencer.py         CHG  — falsy-basis sweep
webapp/app.py                       CHG  — /summary endpoint
webapp/index.html                   CHG  — Tab-3 summary tables (UI only)
docs/macro-echo-hardening-2026-07-12.md      NEW
docs/visual-summary-tables-2026-07-12.md     NEW
tests/test_macro_echo.py            NEW  — echo/template/invariant tests (15)
tests/test_summary_view.py          NEW  — view-model tests (29)
tests/golden/bracket/macros/*       CHG  — regenerated for the sketch-status gate
```

## Test count

The original doc predated these: the suite is now **678 passing** (Stage-7
hardening added 15, the visual summary added 29). Zero regressions across the
changes above.
