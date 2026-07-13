# MTI 2D→3D Pipeline — Section by Section

This is a stage-by-stage walkthrough of the pipeline in `pipeline/`, orchestrated by
`main.py` (single-drawing / `--views-folder` entry) and `pipeline/batch.py`
(`process_drawing_data`, used by the web UI and `run_models.py`). Every stage is
additive: a missing input/key skips the stage without breaking the run. The
guiding rule throughout — *a complete approximate model is always the correct
outcome; an incomplete model is always the wrong outcome* — resolve and flag,
never block or silently drop.

---

## Stage 0 — Input prep

**Module:** `pipeline/utils/image_prep.py`

Normalizes and downscales whatever came in (PDF page render, PNG/JPG, DWG/DXF
converted via `/api/convert-dwg` in the web UI) to a raster the vision model can
consume, capped by `MAX_IMAGE_LONG_EDGE`. Multi-view input (`--views-folder`)
treats each part as a folder of per-view images; view role comes from filename
keywords (`front`/`side`/`top`…) or a leading `01`–`05`; a file named
`full`/`overview`/`isometric` or matching the folder name is overview context
only (not built as a plane). A front view + one other orthographic view is
required.

### Stage 1.2 (escalation only) — Tiled high-res extraction

**Module:** `pipeline/utils/tiled_extraction.py`

Not the default path — fires only when `should_tile()` trips on: the
"appears nearly blank" heuristic, ink density < 0.5%, extraction confidence <
0.6, > 25% of dimensions flagged unclear, or a C-size+ page at the ≤2576px cap.
`adaptive_render()` re-renders the vector PDF at escalating DPI (300→600→900)
until median line width ≥ 2.5px; `make_tiles()` cuts ~1500px tiles at 22%
overlap; each content-bearing tile is extracted in sheet coordinates; `stitch()`
merges readings by anchor+value (conflicts become candidate `possible_values`
for Stage 2.5); `datum_anchor()` re-expresses positions from the datum. Tile
cost logs separately (`extraction_tiled`); tiles cache by (page hash, DPI, grid).

**Output:** none directly (folds into the extraction call's input images).

---

## Stage 1.5 — Holistic overview analysis

**Module:** `pipeline/overview_analysis.py`

The FULL uncropped sheet (`00_full.jpg`) goes to Claude Sonnet 5 with a
RELATIONAL prompt — it does **not** re-extract dimensions. It reports: views
present on the sheet, cross-view feature correspondences (e.g. through-vs-blind
agreement), the overall 3D shape, cross-view conflicts with severity +
recommendation, symmetry, and global notes (e.g. "(6) HLS" with a
`resolved_count`). Call is pinned to `temperature=0`.

Feeds Stage 2.5 as **priority tier 2** (tier 0 = must-meet specs, tier 1 =
per-view extraction owns dimension values, tier 2 = overview owns cross-view
relationships). A deterministic callout-count cross-check (e.g. 5 visible holes
vs. a "(6) HLS" note) becomes a flag with `source: overview_analysis`.

**Output:** `<Part>_overview_analysis.json`. Purely additive — no key or a
failure skips the stage with no pipeline change; `--from-json` reuses a sibling
file. Shown in the UI as the collapsible "Overview Analysis" panel.

---

## Stage 2 — Extraction

**Module:** `pipeline/extractor.py`, schema in `pipeline/schema.py`

One Claude Vision call per part — all views labeled, forced tool call against a
Pydantic v2 schema, `temperature=0`. **Specs-first:** the operator's must-meet
text is injected into the prompt so the model actively looks for those features
from the start; the spec text is part of the cache key (changed specs force a
fresh extraction). Balloon/BOM guardrails in the prompt prevent a callout note
from being synthesized as a phantom feature, and explicitly forbid duplicating
a hole feature as a separate pattern feature.

Raw extraction JSON is always saved; an on-disk cache
(`<output>/.extraction_cache/`) makes identical re-runs free. Token/USD costs
append to `token_usage_log.txt` via `usage_log.py`.

**Output:** `<Part>_extraction.json` — the permanent ground truth, never
overwritten by any later stage.

---

## Stage 3 — Vector hole resolution

**Modules:** `pipeline/vector_extract/`, `pipeline/hole_resolution.py`

Pulls exact hole positions from the original vector file — DXF/DWG entities via
`ezdxf`, vector-PDF Bézier circles via PyMuPDF, HoughCircles raster fallback for
anything else. **Precedence rule:** vector geometry owns *position*, the vision
callout owns *semantics* (diameter/thread/depth) — a disagreement keeps both
values and flags CRITICAL. Every hole carries `position_source` +
`position_confidence`.

**Output:** folds into the extraction/resolution JSON (no standalone file).

---

## Stage 2.6 — Spec reconciliation (must-meet parsing)

**Module:** `pipeline/must_meet.py`

The operator's must-meet text (`must_meet_spec.txt`, the amber box in the UI;
legacy `notes.txt` still works) is parsed into structured `MM-xxx` constraints
via a dedicated Claude call with a deterministic regex fallback (works with no
API key). **Constraints are priority tier 0** — they override vision-extracted
values on any conflict; every conflict is appended to `lessons_learned.jsonl`
(`resolution: spec_override`), never silently dropped. Missing geometry
parameters are derived from extraction where possible (e.g. bolt-circle radius
fit from hole centroid; radii disagreeing > 0.005 in = CRITICAL). Exception: a
spec hole COUNT that contradicts explicitly dimensioned drawing positions keeps
the drawing's geometry, flags CRITICAL, and lets the MM check fail with
measured-vs-required (`spec_vs_drawing_disagreement`).

