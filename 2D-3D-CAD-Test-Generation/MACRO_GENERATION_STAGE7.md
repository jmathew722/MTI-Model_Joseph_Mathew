# Stage 7 — Macro Generation, Full Internal Detail

Modules: `pipeline/macro_generator.py` (2845 lines), `pipeline/macro_audit.py`,
`pipeline/slot_cut.py`, `pipeline/reference_geometry.py`,
`pipeline/methods_config.py`. Fed by `pipeline/build_sequencer.py`'s ordered
`model.build_order`.

This is a line-level account of how the ordered build plan becomes a folder of
numbered VBA macros. See `PIPELINE_SECTION2SECTION.md` for how Stage 7 fits
into the rest of the pipeline.

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

**Callers:**
- `pipeline/batch.py` — the main per-part batch pipeline, called right after
  Stage 2.5 resolution (`resolve_extraction`) and Stage-6 verification
  (`run_verification`). Wrapped in `try/except MacroGenerationError` — on
  failure the batch row is marked `"ERROR"`.
- `pipeline/reconciliation.py::_splice_recovered_features` (Stage 10.5) —
  regenerates a full fresh package into a **scratch temp dir** after a
  re-resolution pass recovers previously-broken features, then copies only the
  recovered feature's new macro file(s) into the real `macros/` dir under a
  `RECONCILE_pass<N>_` prefix (§10).
- `main.py` — the single-file/non-batch path calls it directly.

**Returned `MacroPackage` fields:**

| Field | Meaning |
|---|---|
| `root`, `macros_dir` | output dirs |
| `extraction_json`, `verification_report`, `build_plan_json` | traceability file paths |
| `resolved_extraction_json` | only set when `resolution` was passed |
| `steps: list[BuildStep]` | every emitted step, in order |
| `skipped: list[BuildStep]` | prohibited/unsupported features + fillet/chamfer values that couldn't be found |
| `needs_review: list[BuildStep]` | revolve w/o profile, mirror w/o seed, pattern w/o coverage, cosmetic thread, etc. |
| `dispositions: list[dict]` | the seven-stage per-feature disposition table from `build_sequencer.sequence_build_order` |
| `reference_geometry: list[dict]` | the datum skeleton (`REF_*` entities) |

**Top-to-bottom sequence:**

1. `name = _safe_name(model.display_name)`; create `root/macros/logs`.
2. **Delete every stale `*.vba`** in `macros_dir` first (dedup invariant, §6) — a description-slug change between runs can never leave a second macro behind for the same feature id.
3. Write traceability artifacts: `<part>_extraction.json`, `<part>_verification_report.txt`, `logs/.gitkeep`, and (if `resolution`) `<part>_resolved_extraction.json`.
4. Call `build_sequencer.sequence_build_order(model, resolution)` → sets `model.build_order`, appends `hard_failures` to `model.warnings`, sets `pkg.dispositions`.
5. **Duplicate-feature assertion** over `model.build_order` (§6).
6. **Dropped-position assertion** `_assert_no_dropped_positions` (§6).
7. Write `<part>_build_dispositions.json`.
8. Emit `00_setup.vba`.
9. Emit `01a_reference_geometry.vba` (if any datum entities derived — `REF_DATUM_A` is unconditional so this file is effectively always produced).
10. Loop `model.build_order` — dispatch each feature id to its emitter (unknown id → recorded skip; prohibited/unsupported → `NN_Fxxx_MANUAL_*`; fillet/chamfer → deferred to end; slot_cut → `_emit_slot_decomposition`; circular-pattern-eligible hole → `_emit_circular_pattern_trio`; else → per-type `_macro_*` builder). Each iteration appends a `(sub_name, body)` tuple for `RUN_ALL.vba`.
11. After the loop: emit all deferred fillets/chamfers as one combined `NN_fillets_chamfers.vba`.
12. **Overlapping-holes assertion** `_assert_no_overlapping_holes` (§6) — run once over every emitted hole step.
13. Emit `ZZ_final_verify.vba`.
14. Emit `ZZZ_export_stl.vba`.
15. Emit `RUN_ALL.vba` from all collected `(sub_name, body)` tuples.
16. Emit `macros/README.md`.
17. **Static audit**: `audit_package(macros_dir)` → `write_audit_report`; if `not audit.ok` → **raise `MacroGenerationError`** — generation fails outright.
18. Assemble/write `<part>_build_plan.json` (`_build_plan_dict`, includes `engineering_review`).
19. Write `<part>_engineering_review.txt`.
20. Log summary; return `pkg`.

---

## 2. The numbered macro file scheme

`seq` is a local counter starting at 0, incremented before each numbered emission; every numbered filename is `f"{seq:02d}_..."`.

