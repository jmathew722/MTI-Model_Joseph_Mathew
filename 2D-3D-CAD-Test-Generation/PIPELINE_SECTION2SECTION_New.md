# MTI 2D→3D Pipeline — Section by Section (Current, Detailed)

A complete, current, stage-by-stage walkthrough of the pipeline in `pipeline/`,
orchestrated by `main.py` (single-drawing / `--views-folder`) and
`pipeline/batch.py` (`process_drawing_data`, used by the web UI and
`run_models.py`). This supersedes the original `PIPELINE_SECTION2SECTION.md` and
folds in every change through the latest commit — the centralized coordinate
normalization layer, the Stage-7 hardening (macro echo check, templates,
emission invariants, fully-defined gate), and the Tab-3 visual summary.

**Guiding principle** (from the README): *a complete approximate model is always
the correct outcome; an incomplete model is always the wrong outcome* — resolve
and flag, never block or silently drop. Numbers are chosen from extracted
candidates, never invented; compliance grades are never fabricated.

Every stage is additive: a missing input/key skips the stage without breaking
the run. Extraction JSON is backward-compatible (new fields additive; old JSONs
keep loading via `--from-json`).

---

## Stage 0 — Input prep

**Module:** `pipeline/utils/image_prep.py`

Normalizes and downscales input (PDF page render, PNG/JPG, DWG/DXF converted via
`/api/convert-dwg`) to a raster the vision model can consume, capped by
`MAX_IMAGE_LONG_EDGE`. Multi-view input (`--views-folder`): each part is a folder
of per-view images; view role comes from filename keywords (`front`/`side`/`top`…)
or a leading `01`–`05`; a file named `full`/`overview`/`isometric` or matching the
folder name is overview context only (not built as a plane). A **front view + one
other orthographic view** is required.

### Stage 1.2 (escalation only) — Tiled high-res extraction

**Module:** `pipeline/utils/tiled_extraction.py`

Not the default path. `should_tile()` fires on ANY of: the "appears nearly blank"
heuristic, ink density < 0.5%, extraction confidence < 0.6, > 25% of dims flagged
unclear, or a C-size+ page at the ≤2576px cap. Then `adaptive_render()` re-renders
the vector PDF at escalating DPI (300→600→900) until median line width ≥ 2.5px;
`make_tiles()` cuts ~1500px tiles at 22% overlap; each content-bearing tile is
extracted in sheet coordinates; `stitch()` merges by anchor+value (conflicts kept
as candidate `possible_values` for Stage 2.5); `datum_anchor()` re-expresses
positions from the datum. Tile cost logs separately (`extraction_tiled`); tiles
cache by (page hash, DPI, grid). VLM calls are injected so the machinery is
unit-tested without paid calls.

---

## Stage 1.5 — Holistic overview analysis

**Module:** `pipeline/overview_analysis.py`

The FULL uncropped sheet (`00_full.jpg`) goes to Claude Sonnet 5 with a RELATIONAL
prompt, `temperature=0` — it does **not** re-extract dimensions. Reports: views on
the sheet, cross-view feature correspondences (through-vs-blind), overall 3D shape,
cross-view conflicts with severity + recommendation, symmetry, and global notes
(e.g. "(6) HLS" with a `resolved_count`). Fed to Stage 2.5 as **priority tier 2**
(tier 0 = must-meet specs, tier 1 = per-view extraction owns dimension values,
tier 2 = overview owns cross-view relationships). A deterministic callout-count
cross-check becomes a flag (`source: overview_analysis`).

**Output:** `<Part>_overview_analysis.json`. Additive — no key/failure skips it;
`--from-json` reuses a sibling file. Shown in the UI as the collapsible "Overview
Analysis" panel.

---

## Stage 2 — Extraction

**Modules:** `pipeline/extractor.py`, schema `pipeline/schema.py`

One Claude Vision call per part — all views labeled, forced tool call against a
Pydantic v2 schema, `temperature=0`. **Specs-first:** the operator's must-meet text
is injected into the prompt so the model looks for those features from the start;
the spec text is part of the cache key (changed specs force a fresh extraction).
Balloon/BOM guardrails prevent a callout note being synthesized as a phantom
feature, and forbid duplicating a hole feature as a separate pattern feature.