**Output:** `must_meet_constraints.json`; `must_meet` block inside
`resolved_extraction.json`.

---

## Stage 2.5 — Resolution (the core design decision)

**Module:** `pipeline/resolver.py`

**The pipeline never blocks on ambiguity.** Every unclear dimension gets a
numeric `resolved_value` chosen from extracted candidates (never fabricated)
via a deterministic tree: spec-driven → arithmetic chain → geometric validity →
conservative geometry → last-resort default. Each resolution is tagged
HIGH/MEDIUM/LOW/CRITICAL.

**Specs-first:** an operator must-meet value that clarifies an ambiguous
reading takes precedence (Step 0, `assumption_basis="spec_driven"`).

**Commit-to-extraction mode** (`commit_mode`, default ON): no human in the loop
— the pipeline commits to the extraction and builds every extracted feature.
Positional dimensions are consumed before any escalation. A step/notch cut
missing length+width has its rectangle derived from the outer-profile envelope
minus its partial anchor dims (`basis="profile_delta"`); a hole missing a
diameter inherits the most-common sibling diameter (`_sibling_diameter`); a
genuinely undimensioned size/position commits a declared-basis conservative
value (`committed_conservative`, never `[0,0]`), applied, built, and
CRITICAL-flagged with the value + basis. `commit_mode=False` restores the old
exclude/review behavior for comparison. The only remaining hard failure is no
closed outer profile.

A dimension with exactly one clean reading (not flagged unclear/ambiguous)
resolves unconditionally at `confidence=1.0, basis="extracted_verbatim"` —
sub-1.0 confidence is reserved for dimensions with ≥2 genuine candidates, and
always carries the candidate list + deciding rule. `resolve_extraction` is a
pure function (stable sort order, no randomness); `pipeline/resolution_cache.py`
proves this by hashing (extraction JSON, resolver version) and cross-checking a
same-key result against cache — a mismatch logs a determinism-violation error.

Per-instance hole placement + datum chaining: `_classify_hole_groups`
classifies every hole group as `placement: pattern` (only with hard evidence —
a bolt-circle or uniform pitch with a single owning feature) or `placement:
individual` (default bias — an individually-misbuilt-as-pattern group is wrong
geometry, while a pattern built as individuals is merely more lines), recording
a per-instance `position_basis` datum chain.