| Prefix / pattern | Example | Trigger |
|---|---|---|
| `00_setup.vba` | fixed | Always first. Creates new part from template, sets units, `SaveAs` beside macros folder. |
| `01a_reference_geometry.vba` | fixed | Emitted whenever `derive_reference_geometry(model)` returns ≥1 entity (in practice always, since `REF_DATUM_A` is unconditional). |
| `NN_<feature_id>_<desc_slug>.vba` | `03_F004_Boss1.vba` | Standard single-feature macro; slug = `_vba_name(description)` → `re.sub(r"[^A-Za-z0-9]+","_",text)[:40]`. |
| `NN_<fid>_slot_rect_cut.vba` then `NN_<fid>_slot_corner_fillet.vba` | `04_F007_slot_rect_cut.vba` / `05_F007_slot_corner_fillet.vba` | Emitted as an **inseparable adjacent pair** by `_emit_slot_decomposition` whenever `model.slot_cut_for_feature(feature.id)` returns a slot — two consecutive `seq` values, rectangle (`must_complete=True`) always first, fillet (`defer_on_failure=True`) always second. |
| `NN_<fid>_SeedHoleCut.vba`, `NN_<fid>_reference_axis.vba`, `NN_<fid>_circular_pattern.vba` | trio | Circular-pattern trio from `_emit_circular_pattern_trio`, gated by `route_to_circular_pattern()` (§4/§10). |
| `NN_fillets_chamfers.vba` | one file | Emitted once, at the very end, for ALL features with `type in (FILLET, CHAMFER)` — these are collected into a `deferred` list during the main loop and never emitted individually. |
| `NN_<fid>_MANUAL_<type_slug>.vba` | `06_F009_MANUAL_shell.vba` | `feature.type in PROHIBITED` (currently just `SHELL`) or not in `SUPPORTED`. Creates **no geometry** — comments list the dims, `MsgBox`, `LogResult "WARN"`. `status="skipped_prohibited"`, `requires_input=True`. |
| `ZZ_final_verify.vba` | fixed | Always the last numbered step — `ForceRebuild3`, `GetMassProperties2`, bbox check via `IBody2.GetBodyBox`, `Save3`. |
| `ZZZ_export_stl.vba` | fixed | After `ZZ_` — exports `<part>.stl` beside the `.sldprt` by swapping the saved path's extension, `SaveAs3`. |
| `RUN_ALL.vba` | fixed | One self-contained macro wrapping every collected `(sub_name, body)` as its own `Sub`, called in order from `Sub main()`. |
| `README.md` | fixed | `_MACROS_README` template. |
| `RECONCILE_pass<N>_<original_file_name>.vba` | e.g. `RECONCILE_pass1_04_F007_...vba` | **Not** produced by `generate_macro_package` — produced by `reconciliation.py::_splice_recovered_features` (Stage 10.5). Copies only the recovered feature's new macro into the *existing* `macros/` dir under this prefix; never renumbers or touches existing files (§10). |

**Numbering/dedup mechanics:**
- Slot decomposition and the circular-pattern trio each consume 2 / 3 consecutive `seq` values respectively — helpers take/return `seq` explicitly (`seq = _emit_slot_decomposition(..., seq, ...)`).
- Fillets/chamfers share exactly one `seq` no matter how many deferred features exist.
- Package regeneration deletes ALL `*.vba` up front (`for old in macros_dir.glob("*.vba"): old.unlink()`).

---

## 3. Reference-geometry emission (`01a_reference_geometry.vba`)

