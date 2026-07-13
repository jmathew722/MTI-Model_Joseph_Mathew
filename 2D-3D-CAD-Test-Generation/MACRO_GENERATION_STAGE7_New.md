# Stage 7 — Macro Generation: What Changed Since MACRO_GENERATION_STAGE7.md

This is a **delta** doc. It records everything that changed in Stage 7 since
`MACRO_GENERATION_STAGE7.md` was written (commit `b217f24`). Read the original
for the full internal account of `generate_macro_package`; read this for the
hardening layer added by commit `3052706` (the 158-C Stage-7 hardening).

Everything here is a generation-time guard: it turns a coordinate, dimensioning,
or scoping bug into an instant, NAMED generation failure — instead of a wrong
`.sldprt` discovered later on the SolidWorks machine.

---

## 1. New modules

| File | Role |
|---|---|
| `pipeline/macro_echo.py` | Round-trip verifier — parses emitted VBA literals back out and proves they match the build plan for the SAME feature. |
| `pipeline/macro_template_engine.py` | Single-record VBA template fill (`%%NAME` placeholders). |
| `pipeline/macro_templates/sketch_circle.vba.tmpl` | The circle construction line, audited once. |
| `pipeline/macro_templates/profile_rect.vba.tmpl` | The corner-rectangle construction lines, audited once. |

---

## 2. Macro echo check — `pipeline/macro_echo.py`

The headline addition. Public entry points: `check_macro_echo(pkg, macros_dir)`
→ `EchoReport`, and `assert_macro_echo(pkg, macros_dir)` which raises
`MacroEchoError` on any discrepancy.

**How it parses.** Anchored to the KNOWN call signatures the emitters produce —
never a scan of arbitrary VBA, so structural constants (`0#`, `2#`, `0.01`) are
never mistaken for data. Regexes extract only the meaningful operands:

- `_CIRCLE_RE` — `CreateCircleByRadius CX * UNIT_FACTOR, CY * UNIT_FACTOR, 0#, (DIA / 2#) * UNIT_FACTOR` → (cx, cy, dia) in drawing units.
- `_RECT_RE` — `CreateCornerRectangle CX*…, CY*…, 0#, _ (CX + LEN)*…, (CY + WID)*…` → corner (cx, cy) + length + width.
- `_LINE_RE` — slot `CreateLine x1, y1, 0#, x2, y2, 0#` (literals in METERS).
- `_ARRAY_RE` + `_RMETERS_RE` — slot-fillet target corners `Array(x, y)` (meters) + `rMeters = R * UNIT_FACTOR`.

**What it checks, per in-scope macro** (`extrude_boss`, `extrude_cut`, `hole`,
`thread`, `slot_rect_cut`, `slot_corner_fillet` — scaffolding, reference axes,
interactive fillet/chamfer, and the angle-driven circular-pattern step carry no
directly-mappable coordinate literals and are skipped):

- **cross_contamination** — a parsed literal that is NOT in this feature's plan
  values but IS in another feature's → the issue names the blamed feature (the
  158-C "corner-array coordinates in the wrong macro" class).
- **orphan_literal** — a parsed literal that matches no feature's plan value.
- **missing_value** — a planned position/corner that never appears as a literal.

**Frames + tolerances.** Circle/rect features compare in drawing units
(`TOL_DRAWING = 1e-3`); slot features compare in meters (`TOL_METERS = 5e-6`).
The missing-value check is gated by frame so a slot's meters corners are never
reported spuriously missing against drawing-unit positions and vice versa.
Center/corner matches are done as PAIRS (x AND y together), which is what makes
a leaked (x, y) pair detectable even when one coordinate coincidentally matches.

**Wiring** (`generate_macro_package`): after `audit_package` /
`write_audit_report`, and after the two new emission invariants (below):

```python
_assert_open_edge_overshoot(pkg)
_assert_label_payload_agreement(pkg)
from pipeline.macro_echo import assert_macro_echo
echo = assert_macro_echo(pkg, macros_dir)   # raises MacroEchoError on mismatch
```

Regression test: `tests/test_macro_echo.py::TestCrossContamination` doctors
F002's macro to carry F003's center and asserts a `cross_contamination` issue
naming both features.

---

## 3. Template-based emission — `pipeline/macro_template_engine.py`

**Delimiter is `%%`** (not `$`): VBA uses `$` (`Left$`, `Format$`, `Chr$`) and
`@` (`Point1@Origin`), so both are unusable as markers. `VbaTemplate` subclasses
`string.Template` with `delimiter = "%%"` and an uppercase-only `idpattern`.

`fill(name, record)` is **strict in both directions**:
- a placeholder the record does not provide → `TemplateFillError` (no silently
  empty holes in the VBA);
- a record key the template never consumes → `TemplateFillError` (a leftover key
  means the record builder and template disagree about what this feature emits —
  the drift class that leaks a foreign field).

Because `fill()` receives EXACTLY one feature's record, a template structurally
cannot reference another feature's values — the cross-contamination class dies
at the API boundary, and the echo check then proves the round-trip.

**What is routed through templates now** (in `macro_generator.py`, via
`_tmpl_fill`):
- `_profile_vba` — circle and rectangle profile construction (base bosses,
  profile cuts, circular features).
- `_macro_holes` — the per-instance hole circles + the counterbore circles.
- `_macro_seed_hole` — the circular-pattern seed circle.