**Output:** `<Part>_resolved_extraction.json`.

---

## Stage 6 — Validation

**Module:** `pipeline/validator.py`

Arithmetic/envelope verification. Advisory by default; `--strict-gate` makes
failures blocking.

**Output:** `<Part>_verification_report.txt`.

---

## Stage 6.5 — Canonical build sequencer

**Module:** `pipeline/build_sequencer.py`

The ONE deterministic build-order pass. Re-orders the completeness-gate
survivors into a fixed **seven-stage** sequence:

0. reference geometry
1. base solid (largest closed profile)
2. additive bosses
3. profile subtractions
4. holes: plain → cbore/csk → tapped
5. patterns
6. chamfers → fillets
7. non-geometric

With a stable within-stage sort keyed to feature id → byte-identical
`build_order` across runs. Because the same `model` object flows onward,
macros, `build_plan.json`, CadQuery pre-validation, and the COM build all
inherit this order.

**No type-based omission:** every feature ends in one disposition state —
`BUILT`, `BUILT_WITH_DERIVED_VALUE` (resolver-inferred value),
`EXCLUDED_INCOMPLETE` (missing parameter named; in commit-mode reached only by
non-committable edge/pattern treatments), or the overlay `NEEDS_HUMAN_INPUT`.

**Output:** `<Part>_build_dispositions.json` (this is the file open in your
IDE), plus a `dispositions` block inside `<Part>_build_plan.json`.

---

## Stage 7 — Macro generation

**Modules:** `pipeline/macro_generator.py`, `pipeline/macro_audit.py`,
`pipeline/slot_cut.py`, `pipeline/reference_geometry.py`,
`pipeline/methods_config.py`

Turns the ordered build plan into numbered VBA macros
(`00_setup` … `ZZZ_export_stl`, `RUN_ALL.vba`); prohibited features
(loft/sweep/shell/etc.) become `NN_Fxxx_MANUAL_*.vba` steps; every macro is
statically audited before writing (banned APIs fail generation).

*Full breakdown in `MACRO_GENERATION_STAGE7.md`.*

**Output:** `macros/` folder (numbered `.vba` files, `RUN_ALL.vba`,
`README.md`, `logs/macro_result.json`), plus the `build_plan.json`'s
`reference_geometry[]` block.

---

## Stage 8 — CadQuery pre-validation

**Module:** `pipeline/cq_prevalidate.py`

Headlessly rebuilds the SAME geometry from `build_plan.json` (single shared
source of truth with the VBA/COM path — circular patterns via
`.polarArray(radius, seedAngle, 360, count)` + `cutThruAll`), then checks
watertightness/volume/hole counts against the MM constraints. **A failed check
aborts the SolidWorks build** and surfaces the exact constraint
(`MM-001 FAILED: …`). Graceful no-op when `cadquery` isn't installed.

**Output:** `prevalidation.stl`, `prevalidation_report.json`, a per-run
`prevalidate.py` script.

---

## Stage 9 — SolidWorks COM build

**Modules:** `pipeline/solidworks_builder.py`, `pipeline/model_validator.py`

Windows-only COM build of `.sldprt` + STL export + mass/bbox check. Same
circular-pattern trio as the VBA path. Features are renamed deterministically
right after creation; per-feature outcomes are written to `macro_result.json`
so a failure surfaces as the exact feature, never a generic exit code. Optional
`HoleWizard5` path is opt-in only (`MTI_ENABLE_HOLE_WIZARD=1`, default OFF —
SolidWorks 2024 returns `None` on a clean part, so the proven sketch-cut path
stays default). Everything upstream of this stage runs on any OS.

**Output:** `<Part>.SLDPRT`, `<Part>.STL`, `<Part>_model_check.txt`.

---

## Stage 10 — Post-build must-meet verification

**Module:** `pipeline/constraint_verify.py`

The built STL is measured with `trimesh` (cross-section circle fitting;
through-all = the hole appears near both faces) and every MM constraint is
graded PASS/FAIL with measured vs required. **A run with MM constraints is only
READY when every constraint passes**; each failure is appended to
`lessons_learned.jsonl` with the responsible VBA snippet.