Naming contract (from `reference_geometry.py`'s module docstring):

```
REF_DATUM_<A|B|C>    datum planes (explicit callouts or implied base faces)
REF_SYM_<X|Y>        symmetry mid-planes
REF_AXIS_<purpose>   centerlines / hole-pattern axes
REF_PT_<feature_id>  pattern origins / anchor points
```

`derive_reference_geometry(model) -> list[RefGeom]` (`RefGeom{id, type, definition, source, parent, parents, offset_m}`), deduped by `id`:

1. **`REF_DATUM_A`** — type `plane`, `definition="coincident"`, parent = the base feature's own plane (`_base_feature` picks the first `EXTRUDE_BOSS`/`REVOLVE` in build order, else a fallback). **Always present.**
2. **`REF_DATUM_B` / `REF_DATUM_C`** — from explicit GD&T datum letters (`model.geometric_tolerances[].datum`) or a dimension's `datum_ref`. `B → "Right Plane"`, `C → "Top Plane"` (`A` skipped — already added).
3. **`REF_SYM_X` / `REF_SYM_Y`** — one per `model.relationships.symmetry`, mapped `x/vertical → Right Plane`, `y/horizontal → Top Plane`; `definition="offset"` when a `half = envelope/2` is known, else `"coincident"`.
4. **`REF_AXIS_C<i>`** — one per `model.relationships.concentric_groups`, `type="axis"`, `definition="cyl_face"`.
5. **`REF_AXIS_<feature_ref>`** — for each hole callout with `pattern==CIRCULAR` and a `feature_ref`.
6. **`REF_PT_<feature_ref>`** — for each hole callout with `qty>1` and a `feature_ref` (pattern seed anchor), `parents=["REF_DATUM_A"]`.
7. **`REF_PT_<feature.id>`** (datum-chained hole anchor, A001271E) — for HOLE/THREAD features whose related dimension's notes contain any of `_HOLE_ANCHOR = ("between","spacing","pair","stagger","centerline","hole center","column","adjacent")`.

**VBA emission** (`reference_geometry_macro_body`):
- **Planes** (`_plane_vba`):
  - offset ≠ 0: select the parent plane by ID, then
    ```vba
    Set refFeat = swModel.FeatureManager.InsertRefPlane( _
        swRefPlaneReferenceConstraints_e.swRefPlaneReferenceConstraint_Distance, {offset_m}, 0, 0, 0, 0)
    ```
  - coincident: same `InsertRefPlane` call with offset `0#` — a zero-offset named plane exists purely to give the id a stable selection handle.
  - Every created feature is renamed `refFeat.Name = "{r.id}"`.
- **Axes** (`_axis_vba`): **comment-only placeholder** — states the axis is deferred to the owning feature/circular-pattern trio (built from the bore's cylindrical face when it exists), "never force-built" here.
- **Points** (`_point_vba`): **comment-only placeholder** — the real point is positioned when the seed sketch is placed.
- Final line: `LogResult "PASS", "01a_reference_geometry", "Built {n} named reference geometry landmark(s)"`.

`positioned_from(model, feature)` (populates `BuildStep.positioned_from`): hole callout with `qty>1` → `REF_PT_<fid>`; else a related dimension with a `datum_ref` → `REF_DATUM_<letter>`; else default `REF_DATUM_A`.

The one CONCRETE axis with a real `InsertAxis2` call is emitted separately in `_macro_reference_axis`, part of the circular-pattern trio (§4/§10) — not in `01a`.

---

## 4. Per-feature-type macro emission

### Base solid / boss / profile cuts — `_macro_extrude(model, feature, step, is_cut)`
- `dims = _dims_map(...)`, `depth = _depth_of(dims)`, `plane = _plane_for(feature)`.
- Position: `feature.position_known` → `offset_x/offset_y`; circular feature → plate-envelope center; rectangle → origin `(0,0)`.
- `thru = is_cut and depth is None`; a boss with no depth → `MacroGenerationError`.
- `_profile_vba` draws either:
  - a **circle**: `SketchManager.CreateCircleByRadius cx*UF, cy*UF, 0#, (diameter/2)*UF` — center convention.
  - a **rectangle**: `SketchManager.CreateCornerRectangle cx*UF, cy*UF, 0#, (cx+length)*UF, (cy+width)*UF, 0#` — lower-left-corner convention.
  - neither diameter nor length+width present → `MacroGenerationError`.
- Sketch opened via `_sketch_open` (`SelectRefPlane` + `InsertSketch True`); closed via `_sketch_close_fully_define` (`FullyDefineSketch`, then relies on the ACTIVE sketch — never re-selects by name; audit rule E006 bans that pattern).
- `_cut4(depth_expr, thru)` for cuts, `_extrusion3(depth_expr, blind=True)` for bosses:
  ```
  FeatureExtrusion3(True, False, False, end, swEndCondBlind, depth, 0.01, False, False, False, False,
                     0#, 0#, False, False, False, False, True, True, True, swStartSketchPlane, 0#, False)
  ```
  `FeatureCut4` uses `swEndCondThroughAllBoth` for thru cuts; if the returned feature is `Nothing`, re-finds the most recent `"ProfileFeature"` in the tree (walking `FirstFeature`/`GetNextFeature`, never by name) and retries with direction flipped (`swEndCondThroughAll`).
- `_feature_check_and_name` names the feature `"{feature.id}_{_vba_name(description)}"`, calls `VerifySolidBody`, logs PASS/FAIL, calls `WriteMacroResult`.

### Holes (plain / cbore / csk / tapped) — `_macro_holes(model, feature, step)`
- No hole callout → falls back to `_macro_extrude(..., is_cut=True)` (bare circular cut).
- Positions = `_hole_feature_positions(model, feature)` (§5) — only the instance(s) THIS feature owns.
- `thru = h.thru or h.type == HoleType.THRU`; blind hole with `h.depth<=0` → `MacroGenerationError`.
- One `CreateCircleByRadius` per owned position in one sketch, then one `_cut4`.
- **Counterbore**: `h.type==COUNTERBORE and cbore_diameter>0 and cbore_depth>0` → a **second concentric blind cut**, new sketch at `cbore_diameter`, `_cut4(depth, thru=False, var="swFeatCb")`, named `"{feature.id}_cbore"`.
- **Countersink**: `h.type==COUNTERSINK and csink_diameter>0` → emits a `' TODO: VERIFY API CALL` comment block instructing manual chamfer application (edge selection by coordinates deemed unreliable); `LogResult "WARN"`.
- **Tapped**: `h.type==TAPPED and h.thread_spec` → `' TODO: VERIFY API CALL - cosmetic thread` comment (real helical threads are explicitly prohibited); instructs manual `Insert > Annotations > Cosmetic Thread`; `LogResult "WARN"`.

### `slot_rect_cut` + `slot_corner_fillet` — `_emit_slot_decomposition`
Rule from `slot_cut.py`'s docstring: a U-shaped cutout/open notch/slot is **never** built as one arc-bearing sketch — always exactly two ordered, adjacent steps.

- `corners = corner_array(slot, model)` — the single source of truth both steps derive from (4 corners, lower-left frame; interior/near corners first for an open notch). `anchor_semantics=="edge_to_centerline"` shifts `a -= w/2`. Branches on `open_edge in ("top","bottom","left","right")` vs. a fully-interior closed slot.
- `fillet_corners = interior_corners(slot, corners)` — 2 corners for `open_notch`, all 4 for `closed_slot`/`obround`.
- `expected_corner_count(slot)` → 2 or 4.
- **Step 1 (rectangle, `must_complete=True`)** — `_macro_slot_rect`: draws 4 `CreateLine` segments from `corners_m`, plane preference `SelectRefPlane("REF_DATUM_A", 1)` falling back to `"Front Plane"`; `FeatureCut4` with `swEndCondThroughAllBoth` (thru) or `swEndCondBlind`. On `Nothing` → hard `MsgBox`/`LogResult "FAIL"`/`WriteMacroResult`/`End` — **no silent continuation**, this step is mandatory. `sketch_plane="REF_DATUM_A"`; `slot` dict records `dimension_scheme` (which dim id maps to which edge-to-edge measurement) and `end_condition`.
- **Step 2 (fillets, `defer_on_failure=True`)** — `_macro_slot_fillet`: selects target edges by **vertex proximity**, never `SelectByID2` with screen coordinates — enumerates `swBody.GetEdges`, computes each edge's midpoint via `GetCurveParams3(0,0)`, picks the nearest edge to each target corner. Asserts `selCount == count` (expected corner count) **before** calling `FeatureFillet3` — on mismatch: `LogResult "WARN"` and `End` (deferred, never filleted with the wrong count). `FeatureFillet3(swFeatureFilletPropagate, rMeters, 0#, 0#, 0,0,0, Nothing×7)`. `corner_radius<=0` → `status="needs_review"`, appended to `pkg.needs_review`.
- **Legacy normalization** — `slot_cut.normalize_legacy_slots()` converts a legacy `extrude_cut` (description matching `_NOTCH_KEYWORDS = ("notch","slot","u-cut","u cut","u-shape","u shape","cutout","keyway","channel")`) plus a child fillet feature into one canonical `slot_cut` dict, dropping the loose fillet from `features`/`build_order`, raising a MEDIUM flag.
- **Resolver validation** (Stage 2.5, before this stage ever runs) — `validate_slot(slot, model)`: CRITICAL for fit violation (`anchor+width > axis_extent`), `2R > width`, `R > depth`; reclassifies `closed_slot` → `obround` when `2R == width` (MEDIUM); MEDIUM + default to `edge_to_near_edge` when `anchor_semantics` unset; MEDIUM + gate question when `thru_basis=="single_view_default"` with only one ortho view.
- Note: no literal `EDGE_OVERSHOOT_EPS` constant/name exists in the current codebase snapshot — the open-edge overshoot concept is expressed only as descriptive prose in `_macro_slot_rect`'s docstring comment ("for an open notch the open side's lines extend past the part edge so the cut cleanly breaks the edge"); the actual extended coordinates are computed inline inside `corner_array()`'s edge branches (e.g. `top = height or (a+d)`), not via a separately named epsilon constant.

### Patterns (linear / circular)
- **Linear**: `_pattern_covered_by(model, feature)` checks whether the pattern's instances were already baked as multiple circles in the parent hole cut (`h.qty >= max(feature.quantity, 2)`) — if so, emits a no-op `_macro_pattern_covered` (`LogResult "PASS"`, "already satisfied"). Otherwise `_macro_pattern_skeleton` — a `TODO: VERIFY API CALL` manual-step comment (`FeatureLinearPattern` needs a pre-selected seed + direction edge that can't be chosen reliably), `status="needs_review"`.
- **Circular** — routed via `route_to_circular_pattern(model, h)`: true only if `h.pattern==CIRCULAR and h.qty>=2 and h.bolt_circle_diameter>0`. If true, `_emit_circular_pattern_trio` attempts the 3-macro path (falls back to baked circles if `_bore_axis_probe` finds no concentric bore, or plane isn't `"Front Plane"`).
  - `CIRCULAR_PATTERN_REQUIRED` — every field must be non-null or `canonical_circular_pattern()` raises `MacroGenerationError`:
    ```python
    CIRCULAR_PATTERN_REQUIRED = (
        "feature_type", "seed_feature_name", "pattern_axis", "total_instances",
        "equal_spacing", "total_angle_deg", "reverse_direction", "instances_to_skip",
        "geometry_pattern", "vary_sketch", "bolt_circle_radius_in", "seed_angle_deg",
    )
    ```
  - **`total_instances` includes the seed** — asserted once, never reinterpreted downstream ("6 = seed + 5 copies").
  - **`CreateCircularPatternSafe`** shared VBA helper (defined once in `_HELPERS_VBA`, used by every macro and `RUN_ALL`), citing `IFeatureManager::FeatureCircularPattern5, dispid 261`:
    ```vba
    Function CreateCircularPatternSafe(axisName As String, seedName As String, _
        totalInstances As Integer, totalAngleDeg As Double, reverseDir As Boolean, _
        geometryPattern As Boolean, varySketch As Boolean, newName As String, stepName As String) As Boolean
    ```
    - pattern axis: `SelectByID2(axisName, "AXIS", 0,0,0, False, 1, Nothing, 0)` — **Mark:=1**.
    - seed feature: `SelectByID2(seedName, "BODYFEATURE", 0,0,0, True, 4, Nothing, 0)` — **Mark:=4**, exact tree name.
    - `spacingRad = totalAngleDeg * 4 * Atn(1) / 180` (radians, `EqualSpacing=True`).
    - `FeatureCircularPattern5(totalInstances, spacingRad, reverseDir, "NULL", geometryPattern, True, varySketch, False, False, False, 1, spacingRad, "NULL", False)`.
    - **Fallback** if `Nothing`: `FeatureCircularPattern4(totalInstances, spacingRad, reverseDir, "NULL", geometryPattern, True, varySketch)` (older-release 7-arg signature).
    - Names the new feature immediately (`swFeat.Name = newName`) so downstream selection never depends on auto-numbering.
  - Full trio detail in §10.

### Chamfers / fillets — `_macro_fillet_chamfer(model, features, step)`
- Contract: "the drawing shows WHERE, the macro applies the exact extracted VALUE" — requires the user to pre-select edge(s) in the graphics area before running; checks `GetSelectedObjectCount2(-1) = 0` and prompts/skips if nothing selected.
- Fillet: radius from `dims["fillet_radius"] or dims["radius"] or first dim value`; `<=0` → `_model_radius_fallback` (first `fillet_radius`/`radius` dimension anywhere in the model); still `<=0` → **skipped** (recorded in `skipped`, never silently dropped). `FeatureFillet3(swFeatureFilletPropagate, radius*UF, 0#,0#,0,0,0, Nothing×7)`. Non-fatal on failure (`LogResult "WARN"`, continues).
- Chamfer: distance from `dims["chamfer"] or dims["length"] or first value`; angle default 45°; `_model_chamfer_fallback` similarly. `InsertFeatureChamfer(4, 1, distance*UF, angle_rad, 0#,0#,0#,0#)`.
- Returns `(body, used, skipped)` — `skipped: list[(feature_id, reason)]` surfaced into `pkg.skipped` (so a skip can never vanish as a bare VBA comment).

### Prohibited features → MANUAL steps
`feature.type in PROHIBITED` (`{SHELL}`) or not in `SUPPORTED = {EXTRUDE_BOSS, EXTRUDE_CUT, HOLE, FILLET, CHAMFER, PATTERN, MIRROR, THREAD, REVOLVE}` → numbered `NN_Fxxx_MANUAL_<type>.vba`. Creates zero geometry — comments list every dimension key/value, `MsgBox`, `LogResult "WARN"`. `status="skipped_prohibited"`, `requires_input=True`; appended to both `pkg.skipped` and `pkg.steps`.

### Revolve / Mirror
- `_macro_revolve`: real revolve only if `feature.revolve_profile` has ≥2 points (`revolve_sketch_points` closes the half-profile polygon back to the axis at `y=0`); draws `CreateCenterLine` + `CreateLine` loop, `FeatureRevolve2(True, True, False, False, False, False, 0, 0, 2π, 0#, False, False, 0#, 0#, 0, 0#, 0#, True, True, True)`. No profile → `_macro_revolve_skeleton` manual comment, `status="needs_review"`.
- `_macro_mirror`: real mirror only if `feature.parent_feature` resolves; selects mirror plane by name, selects seed via `SelectByID2(seed_name, "BODYFEATURE", ..., True, 4, ...)` (Mark 4 again), `InsertMirrorFeature2(False, False, True, False)`. No seed → manual `MsgBox`, `status="needs_review"`.

---

## 5. Per-instance hole placement — `_hole_feature_positions`

```python
def _hole_feature_positions(model: DrawingData, feature: Feature) -> list[tuple[float, float]]:
```

The critical distinction (A001271E): when a callout with `qty>1` is attached to ONE feature while SIBLING features of the same diameter also exist, those siblings ARE the other instances — this feature owns exactly ONE of them. Only a VERIFIED regular pattern with a single owning feature lays out multiple instances.

1. `h = model.hole_callout_for_feature(feature.id)`; `is_pat, _ = is_verified_pattern(model, h)`.
2. `h` exists AND `is_pat` → return `_hole_positions(model, h)` (full multi-instance layout — genuine pattern).
3. `group = _hole_group_features(model, feature)` — every HOLE/THREAD feature sharing this feature's nominal diameter (`_hole_diameter_of`, tolerance `1e-4`).
4. **Case A** — `h is not None and h.instance_positions and len(group) <= 1`: sole feature for its callout → owns every explicitly-dimensioned instance, returns all of `h.instance_positions` (corner-frame shifted).
5. **Case B / individual** — `feature.position_known` → return that single feature's own `(offset_x, offset_y)` (never copy a sibling's or lay out the shared callout).
6. Else if `h.instance_positions` non-empty → use only `h.instance_positions[0]`.
7. Else if `h` exists → callout-driven fallback via `_hole_positions` (single/centered).
8. Else → envelope center, last resort.

**`is_verified_pattern(model, h)`** — biased toward "individual" (a mis-built individual-as-pattern is wrong geometry; a pattern built as individuals is merely more lines):
- `h is None` → `(False, "none->individual")`.
- `h.bolt_circle_diameter > 0` → `(True, f"bolt_circle_{value}")`.
- `h.pattern in (LINEAR, CIRCULAR) and h.pattern_spacing>0 and h.qty>=2` → `(True, f"uniform_pitch_{spacing}")`.
- else → `(False, "none->individual")`.

`_hole_positions(model, h)` (the callout-level layout, used only for genuine patterns) precedence: explicit `h.instance_positions` → `_circular_positions` (bolt-circle ring, evenly spaced from `start_angle`) → grounded linear spacing (`_effective_spacing`, centered row about envelope) → single position (`position_known`) → envelope center. `_corner_frame_shift` re-origins negative-coordinate (centerline-referenced) positions into the corner frame by adding `length/2, width/2` when the envelope is known.

---

## 6. Hard generation-time invariants (raise `MacroGenerationError`)

| Function | Check | Message |
|---|---|---|
| `_macro_extrude` | boss with no depth/height dimension | `"{step}: extrude_boss has no depth/height dimension."` |
| `_profile_vba` | no diameter and no length+width | `"{step}: profile needs a diameter or length+width; got {sorted(dims)}"` |
| `_macro_holes` | blind hole, `h.depth<=0` | `"{step}: blind hole {h.id} has no depth."` |
| `_macro_seed_hole` | blind seed hole, `h.depth<=0` | `"{step}: blind seed hole {h.id} has no depth."` |
| `revolve_sketch_points` | <2 profile points | `"revolve profile needs at least 2 points."` |
| `canonical_circular_pattern` | any `CIRCULAR_PATTERN_REQUIRED` field is `None` | `"{fid}: circular_pattern spec is incomplete — null field(s) {...}; refusing to emit VBA."` |
| **`_assert_no_dropped_positions`** (Bug-1 invariant) | disposition's `derivation_source` mentions `"needs_markup_review"`/`"position_unresolved"` while the extraction DOES carry a positional dimension for that feature | `"INVARIANT VIOLATION ({fid}): disposition reports position '{deriv}' while the extraction carries a positional dimension for {fid}. The extracted location was dropped instead of consumed (Bug 1). Refusing to build..."` |
| **duplicate-feature-in-build-order** (inline) | any feature id appears >1 time in `model.build_order` | `"DUPLICATE FEATURE(S) in build_order {dups}: each feature id must be built exactly once..."` |
| **`_assert_no_overlapping_holes`** (A001271E) | within a same-diameter group, two instances resolve within `max(min(d1,d2)/2, 1e-3)` of each other on both axes | `"OVERLAPPING HOLES ({f1} vs {f2}): two instances of the same diameter group resolve to ~({x1},{y1}) and ({x2},{y2})... This is the collapsed-instance bug..."` — run once after ALL holes are emitted, before COM/SolidWorks ever sees the plan. |
| **Static audit gate** (end of generation) | `audit_package(macros_dir)` finds any `error`-severity finding | `"Generated macros failed static self-validation: {rule_id: file: message; ...}"` |

**Soft/non-fatal:** a `MacroGenerationError` raised inside the per-feature `try/except` in the main loop (e.g. profile-shape error, missing depth during per-feature emission) is caught and downgraded to a `needs_review` manual-step VBA — only the package-level invariants above always propagate all the way up.

---

## 7. `macro_audit.py` — static VBA audit

Every generated package is statically checked so a known failure mode (each logged in `docs/solidworks-macro-error-log.md`, E001–E010) can never silently ship again.

**Severities:**
- `error` — a generator defect that must never ship (nonexistent API). Triggers `generate_macro_package` to **raise** `MacroGenerationError`.
- `warn` — worth a human glance, non-fatal, recorded in the audit report only.

**Banned APIs (`BANNED_APIS`):**
1. `E004` — `\bGetModelBoundingBox\b`: "IModelDoc2.GetModelBoundingBox does not exist (invented API)." Replacement: read the box from the solid body via `IBody2.GetBodyBox`.
2. `E006` — `SelectByID2\(\s*[A-Za-z_]\w*Name\b[^,]*,\s*"SKETCH"`: "Re-selecting a closed sketch by name is unreliable." Replacement: consume the active sketch directly, or re-find by `ProfileFeature` type.

**Structural checks** (`.vba` files, `warn` unless noted):
3. Missing `"Option Explicit"` → `warn STRUCT`.
4. Unbalanced `Sub`/`End Sub` count → **`error` STRUCT**.
5. Unbalanced `Function`/`End Function` count → **`error` STRUCT**.
6. Feature macros (`^\d\d_.*\.vba$`) that never call `LogResult` → `warn STRUCT` ("no PASS/FAIL trail"); `README.md` exempt.

**`audit_package(macros_dir)`** globs every `*.vba`, runs `audit_text` on each, returns an `AuditReport(findings)` with `.errors`/`.warnings`/`.ok`.

**On violation:** `write_audit_report(audit, root/f"{name}_audit_report.json")` is always written; if `not audit.ok`, generation **raises** `MacroGenerationError` and no usable package is delivered (though the audit JSON and partial macro files stay on disk). Warnings only log (`log.warning`), never block.

---

## 8. `macro_result.json` — machine-readable per-feature outcome log

Defined once in `_HELPERS_VBA` (embedded verbatim in every standalone macro AND in `RUN_ALL.vba` — "defined ONCE here so the per-feature macros and the single-run RUN_ALL.vba cannot drift apart"):

```vba
Sub WriteMacroResult(featureName As String, status As String, detail As String)
    On Error Resume Next
    Dim macroPath As String, p As String, f As Integer, q As String
    q = Chr$(34)
    macroPath = swApp.GetCurrentMacroPathName
    p = Left$(macroPath, InStrRev(macroPath, "\")) & "..\logs\macro_result.json"
    f = FreeFile
    Open p For Append As #f
    Print #f, "{" & q & "feature" & q & ": " & q & featureName & q & ", " & _
        q & "status" & q & ": " & q & status & q & ", " & _
        q & "detail" & q & ": " & q & Replace(Replace(detail, "\", "/"), q, "'") & q & "}"
    Close #f
    On Error GoTo 0
End Sub
```

- **Path**: derived at runtime from `swApp.GetCurrentMacroPathName`, one directory up from `macros/`, into `logs/macro_result.json`.
- **Format**: JSON Lines — one hand-built JSON object literal per `Print #f` call (not a JSON array; each line independently parseable). Fields: `feature`, `status` (`"PASS"`/`"FAIL"`), `detail` (backslashes → `/`, embedded quotes → `'`).
- **Purpose**: every feature-creation outcome is recorded (feature name → success/fail) so the web UI/FastAPI side surfaces the EXACT failing feature instead of a generic exit code.
- **Call sites**: `_feature_check_and_name` (every extrude/cut/revolve outcome), `_macro_circular_pattern` (pattern PASS/FAIL), `_macro_reference_axis` (axis PASS), `_macro_slot_rect` (FAIL path), `_fail_block` (every hard failure across all macro types).
- **Distinct from `build_log.txt`** (`LogResult` Sub — human-readable `"yyyy-mm-dd hh:nn:ss  [STATUS]  step -- detail"` lines, same relative-path convention but `..\logs\build_log.txt`): `LogResult` is the human trail, `WriteMacroResult` is the machine-readable one.

---

## 9. `methods_config.py` — construction-method dispatch

The machine-readable half of `pipeline/METHODS.md` — a small, override-friendly config so a discovered better construction method becomes permanent pipeline behavior.

**Defaults:**
```python
_DEFAULTS = {
    "hole": "sketch_circle_cut",
    "hole_cbore": "sketch_circle_cut",   # + second concentric blind cut
    "hole_csk": "sketch_circle_cut",
    "hole_tapped": "sketch_circle_cut",  # drill + cosmetic thread
    "slot": "slot2d",
    "cut": "sketch_rect_cut",
}
```
`_KNOWN_METHODS` whitelist: `hole → {sketch_circle_cut, hole_wizard5}`; `slot → {slot2d, create_sketch_slot, capsule_profile}`; `cut → {sketch_rect_cut}`.

**Override precedence:** `load_methods()` = `_DEFAULTS` ← `methods.json` (same dir; malformed file never breaks dispatch, caught silently) ← env var `MTI_METHOD_<CLASS_UPPER>`.

**`method_for(feature_class)`**: looks up `load_methods()`, falls back to `_DEFAULTS`; special-cases `hole*` classes — if `MTI_ENABLE_HOLE_WIZARD` env var is set, always returns `"hole_wizard5"` (matches `solidworks_builder.py`). Note: `HoleWizard5` returned `None` on a clean SolidWorks 2024 part, so it's explicitly not the default.

**How `macro_generator.py` reads it** — inside `_enrich_feature_step` (populates `BuildStep.construction_method`, wrapped in `try/except: pass` so a missing config never breaks emission):
```python
h = model.hole_callout_for_feature(feature.id)
if feature.type in (HOLE, THREAD):
    fclass = ("hole_tapped" if (h and h.thread_spec) else
              "hole_cbore" if (h and h.cbore_diameter > 0) else
              "hole_csk" if (h and h.csink_diameter > 0) else "hole")
elif feature.type == EXTRUDE_CUT:
    fclass = "slot" if getattr(feature, "profile", "") == "slot" else "cut"
else:
    fclass = ""
if fclass:
    step.construction_method = method_for(fclass)
```
The value is recorded per-step in `build_plan.json` (`"construction_method"`, only when non-empty) purely for traceability — it does **not** itself branch macro-emission control flow here (that's driven by schema fields like `slot_cut_for_feature`/`route_to_circular_pattern`). It's consumed downstream by `cq_prevalidate.py` and `construction_experiment.py`.

---

## 10. Other implementation detail

### Circular-pattern trio — `_emit_circular_pattern_trio`
Gate: `probe = _bore_axis_probe(model, h)` requires a concentric bore (another hole callout with diameter ≥1.05× this one's, whose position matches this pattern's center within `tol = max(0.05, 0.02*max(length,width,1))`) AND `plane == "Front Plane"`. Either failing → `None`, caller falls back to baked-circle instances (`log.info(...  "using baked-circle instances")`).

- `axis_no = 1 + count of existing "circular_pattern" steps`; `axis_name = f"PatternAxis{axis_no}"`.
- **Step 1 — Seed hole** (`_macro_seed_hole`): single circle at `_seed_position` (bolt center + radius @ start_angle), cut, named exactly `f"{feature.id}_SeedHoleCut"` so the pattern step can select it by that name.
- **Step 2 — Reference axis** (`_macro_reference_axis`): finds the bore's cylindrical face GEOMETRICALLY — enumerates `swBody.GetFaces`, checks `swSurfAx.IsCylinder`, matches `CylinderParams` (radius ± `0.00002`, center distance `< 0.0005`) against the generated bore's exact coordinates; on match, `swFaceAx.Select4` + `InsertAxis2(True)`. **Fallback**: exact-coordinate face probe via `SelectByID2("", "FACE", px*UF, py*UF, z, ...)` tried at `z ∈ {-thickness/2, +thickness/2, 0}` (both sides — base-extrude direction is template-dependent). Renames the newest `"RefAxis"`-type feature to `axis_name`.
- **Step 3 — Circular pattern** (`_macro_circular_pattern`): `CreateCircularPatternSafe(axis_name, seed_name, n, total_angle_deg, reverse_direction, geometry_pattern, vary_sketch, pat_name, step)`; on failure, `SendMsgToUser2 "PATTERN FAILED at {label} ({feature.id})", swMbStop, swMbOk` then `End`. On success: `VerifySolidBody`, `LogResult`, `WriteMacroResult`.
- Each of the 3 steps is its own `BuildStep` in `pkg.steps` (`feature_type` in `{"hole","reference_axis","circular_pattern"}`); the circular-pattern step alone carries `step.circular_pattern = spec` (only serialized into `build_plan.json` when non-empty).

### Deferred / manual step conventions
- **Deferred (fillets/chamfers)**: collected in a local `deferred: list[Feature]` during the main loop, never emitted per-feature, combined into exactly one `NN_fillets_chamfers.vba` at the very end — always run last both in numbered order and in `RUN_ALL.vba`.
- **Manual** (prohibited/unsupported, or a builder that raised `MacroGenerationError`): numbered like a normal step but creates zero geometry — comment + `MsgBox` + `LogResult` only; `status` is `"skipped_prohibited"` (structurally unsupported) or `"needs_review"` (builder threw at generation time); both funnel into `pkg.needs_review`/`pkg.skipped` → `build_plan.json`'s `needs_review`/`skipped_prohibited` arrays → the engineering review.

### `RECONCILE_pass<N>_` splices (Stage 10.5)
`reconciliation.py::_splice_recovered_features` is the only other code that writes into `macros/` after the initial `generate_macro_package` call. It builds a brand-new package into a scratch dir with the corrected model/resolution, then for each recovered feature id, copies just that feature's freshly-generated macro file into the REAL `macros_dir` as `RECONCILE_pass{pass_num}_{original_name}` — **never renumbering or overwriting existing files** — and patches the real `build_plan.json`'s `steps` (replace or append, stripping the fid out of `skipped_prohibited`/`needs_review`) and the real `*_build_dispositions.json`. Each copied step's `notes` gets an explicit annotation: *"added by reconciliation pass N — run this macro manually or re-run RUN_ALL after regenerating the package; a full .sldprt rebuild is needed to reflect this in the 3D model"* — these splices update JSON/VBA artifacts only, never reopen/rebuild the already-built `.sldprt`.

### `RUN_ALL.vba`'s role
Built by `_build_run_all(model, unit_factor, feature_subs)`. A single self-contained macro that:
- Re-declares `UNIT_FACTOR`, `swApp`/`swModel`, embeds the SAME `_HELPERS_VBA` and `_FIND_TEMPLATE_VBA` blocks verbatim so it can never drift from the individually-numbered macros.
- Wraps `_setup_body(...)` as `Sub Step00_Setup()`, every `(sub_name, body)` (including `01a`'s reference-geometry sub if present, every feature/slot/pattern/manual/fillet-chamfer sub in build order) as its own named `Sub`, `_final_verify_body` as `Sub StepZZ_FinalVerify()`, `_export_stl_body` as `Sub StepZZZ_ExportStl()`.
- `Sub main()` calls them in sequence: `Step00_Setup` → every feature sub → `StepZZ_FinalVerify` → `StepZZZ_ExportStl`; `LogResult` brackets the run; final `MsgBox "RUN_ALL finished. See ..\logs\build_log.txt..."`.
- A failing step calls VBA `End` (via `_fail_block` or inline checks), halting the ENTIRE `RUN_ALL` run — same stop-on-first-failure semantics as running numbered macros individually.
- No install/Python needed on the SolidWorks machine — paste once, press F5 once.

### `README.md` generation
Static `_MACROS_README` template, `.format(folder=name, name=name)`, written verbatim to `macros/README.md`. Documents the `RUN_ALL` fast path; the 8-step numbered-macro walkthrough (copy folder → open SolidWorks 2024 → paste/run `00_setup.vba` → run each numbered macro in order, stopping on first failure → run `NN_fillets_chamfers.vba` interactively → run `ZZ_final_verify.vba` → run `ZZZ_export_stl.vba`); notes that `TODO: VERIFY API CALL` macros need manual completion, `POSITION ASSUMED` comments need verification against the drawing, and to check `{name}_build_plan.json` for the full step list including anything skipped as prohibited (lofts/sweeps/shells are never generated).

### Shared VBA scaffolding (`_HELPERS_VBA`)
Emitted verbatim into every standalone macro's header AND once into `RUN_ALL.vba`. Five Subs/Functions:
- `LogResult` — writes to `build_log.txt`.
- `VerifySolidBody` — checks `GetBodies2(swSolidBody, True)` is non-empty, logs bbox via `IBody2.GetBodyBox`.
- `WriteMacroResult` — `macro_result.json` (§8).
- `CreateCircularPatternSafe` (§4/§10).
- `SelectRefPlane(planeName, planeIndex)` — tries `planeName`, then `planeName` minus `" Plane"` suffix, then `"Plane"+index` via `SelectByID2(..., "PLANE", ...)`; falls back to walking `FirstFeature`/`GetNextFeature` counting `"RefPlane"`-type features positionally (`PLANE_INDEX = {"Front Plane":1, "Top Plane":2, "Right Plane":3}` — a name-independent fallback for non-English/reordered templates).

### Units and coordinate conventions
- `UNIT_FACTORS = {MM: 0.001, CM: 0.01, INCH: 0.0254}` — drawing units → meters (SolidWorks API is meters-only); every VBA literal is `value * UNIT_FACTOR`.
- `PLANE_NAMES` maps drawing sketch-plane labels (`front/top/right/side/second_side/left/bottom/back/rear`) to one of the 3 standard planes; `bottom → "Top Plane"` (opposite face), `side`/`second_side`/`left → "Right Plane"` — "direction-proof" since thru-cuts reach material either way.
- Coordinate frame is fixed as **lower-left-corner-of-base-solid origin** (`coordinate_origin` field in `build_plan.json`), `+X right`, `+Y up` — stated explicitly so no downstream consumer has to infer it.