Raw extraction JSON is always saved; an on-disk cache
(`<output>/.extraction_cache/`) makes identical re-runs free. Token/USD costs
append to `token_usage_log.txt` via `usage_log.py`.

**Output:** `<Part>_extraction.json` — the permanent ground truth, never
overwritten by any later stage.

---

## Stage 3 — Vector hole resolution

**Modules:** `pipeline/vector_extract/`, `pipeline/hole_resolution.py`

Exact hole positions from the original vector file — DXF/DWG entities via `ezdxf`,
vector-PDF Bézier circles via PyMuPDF, HoughCircles raster fallback. **Precedence:**
vector geometry owns *position*, the vision callout owns *semantics*
(diameter/thread/depth) — disagreement keeps both and flags CRITICAL. Every hole
carries `position_source` + `position_confidence`.

**Output:** folds into the extraction/resolution JSON.

---

## Stage 2.6 — Spec reconciliation (must-meet parsing)

**Module:** `pipeline/must_meet.py`

The operator's must-meet text (`must_meet_spec.txt`; legacy `notes.txt`) is parsed
into structured `MM-xxx` constraints via a dedicated Claude call with a
deterministic regex fallback (works with no key). **Constraints are priority tier
0 — they override vision-extracted values on any conflict**; every conflict →
`lessons_learned.jsonl` (`resolution: spec_override`). Missing geometry parameters
are derived where possible (bolt-circle fit: radius = mean √(x²+y²) about the hole
centroid; radii disagreeing > 0.005 in = CRITICAL). Exception: a spec hole COUNT
contradicting explicitly dimensioned drawing positions keeps the drawing's
geometry, flags CRITICAL, MM check fails with measured-vs-required
(`spec_vs_drawing_disagreement`).

**Output:** `must_meet_constraints.json`; `must_meet` block in
`resolved_extraction.json`.

---

## Stage 2.5 — Resolution (the core design decision)

**Module:** `pipeline/resolver.py`

**The pipeline never blocks on ambiguity.** Every unclear dimension gets a numeric
`resolved_value` chosen from extracted candidates (never fabricated) via a
deterministic tree: spec-driven → arithmetic chain → geometric validity →
conservative geometry → last-resort default. Tagged HIGH/MEDIUM/LOW/CRITICAL.

- **Specs-first:** an operator must-meet value clarifying an ambiguous reading
  takes precedence (Step 0, `assumption_basis="spec_driven"`).
- **Commit-to-extraction mode** (`commit_mode`, default ON): no human in the loop —
  builds every extracted feature. Positional dimensions are consumed before any
  escalation. A step/notch cut missing length+width derives its rectangle from the
  outer-profile envelope minus its partial anchor dims (`basis="profile_delta"`); a
  hole missing a diameter inherits the most-common sibling diameter
  (`_sibling_diameter`); a genuinely-undimensioned size/position commits a
  declared-basis conservative value (`committed_conservative`, never `[0,0]`),
  built and CRITICAL-flagged. No feature TYPE reaches `EXCLUDED_INCOMPLETE` (a
  fillet/chamfer with no size commits a small shop-typical edge-break; a pattern
  with no count+spacing commits a conservative spacing). Only hard failure: no
  closed outer profile.
- **Extraction-verbatim carry + determinism:** a sole clean reading resolves at
  `confidence=1.0, basis="extracted_verbatim"`; sub-1.0 confidence is reserved for
  ≥2 genuine candidates and always carries the candidate list + deciding rule.
  `resolve_extraction` is a pure function; `pipeline/resolution_cache.py` proves it
  by hashing (extraction JSON, resolver version) and cross-checking a same-key
  result — a mismatch logs a determinism-violation error.
- **Phantom-feature reconciliation:** `_reconcile_phantom_duplicate` runs before
  any exclusion — a `pattern`/`mirror` feature whose parent already accounts for
  the same callout quantity, or a BOM/balloon/applied-item note mis-synthesized as
  a feature, is reclassified to `PHANTOM_RECLASSIFIED` (removed from build_order,
  LOW-tier informational flag, never CRITICAL, never gating).
- **Per-instance hole placement + datum chaining:** `_classify_hole_groups`
  classifies each hole group as `placement: pattern` (hard evidence only — a
  bolt-circle or uniform pitch with a single owning feature) or `placement:
  individual` (default bias), recording `pattern_evidence` and a per-instance
  `position_basis` datum chain.
