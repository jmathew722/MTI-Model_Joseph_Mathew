# Stage 7 — Macro Generation (Current, Exhaustive)

A complete, current, line-level account of how the ordered build plan becomes a
folder of numbered SolidWorks VBA macros. This supersedes the original
`MACRO_GENERATION_STAGE7.md` and folds in every change through the latest commit:
the centralized coordinate normalization layer, the macro echo check, the
template engine, the fully-defined gate, and the full set of emission invariants.

**Modules:** `pipeline/macro_generator.py` (~3000 lines), `pipeline/macro_audit.py`,
`pipeline/macro_echo.py`, `pipeline/macro_template_engine.py`,
`pipeline/macro_templates/*.vba.tmpl`, `pipeline/slot_cut.py`,
`pipeline/coordinate_normalize.py`, `pipeline/reference_geometry.py`,
`pipeline/methods_config.py`. Fed by `pipeline/build_sequencer.py`'s ordered
`model.build_order`.

**Discipline** (unchanged constants): VBA uses named enum constants
(`swEndConditions_e.swEndCondBlind` …); only verified call shapes are emitted
(`FeatureExtrusion3`, `FeatureCut4` mirroring `solidworks_builder.py`); anything
unverified becomes a `' TODO: VERIFY API CALL` block, never invented silently;
every drawing value is written `value * UNIT_FACTOR` (meters); one macro per
feature, each appending PASS/FAIL to `logs/build_log.txt` and stopping on failure.

---

## 1. Entry point — `generate_macro_package()`

```python
def generate_macro_package(
    model: DrawingData,
    raw_extraction: dict[str, Any],
    verification_text: str,
    output_dir: Path | str,
    resolution: Any = None,
) -> MacroPackage
```

**Callers:** `pipeline/batch.py` (per-part pipeline, after Stage 2.5 + Stage 6),
wrapped in `try/except MacroGenerationError`; `reconciliation.py::_splice_recovered_features`
(Stage 10.5, into a scratch temp dir); `main.py` (single-drawing).

**`MacroPackage` fields:** `root`, `macros_dir`, `extraction_json`,
`verification_report`, `build_plan_json`, `resolved_extraction_json`,
`steps: list[BuildStep]`, `skipped`, `needs_review`, `dispositions: list[dict]`,
`reference_geometry: list[dict]`.

**Top-to-bottom sequence (current):**

1. `name = _safe_name(model.display_name)`; create `root/macros/logs`.
2. **Delete every stale `*.vba`** in `macros_dir` (dedup invariant).
3. Write traceability: `<part>_extraction.json`, `<part>_verification_report.txt`, `logs/.gitkeep`, and (if `resolution`) `<part>_resolved_extraction.json`.
4. `build_sequencer.sequence_build_order(model, resolution)` → sets `model.build_order`, appends hard failures to `model.warnings`, sets `pkg.dispositions`.
5. **Duplicate-feature assertion** over `model.build_order`.
6. **`_assert_no_dropped_positions`** (Bug-1 invariant).
7. Write `<part>_build_dispositions.json`.
8. Emit `00_setup.vba`.
9. Emit `01a_reference_geometry.vba` (if any datum entity derived — `REF_DATUM_A` is unconditional).
10. Loop `model.build_order` — dispatch each feature (unknown id → recorded skip; prohibited/unsupported → MANUAL; fillet/chamfer → deferred; slot_cut → `_emit_slot_decomposition`; circular-pattern-eligible hole → `_emit_circular_pattern_trio`; else per-type `_macro_*`). Each iteration appends a `(sub_name, body)` for `RUN_ALL.vba`.
11. Emit deferred fillets/chamfers as one combined `NN_fillets_chamfers.vba`.
12. **`_assert_no_overlapping_holes`** over all hole steps.
13. Emit `ZZ_final_verify.vba`, `ZZZ_export_stl.vba`, `RUN_ALL.vba`, `README.md`.
14. **Static audit** (`audit_package` → `write_audit_report`); any `error`-severity finding raises `MacroGenerationError`. Warnings log only.
15. **`_assert_open_edge_overshoot(pkg)`** — §9.
16. **`_assert_notch_orientation(model, pkg)`** — §9 (the 158-C top/bottom guard).
17. **`_assert_label_payload_agreement(pkg)`** — §9.
18. **`assert_macro_echo(pkg, macros_dir)`** — §8 (raises `MacroEchoError`; logs literal/macro counts).
19. Assemble/write `<part>_build_plan.json` (`_build_plan_dict`; includes `engineering_review`, `dispositions`, `reference_geometry`).
20. Write `<part>_engineering_review.txt`; log summary; return `pkg`.