**Output:** `constraint_verification.json`.

---

## Stage 10.5 — Reconciliation pass

**Module:** `pipeline/reconciliation.py`

The pipeline's own closing check of its output against the **original raw**
`_extraction.json` (never the resolved/downstream artifacts, which could
themselves hide the bug). Builds a ground-truth checklist (every feature id +
expected instance count) and diffs it against the build-sequencer's
disposition table + `build_plan.json`'s actual instance positions. A justified
`skipped_prohibited` entry is accepted; anything else missing/short is named
exactly.

On a gap, re-runs **only** `resolve_extraction` (never the extractor — no paid
API call) with every requirements/overview-analysis signal freshly reloaded,
up to `max_passes` (default 3); since the resolver is pure, a pass recovering
nothing new stops the loop immediately. A recovered feature is spliced into the
existing `build_plan.json` and a new `RECONCILE_pass<N>_*.vba` macro is added
(no existing file renumbered/touched) — the already-built `.sldprt` is not
hot-patched; a full rebuild is needed, noted in the report.

**Output:** `<Part>_reconciliation_report.json`
(`checklist_total`/`confirmed_built`/`loop_passes_used`/`unresolved[]`/
`splices_applied[]`/`final_status: READY | READY_WITH_OPEN_ITEMS`). Any
unresolved item folds into the engineering review (CRITICAL,
`source: reconciliation`) and gates the run's binary READY status (exit code
8). Wired into both `batch.py` and `main.py`'s `--engine vba` path. Skipped
(not an error) when `--no-resolve` was used.

---

## Stage 10.6 — Per-feature geometric verification

**Module:** `pipeline/feature_verify.py`

Where Stage 10 grades the built STL against operator MM constraints only, this
measures **every** planned feature against `build_plan.json` — each hole's
position + diameter + through/blind, each profile cut/notch's location
(material-absence probe), each slot's obround, and the base envelope (+ a
COM-vs-CadQuery volume cross-check). Every feature ends with one
classification: `OK` / `MISSING` / `MISPLACED` (measured position reported) /
`WRONG_SIZE` (measured size reported) / `EXTRA` / `UNMEASURABLE` (always with a
stated reason). Reuses the cross-section machinery from `constraint_verify.py`.

**Output:** `<Part>_feature_verification.json`.

---

## Stage 10.7 — Geometric correction loop

**Module:** `pipeline/reconciliation.py::geometric_correction_loop`

Wraps Stage 10.6 in a bounded build→measure→correct→rebuild loop (cap 3).
Correction policy by class: a **systematic** transform error (origin offset /
axis swap / uniform scale — detected only when ≥2 features share one
consistent error) is corrected once and pre-compensated on every affected step;
a one-off `MISPLACED` re-emits the single step with the resolver-derived
position (the drawing is truth, never the measured value);
`MISSING`/`EXTRA`/unresolvable `WRONG_SIZE` are flagged, never fabricated.
Terminates on all-PASS (READY), the cap, no-applicable-correction, or
oscillation (a previously-PASS feature regressing → stop). The COM builder is
injected so the loop is fully unit-tested without SolidWorks.

**Output:** `<Part>_geometric_loop_report.json`; appends a
`geometric_loop_iteration` entry to `lessons_learned.jsonl` each pass.

---

## Stage 10.8 — Human-assist escalation

**Module:** `pipeline/human_assist.py`

The exit ramp when the automated ladder is genuinely exhausted. A
feature/dimension becomes a question ONLY after all four automated stages
fail: resolver plausibility ladder → TYP/derivation → Phase B correction loop
(3-pass cap) → Phase D method experiments (chronic construction kinds only).
Each eligible item becomes a narrow question object: one-sentence
`question_text`, pre-populated `candidates` with basis, a tight `region_crop`,
and an always-populated `default_if_unanswered`. Capped (default 3),
prioritized by leverage.