- **Falsy-basis discipline** (see Stage 6.5): an assumption with a blank basis
  reads as derived, never as directly-extracted.

**Output:** `<Part>_resolved_extraction.json`; `.resolution_cache/`.

---

## Stage 6 — Validation

**Module:** `pipeline/validator.py`

Arithmetic/envelope verification. Advisory by default; `--strict-gate` makes
failures blocking (the run still produces outputs otherwise).

**Output:** `<Part>_verification_report.txt`.

---

## Coordinate normalization — the one canonical CAD frame

**Module:** `pipeline/coordinate_normalize.py` (consumed by Stage 6.5 / 7)

The ONE place semantic drawing anchors become global CAD coordinates, so the UI
table and the VBA can never disagree. Convention: **lower-left origin, +X right,
+Y up, +Z thickness.** Lengths stay in inches through the model; conversion to
meters happens exactly once at the VBA boundary via `INCH_TO_M = 0.0254` /
`to_meters()`.

- `Anchor` enum: `TOP_EDGE`, `BOTTOM_EDGE`, `LEFT_EDGE`, `RIGHT_EDGE`,
  `LOWER_LEFT`, `LOWER_RIGHT`, `UPPER_LEFT`, `UPPER_RIGHT`, `CENTER`,
  `DATUM_POINT`, `DATUM_AXIS`, `FEATURE_RELATIVE`, `ABSOLUTE_GLOBAL`.
- `resolve_notch_anchor(...)` — edge notches → `Bounds`. **The single locus of the
  `y = parent_height - depth` math** (158-C: 6.25 − 1.88 = 4.37).
- `resolve_point_anchor(...)` — holes/corners/center → `Point`.
- `validate_bounds()` — in-parent check with open-edge overshoot allowance;
  `assert_edge_orientation()` — refuses a notch resolved to the wrong side.

`slot_cut.corner_array()` delegates its edge→global math here (byte-faithful).

---

## Stage 6.5 — Canonical build sequencer

**Module:** `pipeline/build_sequencer.py`

The ONE deterministic build-order pass, called at the top of
`generate_macro_package`. Re-orders completeness-gate survivors into a fixed
**seven-stage** sequence with a stable within-stage sort keyed to feature id →
byte-identical `build_order` across runs:

```
0 reference geometry
1 base solid (largest closed profile)
2 additive bosses
3 profile subtractions
4 holes: plain → cbore/csk → tapped
5 patterns
6 chamfers → fillets
7 non-geometric
```

Because the same `model` object flows onward, macros, `build_plan.json`, CadQuery
pre-validation, and the COM build all inherit this order.