Output is **byte-faithful** to the previous f-string emission (the golden macro
set is unchanged by the routing — only the sketch-status gate changed it).
`template_names()` enumerates the templates for the static template audit
(`tests/test_macro_echo.py::TestTemplateEngine::test_every_template_passes_static_audit`),
so generated output inherits the banned-API audit. Each template's leading
comment cites the METHODS.md recipe it implements.

---

## 4. Fully-defined sketch gate — `ReportSketchStatus`

Added to `_HELPERS_VBA` (so every macro carries it). A READ-ONLY VBA helper that,
after `FullyDefineSketch`, queries the documented `ISketch::GetConstrainedStatus`
and logs `PASS` (fully defined, status == 2) or `WARN` (still under-defined →
"template under-specified"). Called from `_sketch_close_fully_define` right after
`FullyDefineSketch`:

```vba
swModel.SketchManager.FullyDefineSketch True, True, 0, True, 1, Nothing, 1, Nothing, 0, 0
ReportSketchStatus "<step>"   ' gate: log fully-defined vs under-defined (Task 3)
```

This makes sketch definition **observable** — a gate, not a fixer. It uses only
a verified read-only API. `AddDimension2` / `SketchAddConstraints` smart-
dimensioning (emitting a driving dimension per build-plan value) was
deliberately NOT made the default: those call shapes are not yet verified on
this machine's SolidWorks 2024, and Stage 7's discipline is verified-call-shapes-
only. The gate surfaces under-definition today; smart dimensioning can be
promoted to the default path once verified live, exactly as `HoleWizard5` is
gated behind `MTI_ENABLE_HOLE_WIZARD`.

---

## 5. Open-edge overshoot — `pipeline/slot_cut.py`

New constant `EDGE_OVERSHOOT_EPS = 0.050` (drawing units). `corner_array()` now
pushes an open notch's OPEN side PAST the part edge by ε; the closed sides stay
exact, and `interior_corners()` / the corner fillets are unaffected (always the
closed-side pair). Per open edge:

| open_edge | closed corners | open corners |
|---|---|---|
| top    | `y = top - depth` | `y = top + ε` |
| bottom | `y = depth`       | `y = -ε` |
| left   | `x = depth`       | `x = -ε` |
| right  | `x = right - depth`| `x = right + ε` |

A closed slot has no open edge → no overshoot. For 158-C the notch corners are
now `[[1.56, 4.37], [3.18, 4.37], [3.18, 6.30], [1.56, 6.30]]` (was `6.25`) — a
cut coincident with the `6.25` edge built an enclosed WINDOW instead of an open
notch (numerically fragile). The rectangle sketch and the corner-fillet edge
selection still derive from the one `corner_array()` source of truth.

---

## 6. New generation-time invariants — `pipeline/macro_generator.py`

Both raise `MacroGenerationError` (they join the existing
`_assert_no_dropped_positions`, duplicate-feature, and
`_assert_no_overlapping_holes` invariants).

**`_assert_open_edge_overshoot(pkg)`** — for every `slot_rect_cut` step whose
slot has an `open_edge`, the sketch's open-axis span must EXCEED the slot depth
(by ~ε). A span equal to the depth means the overshoot was lost and the cut
terminates at the edge — refused:

> `OPEN-EDGE CUT WITHOUT OVERSHOOT (F002): … span (1.88) does not exceed the depth (1.88) — the cut terminates at the edge instead of crossing it, which builds an enclosed window, not an open notch. Refusing to generate.`

**`_assert_label_payload_agreement(pkg)`** — a step's description may name only
its own feature id, its parent (`parent_feature_id`), or (for a compound step
like `F002_fillets`) the base feature. A foreign real-feature id in a
description means the label was assembled from a different record than the values
the step carries — refused:

> `LABEL/PAYLOAD DISAGREEMENT (F002): its description names foreign feature F001 … Refusing to generate.`

---

## 7. `generate_macro_package` — updated tail

The orchestration tail now reads:

```
… emit all macros …
audit = audit_package(macros_dir)                 # existing static audit
write_audit_report(...)                            # existing
if not audit.ok: raise MacroGenerationError(...)   # existing
# ── NEW (2026-07-12) ──
_assert_open_edge_overshoot(pkg)                   # §6
_assert_label_payload_agreement(pkg)               # §6
assert_macro_echo(pkg, macros_dir)                 # §2  (logs literal/macro counts)
plan = _build_plan_dict(...); write build_plan.json
write_review(...)
```

So the ordering of guarantees is: static API audit → emission invariants →
literal round-trip → write the plan. A failure at any guard refuses the whole
package rather than shipping a suspect macro.

---

## 8. Tests + verification

- `tests/test_macro_echo.py` (15) — echo passes clean on a plate-with-holes and
  on 158-C; the cross-contamination / orphan / raise-with-detail cases; template
  fill strictness both ways + unknown template + static template audit; open-edge
  overshoot (158-C = 6.30) + the no-overshoot refusal; label/payload agreement
  (own+parent allowed, foreign refused); falsy-basis reads as derived.
- `tests/test_golden_macros.py` — regenerated for the `ReportSketchStatus` gate
  (additive diff only; run with `UPDATE_GOLDEN=1` after an intentional change).
- `tests/test_slot_cut.py`, `tests/test_commit_mode.py` — updated to the
  overshoot geometry (`6.30`, `±ε` open ends).
- CadQuery headless parity: 158-C builds a valid solid with the overshoot notch.
- Live SolidWorks COM build is the standard final check on a SolidWorks machine;
  it is not runnable in a headless environment.

Full suite: **678 passing**, zero regressions.