**Never blocks:** a pending question is a new flagged disposition overlay
`NEEDS_HUMAN_INPUT` (additive; geometric state unchanged) whose default still
ships — the part still produces its complete approximate model and usual
READY status; questions do not gate READY.

**Answer feedback:** a human answer feeds back as the resolver's
highest-priority candidate (`assumption_basis="human_provided"`) and re-splices
the affected feature via the exact reconciliation splice-back — no paid
re-extraction.

**Output:** `<Part>_assist_queue.json`; entries append to
`lessons_learned.jsonl`. Webapp exposes `GET/POST /api/parts/{session}/assist`.

---

## Stage 11 — Final gates

**Modules:** `pipeline/overview_check.py`, `pipeline/requirements_check.py`

Status-only checks (outputs are still produced regardless):
`overview_check.py` re-examines the part's overview drawing alone and diffs it
against the build (a missing visible feature = CRITICAL); `requirements_check.py`
grades operator must-meet notes met/partial/unmet — an unmet line gates READY.

**Output:** `<Part>_requirements.json`.

---

## Stage 12 — Engineering review

**Module:** `pipeline/engineering_review.py`

The single severity-ranked human report, regenerated after the COM build so
skipped features are included. **Read this first.**

**Output:** `<Part>_engineering_review.txt`.

---

## Stage 13 — Learning loop

**Module:** `pipeline/learning_loop.py`

At the end of every run, reads the run's artifacts and writes one plain-text
failure report (gate reasons, MM constraint fails, Stage-1.5 conflicts, every
engineering-review flag at every severity, macro/build failures) ending with a
paste-ready "FIXES FOR FABLE" brief naming the suspected code area per failure.
Exception-safe — never breaks a run.

**Output:** `Learning Loop/<Part>__<timestamp>.txt`; one line appended to
`Learning Loop/INDEX.md` per run.

---

## Also produced

- `<Part>_audit_report.json` — macro static-audit results (banned-API check,
  Stage 7).
- Successful runs are copied to `UI_Output/<Part>/` (gitignored) and
  `~/Downloads/SolidWorksModel_Parts/<Part>/`.
- `token_usage_log.txt` / `chat_usage_log.jsonl` — cost ledgers.
- `lessons_learned.jsonl` (output-tree root) — accumulates spec-override
  conflicts, constraint failures, and correction-loop iterations across runs.

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
<Part>_build_plan.json                 (Stage 6.5)
<Part>_build_dispositions.json         (Stage 6.5)
macros/                                (Stage 7)
  00_setup.vba
  01a_reference_geometry.vba
  01_Fxxx_*.vba                        (base solid)
  02_Fxxx_slot_rect_cut.vba            (slot pairs, if present)
  03_Fxxx_slot_corner_fillet.vba
  04_Fxxx_*.vba                        (holes)
  05_fillets_chamfers.vba
  NN_Fxxx_MANUAL_*.vba                 (prohibited features, if any)
  RECONCILE_pass<N>_*.vba              (only if Stage 10.5 spliced a recovery)
  ZZ_final_verify.vba
  ZZZ_export_stl.vba
  RUN_ALL.vba
  README.md
  logs/macro_result.json
prevalidation.stl                      (Stage 8)
prevalidation_report.json
prevalidate.py
<Part>_model_check.txt                 (Stage 9)
constraint_verification.json           (Stage 10)
<Part>_reconciliation_report.json      (Stage 10.5)
<Part>_feature_verification.json       (Stage 10.6)
<Part>_geometric_loop_report.json      (Stage 10.7)
<Part>_assist_queue.json               (Stage 10.8)
<Part>_requirements.json               (Stage 11)
<Part>_engineering_review.txt          (Stage 12, read first)
<Part>_audit_report.json
```

Plus, one level up from `<output>/<Part>/`: `token_usage_log.txt`,
`lessons_learned.jsonl`, and repo-root `Learning Loop/<Part>__<timestamp>.txt`
+ `Learning Loop/INDEX.md` (Stage 13).