**Disposition states** (`<Part>_build_dispositions.json`, also in
`build_plan.json`'s `dispositions`): `BUILT`, `BUILT_WITH_DERIVED_VALUE`
(resolver-inferred value), `EXCLUDED_INCOMPLETE` (missing parameter named; in
commit-mode reached only by non-committable edge/pattern treatments),
`PHANTOM_RECLASSIFIED`, and the overlay `NEEDS_HUMAN_INPUT`.

**Falsy-basis sweep:** `_EXPLICIT_BASES` and `_READ_POSITIONS` no longer contain
`""`; `_derivation_source` classifies a blank-basis assumption as
`"unspecified_basis"` (blank position → `"position:unspecified"`) →
`BUILT_WITH_DERIVED_VALUE`. An assumption with a missing basis can no longer
masquerade as a clean read.

---

## Stage 7 — Macro generation

**Modules:** `pipeline/macro_generator.py`, `pipeline/macro_audit.py`,
`pipeline/macro_echo.py`, `pipeline/macro_template_engine.py`,
`pipeline/macro_templates/`, `pipeline/slot_cut.py`,
`pipeline/reference_geometry.py`, `pipeline/methods_config.py`,
`pipeline/coordinate_normalize.py`.

Turns the ordered build plan into numbered VBA macros (`00_setup` …
`ZZZ_export_stl`, `RUN_ALL.vba`, `README.md`). Prohibited features
(loft/sweep/shell) become `NN_Fxxx_MANUAL_*.vba` steps. **Full internal detail in
`MACRO_GENERATION_STAGE7_New.md`** — this is a summary of the guarantees:

- **Static audit** (`macro_audit.py`) — banned/nonexistent APIs (E004, E006) +
  structural checks fail generation.
- **Macro echo check** (`macro_echo.py`) — every emitted geometry literal is
  parsed back out of the VBA (anchored to the known call signatures) and must
  round-trip to the build-plan value for the SAME feature; cross-contamination,
  orphan literals, and missing values raise `MacroEchoError`.
- **Template-based emission** (`macro_template_engine.py` + `macro_templates/`) —
  circle/rectangle primitives fill from EXACTLY one feature's record; a template
  structurally cannot reference another feature's data.
- **Fully-defined gate** — `ReportSketchStatus` (read-only `GetConstrainedStatus`
  after `FullyDefineSketch`) logs PASS/WARN; under-definition observable, not
  silently accepted.
- **Emission invariants** (all raise `MacroGenerationError`): open-edge overshoot,
  notch orientation (the 158-C top/bottom guard), label/payload agreement,
  no-dropped-positions, no-overlapping-holes, duplicate-feature-in-build-order.
- **Circular-pattern trio** — seed hole → named reference axis → pattern via the
  single `CreateCircularPatternSafe` helper (version-pinned
  `FeatureCircularPattern5`, fallback `...4`).
- **Slot decomposition** — every open notch/slot is two adjacent steps: a
  mandatory `slot_rect_cut` then a deferred `slot_corner_fillet`, sharing one
  `corner_array()` source of truth (which delegates to the coordinate resolver).
- **Reference geometry** (`reference_geometry.py`) — `01a_reference_geometry.vba`
  builds the datum skeleton (`REF_DATUM_*`, `REF_SYM_*`, `REF_AXIS_*`,
  `REF_PT_<fid>`) before any feature.

**Output:** `macros/` (numbered `.vba`, `RUN_ALL.vba`, `README.md`,
`logs/macro_result.json`), plus `build_plan.json`'s `reference_geometry[]` block.

---

## Stage 8 — CadQuery pre-validation

**Module:** `pipeline/cq_prevalidate.py`

Headlessly rebuilds the SAME geometry from `build_plan.json` (single shared source
of truth; circular patterns via `.polarArray(radius, seedAngle, 360, count)` +
`cutThruAll`; slot cuts cut the identical stored `corners_drawing_units` polygon).
Checks watertightness/volume/hole counts against MM constraints. **A failed check
aborts the SolidWorks build** and surfaces the exact constraint (`MM-001 FAILED:
…`). Graceful no-op when `cadquery` isn't installed.

**Output:** `prevalidation.stl`, `prevalidation_report.json`, `prevalidate.py`.

---

## Stage 9 — SolidWorks COM build

**Modules:** `pipeline/solidworks_builder.py`, `pipeline/model_validator.py`

Windows-only COM build of `.sldprt` + STL export + mass/bbox check. Same
circular-pattern trio as the VBA path. Features renamed deterministically right
after creation; per-feature outcomes written to `macro_result.json`. Optional
`HoleWizard5` path opt-in (`MTI_ENABLE_HOLE_WIZARD=1`, default OFF). Everything
upstream runs on any OS.

**Output:** `<Part>.SLDPRT`, `<Part>.STL`, `<Part>_model_check.txt`.

---

## Stage 10 — Post-build must-meet verification

**Module:** `pipeline/constraint_verify.py`

The built STL is measured with `trimesh` (cross-section circle fitting; through-all
= the hole appears near both faces) and every MM constraint graded PASS/FAIL with
measured vs required. **A run with MM constraints is only READY when every
constraint passes**; each failure → `lessons_learned.jsonl` with the responsible
VBA snippet.

**Output:** `constraint_verification.json`.

---

## Stage 10.5 — Reconciliation pass

**Module:** `pipeline/reconciliation.py`

The pipeline's closing check of its output against the **original raw**
`_extraction.json` (never the resolved/downstream artifacts). Builds a ground-truth
checklist (every feature id + expected instance count) and diffs it against the
disposition table + `build_plan.json`'s actual instance positions. A justified
`skipped_prohibited` is accepted; a `PHANTOM_RECLASSIFIED` entry is represented
explicitly (`phantom_reclassified[]`, `accounted_total`), never a miss; anything
else missing/short is named.

On a gap, re-runs **only** `resolve_extraction` (never the extractor — no paid API
call) with every requirements/overview signal freshly reloaded, up to `max_passes`
(default 3); a pass recovering nothing new stops the loop. A recovered feature is
spliced into the existing `build_plan.json` and a new `RECONCILE_pass<N>_*.vba`
macro added (no existing file renumbered) — the `.sldprt` is not hot-patched; a
full rebuild is needed, noted in the report.

**Output:** `<Part>_reconciliation_report.json` (`checklist_total`,
`confirmed_built`, `loop_passes_used`, `unresolved[]`, `splices_applied[]`,
`phantom_reclassified[]`, `accounted_total`, `final_status: READY |
READY_WITH_OPEN_ITEMS`). Any unresolved item gates READY (exit code 8).

---

## Stage 10.6 — Per-feature geometric verification

**Module:** `pipeline/feature_verify.py`

Measures **every** planned feature against `build_plan.json` — each hole's position
+ diameter + through/blind, each profile cut/notch's location (material-absence
probe; open-edge slots get an `EDGE_NOT_BROKEN` cross-section AT the drawn edge),
each slot's obround, the base envelope (+ a COM-vs-CadQuery volume cross-check).
Every feature ends OK / MISSING / MISPLACED / WRONG_SIZE / EXTRA / EDGE_NOT_BROKEN /
UNMEASURABLE (always with a stated reason).