So the guarantee ordering is: **static API audit → emission invariants → literal
round-trip → write the plan.** A failure at any guard refuses the whole package.

---

## 2. Numbered macro file scheme

`seq` starts at 0, incremented before each numbered emission; format
`f"{seq:02d}_..."`.

| Prefix / pattern | Trigger |
|---|---|
| `00_setup.vba` | Always first. New part from template, units, `SaveAs`. |
| `01a_reference_geometry.vba` | When `derive_reference_geometry(model)` returns ≥1 entity (effectively always — `REF_DATUM_A` unconditional). |
| `NN_<fid>_<desc_slug>.vba` | Standard single-feature macro; slug = `_vba_name(description)` = `re.sub(r"[^A-Za-z0-9]+","_",text)[:40]`. |
| `NN_<fid>_slot_rect_cut.vba` then `NN_<fid>_slot_corner_fillet.vba` | Inseparable adjacent pair from `_emit_slot_decomposition` (rect `must_complete` first, fillet `defer_on_failure` second). |
| `NN_<fid>_SeedHoleCut.vba`, `NN_<fid>_reference_axis.vba`, `NN_<fid>_circular_pattern.vba` | Circular-pattern trio from `_emit_circular_pattern_trio`. |
| `NN_fillets_chamfers.vba` | One combined file at the end for all `FILLET`/`CHAMFER` features. |
| `NN_<fid>_MANUAL_<type>.vba` | `feature.type in PROHIBITED` (`{SHELL}`) or not in `SUPPORTED`. Zero geometry — comments + `MsgBox` + `LogResult "WARN"`. |
| `ZZ_final_verify.vba` | Last numbered — `ForceRebuild3`, `GetMassProperties2`, bbox via `IBody2.GetBodyBox`, `Save3`. |
| `ZZZ_export_stl.vba` | Exports `<part>.stl` beside the `.sldprt`. |
| `RUN_ALL.vba` | One self-contained macro wrapping every `(sub_name, body)`. |
| `README.md` | Static `_MACROS_README`. |
| `RECONCILE_pass<N>_<orig>.vba` | Stage 10.5 splice only (copies one recovered feature's macro; never renumbers existing files). |

`SUPPORTED = {EXTRUDE_BOSS, EXTRUDE_CUT, HOLE, FILLET, CHAMFER, PATTERN, MIRROR,
THREAD, REVOLVE}`. Package regeneration deletes ALL `*.vba` up front, so a
description-slug change never leaves a stale second macro.

---

## 3. Coordinate normalization — the one canonical CAD frame

**Module:** `pipeline/coordinate_normalize.py`. The ONE place semantic drawing
anchors become global CAD coordinates, so the UI table and the VBA can never
disagree, and the 158-C top/bottom orientation bug is structurally impossible.

Convention: **lower-left origin, +X right, +Y up, +Z thickness.** Lengths stay in
inches through the model; `INCH_TO_M = 0.0254` / `to_meters()` is the ONE
inch→meter conversion, at the VBA boundary only.

- `Anchor` enum: `TOP_EDGE`, `BOTTOM_EDGE`, `LEFT_EDGE`, `RIGHT_EDGE`,
  `LOWER_LEFT`, `LOWER_RIGHT`, `UPPER_LEFT`, `UPPER_RIGHT`, `CENTER`,
  `DATUM_POINT`, `DATUM_AXIS`, `FEATURE_RELATIVE`, `ABSOLUTE_GLOBAL`.
- `resolve_notch_anchor(anchor, offset_x, offset_y, width, depth, height,
  parent_width, parent_height) -> Bounds` — **the single locus of the
  `H - depth` math**:
  - TOP: `y_min = parent_height - depth, y_max = parent_height`
  - BOTTOM: `y_min = 0, y_max = depth`
  - LEFT: `x_min = 0, x_max = depth` (Y from `offset_y`/`height`)
  - RIGHT: `x_min = parent_width - depth, x_max = parent_width`
- `resolve_point_anchor(...) -> Point` — LOWER/UPPER × LEFT/RIGHT, CENTER, and
  ABSOLUTE/DATUM/FEATURE_RELATIVE passthrough.
- `validate_bounds(bounds, parent_width, parent_height, overshoot_edge, overshoot_eps)`
  — degenerate / non-finite / out-of-parent checks with an open-edge overshoot
  allowance; returns violation strings (never raises).
- `assert_edge_orientation(anchor, bounds, parent_height, parent_width, depth)` —
  raises `CoordinateError` if an edge notch resolved to the wrong side
  (TOP at `y=0..depth`, etc.).
- `anchor_from_open_edge("top"|"bottom"|"left"|"right") -> Anchor | None`.

For **158-C**: `resolve_notch_anchor(TOP_EDGE, offset_x=1.56, width=1.62,
depth=1.88, parent_height=6.25)` → `Bounds(x_min=1.56, x_max=3.18, y_min=4.37,
y_max=6.25)`. `6.25 - 1.88 = 4.37`.

---

## 4. Reference-geometry emission (`01a_reference_geometry.vba`)

Naming contract (`reference_geometry.py`): `REF_DATUM_<A|B|C>` (planes),
`REF_SYM_<X|Y>` (symmetry mid-planes), `REF_AXIS_<purpose>` (centerlines/pattern
axes), `REF_PT_<feature_id>` (pattern origins/anchors).

`derive_reference_geometry(model)` emits: **`REF_DATUM_A`** (unconditional, base
feature's plane) → **`REF_DATUM_B/C`** (GD&T/dimension `datum_ref`; B→Right, C→Top)
→ **`REF_SYM_X/Y`** (symmetry) → **`REF_AXIS_C<i>`** (concentric groups) →
**`REF_AXIS_<feature_ref>`** (circular callouts) → **`REF_PT_<feature_ref>`** (qty>1
callouts) → **`REF_PT_<feature.id>`** (datum-chained hole anchors when related-dim
notes contain `between/spacing/pair/stagger/centerline/hole center/column/adjacent`).

VBA: planes via `InsertRefPlane` (offset or coincident, renamed to the id); axes
and points are comment-only placeholders in `01a` (the concrete axis with a real
`InsertAxis2` is built in the circular-pattern trio). `positioned_from(model,
feature)` populates `BuildStep.positioned_from` (`REF_PT_<fid>` for qty>1, else
`REF_DATUM_<letter>`, else `REF_DATUM_A`).

---

## 5. Per-feature emission

### Base solid / boss / profile cuts — `_macro_extrude`
`dims = _dims_map(...)`, `depth = _depth_of(dims)`, `plane = _plane_for(feature)`.
Position: `position_known` → offsets; circular → plate-envelope center; rectangle →
origin. `_profile_vba` draws a circle (center convention) or corner rectangle
(lower-left convention) — **now via the template engine** (§6). A boss with no depth
→ `MacroGenerationError`; neither diameter nor length+width → `MacroGenerationError`.
Feature call `_extrusion3` (boss) or `_cut4` (cut, direction-proof
`swEndCondThroughAllBoth` with a flipped-direction retry). `_feature_check_and_name`
names `{fid}_{_vba_name(desc)}`, `VerifySolidBody`, logs PASS/FAIL,
`WriteMacroResult`.

### Holes — `_macro_holes`
No callout → falls back to a plain circular cut. Positions =
`_hole_feature_positions` (§7). Each circle emitted via the template. `thru = h.thru
or type==THRU`; blind with `depth<=0` → `MacroGenerationError`. **Counterbore:** a
second concentric blind cut (`swFeatCb`, named `{fid}_cbore`). **Countersink:** a
`' TODO: VERIFY API CALL` chamfer comment + WARN. **Tapped:** a `' TODO: VERIFY API
CALL` cosmetic-thread comment + WARN (real helical threads prohibited).

### Slot decomposition — `_emit_slot_decomposition`
An open notch/slot is NEVER one arc-bearing sketch — always two adjacent steps.
`corner_array(slot, model)` is the ONE source of truth both derive from; it
**delegates its edge→global math to `resolve_notch_anchor`** (§3), then applies the
open-side overshoot (`EDGE_OVERSHOOT_EPS = 0.050`) and the near/interior-corners-
first ordering.

- **Step A — `slot_rect_cut`** (`must_complete=True`): `_macro_slot_rect` draws 4
  `CreateLine` from `corners_m` (METERS), through-cut; hard `End` on `Nothing`. The
  build-plan step records `sketch.corners_drawing_units`, `dimension_scheme`,
  `end_condition`, `open_edges`.
- **Step B — `slot_corner_fillet`** (`defer_on_failure=True`): `_macro_slot_fillet`
  selects edges by **vertex proximity** (never screen coords), asserts the expected
  corner count (`interior_corners`: 2 for open_notch, 4 for closed/obround) BEFORE
  `FeatureFillet3`; on mismatch it defers (`End`) rather than fillet the wrong count.
- **Overshoot table** (open side crosses the edge by ε; closed sides + fillets
  exact): top `y = H+ε`, bottom `y = -ε`, left `x = -ε`, right `x = right+ε`.
- Resolver validation (Stage 2.5, `validate_slot`): CRITICAL for fit
  (`anchor+width > extent`), radius (`2R > width`, `R > depth`); MEDIUM for
  ambiguous anchor semantics / single-view through-all. `normalize_legacy_slots`
  converts a legacy extrude_cut+child-fillet into one slot_cut.

### Patterns
- **Linear:** `_pattern_covered_by` — if the parent hole cut already baked all
  instances, a no-op `_macro_pattern_covered` (PASS); else `_macro_pattern_skeleton`
  (`' TODO: VERIFY API CALL`, `needs_review`).
- **Circular:** `route_to_circular_pattern` (true iff `pattern==CIRCULAR`,
  `qty>=2`, `bolt_circle_diameter>0`) → `_emit_circular_pattern_trio` (§ below);
  falls back to baked circles if no concentric bore or non-Front plane.

### Circular-pattern trio — `_emit_circular_pattern_trio`
Gate: `_bore_axis_probe` (needs a concentric bore ≥1.05× this diameter within tol)
AND `plane == "Front Plane"`. `axis_name = f"PatternAxis{n}"`.
- **Seed hole** (`_macro_seed_hole`): one circle at the seed position, named exactly
  `{fid}_SeedHoleCut`.
- **Reference axis** (`_macro_reference_axis`): finds the bore's cylindrical face
  GEOMETRICALLY (`IsCylinder` + `CylinderParams` radius ± 0.00002, center < 0.0005),
  `InsertAxis2(True)`, fallback exact-coordinate face probe at z ∈ {−t/2, +t/2, 0};
  renames the newest `RefAxis` to `axis_name`.
- **Circular pattern** (`_macro_circular_pattern`): `CreateCircularPatternSafe(axis,
  seed, n, total_angle_deg, reverse, geometry_pattern, vary_sketch, name, step)`.
- **`CIRCULAR_PATTERN_REQUIRED`** — every field must be non-null or
  `canonical_circular_pattern` raises `MacroGenerationError`. `total_instances`
  INCLUDES the seed (6 = seed + 5 copies), asserted once. `CreateCircularPatternSafe`
  (in `_HELPERS_VBA`): axis Mark=1, seed Mark=4 (`BODYFEATURE`), spacing in radians;
  version-pinned `FeatureCircularPattern5` with a `FeatureCircularPattern4` fallback;
  renames the pattern immediately.

### Chamfers / fillets — `_macro_fillet_chamfer`
One combined interactive macro (user pre-selects edges): checks
`GetSelectedObjectCount2(-1)=0` and prompts/skips. Radius/distance from dims with a
model-wide fallback; still `<=0` → **skipped** (recorded in `pkg.skipped`, never a
bare comment). Non-fatal on failure. Returns `(body, used, skipped)`.

### Prohibited / MANUAL, Revolve, Mirror
Prohibited/unsupported → numbered `MANUAL` macro (zero geometry, `status="skipped_prohibited"`,
`requires_input=True`). Revolve: real from `feature.revolve_profile` (≥2 pts,
`FeatureRevolve2`) else manual skeleton. Mirror: real if `parent_feature` resolves
(`InsertMirrorFeature2`, seed Mark=4) else manual.

---

## 6. Template-based emission — `pipeline/macro_template_engine.py`

`%%NAME` placeholders (VBA uses `$` and `@`, so both are unusable — `%%` never
appears in emitted VBA). `VbaTemplate(string.Template)` with `delimiter="%%"`,
uppercase-only `idpattern`.

`fill(name, record)` is **strict in both directions**: a missing placeholder OR an
unused record key raises `TemplateFillError`. Because `fill()` receives EXACTLY one
feature's record, a template structurally cannot reference another feature's data —
the cross-contamination class dies at the API boundary; the echo check then proves
the round-trip.

**Templates** (`pipeline/macro_templates/`): `sketch_circle.vba.tmpl` (the
`CreateCircleByRadius` line), `profile_rect.vba.tmpl` (the `CreateCornerRectangle`
lines). Routed through by `_profile_vba` (circle + rect), `_macro_holes` (per-instance
circles + counterbore circles), `_macro_seed_hole` (seed circle). Output is
**byte-faithful** to the previous f-string emission (golden set unchanged by the
routing). `template_names()` enumerates them for the static template audit; each
template's comment cites its METHODS.md recipe.

---

## 7. Per-instance hole placement — `_hole_feature_positions`

The A001271E distinction: when a `qty>1` callout is on ONE feature while SIBLING
features of the same diameter also exist, those siblings ARE the other instances —
this feature owns exactly ONE (never the whole shared layout). Only a VERIFIED
regular pattern (`is_verified_pattern`: bolt-circle, or uniform pitch + qty>=2 with
a single owning feature) lays out multiple instances. `_hole_group_features` groups
same-diameter (tol 1e-4) HOLE/THREAD features; `_corner_frame_shift` re-origins
centerline-referenced negatives into the corner frame.

---

## 8. Macro echo check — `pipeline/macro_echo.py`

The round-trip guarantee. `assert_macro_echo(pkg, macros_dir)` → `EchoReport`;
raises `MacroEchoError` on any issue.

**Parsing** anchored to the known call signatures (never arbitrary VBA), extracting
only the meaningful operands:
- `_CIRCLE_RE` → (cx, cy, dia) drawing units.
- `_RECT_RE` → corner (cx, cy) + length + width.
- `_LINE_RE` → slot `CreateLine` endpoints (METERS).
- `_ARRAY_RE` + `_RMETERS_RE` → slot-fillet corners (meters) + radius.

**Per in-scope macro** (`extrude_boss`, `extrude_cut`, `hole`, `thread`,
`slot_rect_cut`, `slot_corner_fillet`):
- **cross_contamination** — a literal in this feature's macro matching a DIFFERENT
  feature's plan value and not its own (the 158-C class); the issue names the
  blamed feature.
- **orphan_literal** — a literal matching no feature's plan value.
- **missing_value** — a planned position/corner that never appears as a literal.

Center/corner matches are PAIRS (x AND y together). Tolerances: `TOL_DRAWING=1e-3`,
`TOL_METERS=5e-6`. The missing-value check is frame-gated (circle/rect features
compare drawing units; slot features compare meters).

Regression: `tests/test_macro_echo.py::TestCrossContamination` doctors F002's macro
to carry F003's center and asserts a cross-contamination error naming both.

---

## 9. Generation-time invariants (all raise `MacroGenerationError`)

| Function | Check |
|---|---|
| `_macro_extrude` / `_profile_vba` / `_macro_holes` / `_macro_seed_hole` | boss with no depth; profile without diameter or length+width; blind hole with `depth<=0`. |
| `canonical_circular_pattern` | any `CIRCULAR_PATTERN_REQUIRED` field null. |
| `_assert_no_dropped_positions` | disposition `derivation_source` marks a position `needs_markup_review`/`position_unresolved` while the extraction carries a positional dimension for that feature (Bug-1). |
| duplicate-feature (inline) | any feature id appears >1 time in `build_order`. |
| `_assert_no_overlapping_holes` | two same-diameter instances within `max(min(d)/2, 1e-3)` on both axes. |
| **`_assert_open_edge_overshoot`** | an `open_edge` slot whose open-axis span equals the depth (coincident-with-edge termination — enclosed-window bug). |
| **`_assert_notch_orientation(model, pkg)`** | an open-edge slot resolved to the WRONG side vs the REAL parent envelope — e.g. a TOP_EDGE notch at `y=0..depth` (the exact 158-C top/bottom bug). Uses `assert_edge_orientation`. |
| **`_assert_label_payload_agreement`** | a step whose description names a FOREIGN feature id (only own/parent/base allowed). |
| Static audit gate | any `error`-severity finding from `audit_package`. |

A `MacroGenerationError` raised INSIDE the per-feature `try/except` in the main loop
is downgraded to a `needs_review` manual step; the package-level invariants above
always propagate.

---

## 10. Static VBA audit — `pipeline/macro_audit.py`

`error` (must never ship — generation raises) vs `warn` (recorded only).
**Banned APIs:** `E004` `\bGetModelBoundingBox\b` (invented API → use
`IBody2.GetBodyBox`); `E006` re-selecting a closed sketch by name
(`SelectByID2(...Name, "SKETCH")` → consume the active sketch). **Structural
(`.vba`):** missing `Option Explicit` (warn); unbalanced `Sub`/`End Sub` or
`Function`/`End Function` (error); a feature macro (`^\d\d_.*\.vba$`) with no
`LogResult` (warn; README exempt). `audit_package` globs every `*.vba` →
`AuditReport(.errors/.warnings/.ok)`; `write_audit_report` writes
`<part>_audit_report.json`.

---

## 11. Fully-defined sketch gate — `ReportSketchStatus`

In `_HELPERS_VBA` (every macro carries it). Read-only: after `FullyDefineSketch`,
queries `ISketch::GetConstrainedStatus` and logs `PASS` (status == 2, fully
defined) or `WARN` (under-defined → template under-specified). Called from
`_sketch_close_fully_define` right after `FullyDefineSketch`. A gate, not a fixer —
under-definition is observable, never silently accepted. Smart dimensioning
(`AddDimension2`/`SketchAddConstraints`) is intentionally NOT the default: those
call shapes are unverified on this machine's SolidWorks 2024, and Stage-7 discipline
is verified-call-shapes-only. It can be promoted once verified live, like
`HoleWizard5` (`MTI_ENABLE_HOLE_WIZARD`).

---

## 12. `macro_result.json` — machine-readable per-feature log

`WriteMacroResult(featureName, status, detail)` in `_HELPERS_VBA`. Path derived from
`GetCurrentMacroPathName`, one dir up into `logs/macro_result.json`. **JSON Lines** —
one hand-built object per line (`feature`, `status`, `detail`; backslashes → `/`,
quotes → `'`). Call sites: `_feature_check_and_name`, `_macro_circular_pattern`,
`_macro_reference_axis`, `_macro_slot_rect`, `_fail_block`. Complementary to
`LogResult`/`build_log.txt` (the human trail).

---

## 13. `methods_config.py` — construction-method dispatch

Machine half of `pipeline/METHODS.md`. `_DEFAULTS`: `hole/hole_cbore/hole_csk/
hole_tapped → sketch_circle_cut`, `slot → slot2d`, `cut → sketch_rect_cut`.
Override precedence: `_DEFAULTS` ← `methods.json` ← env `MTI_METHOD_<CLASS>`.
`method_for(class)` special-cases `hole*` when `MTI_ENABLE_HOLE_WIZARD` is set.
`_enrich_feature_step` records `BuildStep.construction_method` (traceability only;
schema fields, not this value, drive emission branching); consumed downstream by
`cq_prevalidate.py` / `construction_experiment.py`.

---

## 14. RUN_ALL, README, shared helpers, conventions

**`RUN_ALL.vba`** (`_build_run_all`): one self-contained macro embedding the SAME
`_HELPERS_VBA` + `_FIND_TEMPLATE_VBA` verbatim (so they can't drift), wrapping
`Step00_Setup`, every feature sub, `StepZZ_FinalVerify`, `StepZZZ_ExportStl`;
`main()` runs them in order; a failing step calls `End` (stop-on-first-failure).

**`_HELPERS_VBA`** (once per macro + RUN_ALL): `LogResult`, `VerifySolidBody`,
`WriteMacroResult`, `CreateCircularPatternSafe`, `SelectRefPlane`,
`ReportSketchStatus`.

**Units/coordinates:** `UNIT_FACTORS = {MM:0.001, CM:0.01, INCH:0.0254}`; every VBA
literal is `value * UNIT_FACTOR`. `PLANE_NAMES` maps view labels to the 3 standard
planes (direction-proof through-cuts). Coordinate frame fixed as
lower-left-corner-of-base-solid origin, +X right, +Y up (`coordinate_origin` stated
explicitly in `build_plan.json`).

---

## 15. Tests + verification

- `tests/test_coordinate_normalize.py` (23) — all four edge notches, point anchors,
  inch→meter, bounds validation, the orientation guard (correct top passes;
  top-at-bottom / bottom-at-top rejected), `open_edge`→anchor mapping, and the
  end-to-end 158-C generator regression.
- `tests/test_macro_echo.py` (15) — echo clean on plate-with-holes + 158-C;
  cross-contamination / orphan / raise-with-detail; template strictness both ways +
  static template audit; open-edge overshoot + no-overshoot refusal; label/payload
  agreement; falsy-basis reads as derived.
- `tests/test_golden_macros.py` — full package snapshot (regen with
  `UPDATE_GOLDEN=1`).
- `tests/test_slot_cut.py`, `test_commit_mode.py` — overshoot geometry (6.30, ±ε).
- CadQuery headless parity: 158-C builds a valid solid with the overshoot notch.

Full suite: **701 passing**, zero regressions.

## 158-C proof (end to end)

```
plate H = 6.25 in, notch depth = 1.88 in
y_min = H - depth = 6.25 - 1.88 = 4.37 in ; y_max = H = 6.25 in (open side -> 6.30)
F002 resolved:  x = 1.56 .. 3.18 in,  y = 4.37 .. 6.25 in   NEVER y = 0 .. 1.88
```

Generated `02_F002_slot_rect_cut.vba` CreateLine literals: closed edge
`0.110998 m` (= 4.37 in), open edge `0.160020 m` (= 6.30 in). Live SolidWorks COM
build remains the standard final check on a SolidWorks machine (not runnable
headless).