**Output:** `<Part>_feature_verification.json` (not always produced — the Tab-3
summary degrades to "pending" when absent).

---

## Stage 10.7 — Geometric correction loop

**Module:** `pipeline/reconciliation.py::geometric_correction_loop`

Wraps Stage 10.6 in a bounded build→measure→correct→rebuild loop (cap 3). A
**systematic** transform error (origin offset / axis swap / uniform scale — ≥2
features share one error) is corrected once and pre-compensated on every affected
step; a one-off MISPLACED/EDGE_NOT_BROKEN re-emits the single step with the
resolver-derived position (the drawing is truth, never the measured value);
MISSING/EXTRA/unresolvable WRONG_SIZE are flagged, never fabricated. Terminates on
all-PASS, the cap, no-applicable-correction, or oscillation. COM builder injected
for unit testing.

**Output:** `<Part>_geometric_loop_report.json`; `geometric_loop_iteration` in
`lessons_learned.jsonl` per pass.

---

## Stage 10.8 — Human-assist escalation

**Module:** `pipeline/human_assist.py`

The exit ramp after all four automated stages fail (resolver ladder → TYP/derivation
→ Phase B correction loop → Phase D method experiments). Each eligible item becomes
a narrow question: one-sentence `question_text`, pre-populated `candidates` with
basis, tight `region_crop`, and an always-populated `default_if_unanswered`.
Capped (default 3), prioritized by leverage. **Never blocks:** a pending question is
the overlay `NEEDS_HUMAN_INPUT` whose default still ships; questions don't gate
READY. Answer feedback re-splices via the reconciliation splice-back — no paid
re-extraction.

**Output:** `<Part>_assist_queue.json`; webapp `GET/POST /api/parts/{session}/assist`.

---

## Stage 11 — Final gates

**Modules:** `pipeline/overview_check.py`, `pipeline/requirements_check.py`

Status-only (outputs still produced): `overview_check.py` re-examines the overview
drawing and diffs against the build (missing visible feature = CRITICAL);
`requirements_check.py` grades must-meet notes met/partial/unmet — an unmet line
gates READY.

**Output:** `<Part>_requirements.json`.

---

## Stage 12 — Engineering review

**Module:** `pipeline/engineering_review.py`

The single severity-ranked human report, regenerated after the COM build so skipped
features are included. **Read this first.**

**Output:** `<Part>_engineering_review.txt`.

---

## Stage 13 — Learning loop

**Module:** `pipeline/learning_loop.py`

At the end of every run, writes one plain-text failure report (gate reasons, MM
fails, Stage-1.5 conflicts, every engineering-review flag at every severity,
macro/build failures) ending with a paste-ready "FIXES FOR FABLE" brief.
Exception-safe.

**Output:** `Learning Loop/<Part>__<timestamp>.txt`; one line in
`Learning Loop/INDEX.md`.

---

## Web UI — Tab-3 Visual Summary

**Modules:** `pipeline/summary_view.py`; `webapp/app.py`; `webapp/index.html`.

A pure presentation layer (no pipeline stage, no new computation) over artifacts
already on disk. Above the Run-Outputs dock, a collapsible band shows a part-header
strip (envelope, feature counts, severity flag counts, READY status, open-question
`?N` affix) plus two linked tables: **Extracted Features & Dimensions** (ID · Type ·
Size ⌀/W×H×D · Position · Basis · Qty · Status; a collapsed "Notes & references"
subsection) and **Build Plan** in build order (Step · Feature · Stage · Operation ·
Key values · Placement · Result = disposition ⊕ verification verdict).
`build_summary(output_dir)` holds all number formatting in one place (drawing-style
numbers, `⌀`, `(x,y)`, meters never surfaced, `—` for absent), discovers the
artifact prefix from disk, degrades gracefully on missing artifacts. Endpoint
`GET /api/parts/{session}/{part}/summary`. Rows expand to detail; feature id
cross-highlights across both tables; headers sort; narrow widths drop to four
columns; a `⎙ Print` view. The Three.js viewer applies **no** orientation flip
(loads the STL raw, centers + frames only) — orientation correctness lives in the
geometry, so the UI table, build plan, SolidWorks model, STL, and browser preview
all agree.

---

## Full output listing per part, `<output>/<Part>/`

```
<Part>.SLDPRT
<Part>.STL
<Part>_extraction.json                 (raw, never overwritten)
overview_analysis.json                 (Stage 1.5)
must_meet_spec.txt                     (operator input, persisted)
must_meet_constraints.json             (Stage 2.6)
<Part>_resolved_extraction.json        (Stage 2.5)
<Part>_verification_report.txt         (Stage 6)
<Part>_build_plan.json                 (Stage 6.5; steps + dispositions + reference_geometry + engineering_review)
<Part>_build_dispositions.json         (Stage 6.5)
macros/                                (Stage 7)
  00_setup.vba
  01a_reference_geometry.vba
  01_Fxxx_*.vba                        (base solid)
  02_Fxxx_slot_rect_cut.vba            (slot pairs, if present)
  03_Fxxx_slot_corner_fillet.vba
  04_Fxxx_*.vba                        (holes)
  05_fillets_chamfers.vba
  NN_Fxxx_MANUAL_*.vba                 (prohibited features)
  NN_Fxxx_SeedHoleCut / _reference_axis / _circular_pattern.vba  (circular trio)
  RECONCILE_pass<N>_*.vba              (only if Stage 10.5 spliced a recovery)
  ZZ_final_verify.vba
  ZZZ_export_stl.vba
  RUN_ALL.vba
  README.md
  logs/macro_result.json
prevalidation.stl / prevalidation_report.json / prevalidate.py   (Stage 8)
<Part>_model_check.txt                 (Stage 9)
constraint_verification.json           (Stage 10)
<Part>_reconciliation_report.json      (Stage 10.5)
<Part>_feature_verification.json       (Stage 10.6, when produced)
<Part>_geometric_loop_report.json      (Stage 10.7)
<Part>_assist_queue.json               (Stage 10.8)
<Part>_requirements.json               (Stage 11)
<Part>_engineering_review.txt          (Stage 12, read first)
<Part>_audit_report.json               (Stage 7 static audit)
.extraction_cache/ / .resolution_cache/
```

Plus one level up: `token_usage_log.txt`, `lessons_learned.jsonl`, and repo-root
`Learning Loop/<Part>__<timestamp>.txt` + `Learning Loop/INDEX.md`. Successful runs
are copied to `UI_Output/<Part>/` (gitignored) and
`~/Downloads/SolidWorksModel_Parts/<Part>/`.

## Test suite

**701 passing** as of HEAD. Notable suites: `test_coordinate_normalize.py` (23),
`test_macro_echo.py` (15), `test_summary_view.py` (29), `test_golden_macros.py`
(snapshot), `test_slot_cut.py`, `test_commit_mode.py`, `test_reconciliation.py`,
`test_phantom_reconciliation.py`, `test_multiview.py`. Live SolidWorks COM build is
the standard final check on a SolidWorks machine (not runnable headless).
