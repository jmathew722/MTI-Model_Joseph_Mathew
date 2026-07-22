# MTI 2D → 3D Pipeline — Two-Week Change Presentation

**Period covered:** 2026‑07‑08 → 2026‑07‑22 (the last two weeks)
**Repository:** `jmathew722/MTI-Model_Joseph_Mathew`
**At a glance:** ~84 commits, ~740 files touched, 45 test suites, 42 pipeline modules.
Active branches this period: `main`, `MTI_Finalized`, `MTI_Codex` (current), plus the
`feat/2d-to-3d-solidworks-pipeline` integration line and several experiment branches.

> This document has two halves. **Part A** explains how the pipeline works *today*,
> end to end. **Part B** is the chronological record of everything that changed in
> the last two weeks and why. Read Part A to understand the machine; read Part B to
> see how it got here.

---

## What this system is (one paragraph)

The MTI pipeline turns a **2D engineering drawing** (PDF / PNG / JPG / DWG / DXF /
eDrawings) into a **SolidWorks 2024 part** (`.sldprt` + `.stl`) plus a complete,
auditable paper trail. A vision LLM reads the drawing; a deterministic resolver
commits every ambiguity to a defensible number; a build sequencer orders the
features; and three independent builders (VBA macros, a headless CadQuery
pre‑validation, and a live SolidWorks COM build) construct the geometry. The
guiding principle throughout: **a complete approximate model is always the correct
outcome; an incomplete model is always the wrong outcome — resolve and flag, never
block or silently drop.**

---

# PART A — How the pipeline works (end to end)

The pipeline is a sequence of one‑module‑per‑stage steps, orchestrated by `main.py`
(single drawing) or `pipeline/batch.py` (a `--views-folder` of parts). The web UI
runs `main.py` as a subprocess, so the CLI is the single source of truth.

### Stage 0 — Image prep (`utils/image_prep.py`)
Normalizes and downscales the input images to a size the vision model handles well.

### Stage 1.2 — Tiled high‑res extraction, *escalation only* (`utils/tiled_extraction.py`)
Large‑format sheets whose thin line work dilutes to sub‑pixel at the standard raster
cap (the "image appears nearly blank" case) trigger a zoom pass: the vector PDF is
re‑rendered at escalating DPI until the median line width is legible, cut into
overlapping tiles, extracted per tile in sheet coordinates, and stitched by
anchor+value. Clean small drawings skip this and take the single‑shot path.

### Stage 1.5 — Holistic overview analysis (`overview_analysis.py`)
The **full, uncropped sheet** goes to the vision model with a *relational* prompt: how
many views, which features correspond across views (a circle in the front view that
matches full‑height hidden lines in the side view is a **through** bore, not blind),
the overall 3D shape, cross‑view conflicts (with severity + recommendation), symmetry,
global notes like "(6) HLS", and — added this period — a **dimension‑locations**
sentence saying *where* the governing dimensions live on the sheet. It never
re‑extracts dimensions; it owns cross‑view relationships (priority **tier 2**).

### Stage 2 — Extraction (`extractor.py`)
**One vision call per part** with a forced tool call against the Pydantic schema in
`schema.py`. **Specs‑first:** the operator's must‑meet specifications are injected into
the prompt so the model actively hunts for those features. Raw extraction JSON is
always saved; an on‑disk cache makes identical re‑runs free. Every positioned feature
can now carry **`anchors`** describing *what its position is measured from* (see
"dimensioning architecture" below). Token/USD cost is logged per stage.

### Stage 3 — Exact hole positions (`vector_extract/` + `hole_resolution.py`)
When a vector source exists, exact hole centers are read from the file itself (DXF/DWG
entities via ezdxf, vector‑PDF Bézier circles via PyMuPDF, HoughCircles raster
fallback). **Precedence rule:** vector geometry owns *position*, the vision callout
owns *semantics* (diameter/thread/depth); disagreement keeps both and flags CRITICAL.

### Stage 2.6 — Spec reconciliation (`must_meet.py`)
The operator's must‑meet text is parsed into structured `MM‑xxx` constraints (LLM with a
deterministic regex fallback). **Constraints are priority tier 0 — they override
vision‑extracted values on any conflict**; every conflict is logged, never dropped.

### Stage 2.5 — Ambiguity resolution (`resolver.py`) — the core design decision
**The pipeline never blocks on ambiguity.** Every unclear or under‑dimensioned value is
resolved to a numeric value chosen *from extracted candidates* (never fabricated) via a
deterministic tree — spec‑driven → arithmetic chain → geometric validity → conservative
geometry → last‑resort default — and tagged HIGH/MEDIUM/LOW/CRITICAL. In
**commit‑to‑extraction mode** (default), the pipeline builds *every* extracted feature
rather than excluding incomplete ones. New this period: the resolver also expands
patterns‑of‑slots into explicit slots, drops valueless finishing notes, and records the
canonical **coordinate frame** the part uses.

### Stage 6 / 6.5 — Verification + the canonical build sequencer (`validator.py`, `build_sequencer.py`)
Arithmetic/envelope verification (advisory by default). Then the **one deterministic
build‑order pass**: survivors are re‑ordered into a fixed **seven‑stage** sequence
(reference → base solid → additive bosses → profile cuts → holes → patterns →
chamfers/fillets → non‑geometric) with a stable within‑stage sort, so the macros,
`build_plan.json`, CadQuery pre‑validation, and the COM build all inherit **one byte‑
identical order**. Every feature ends in exactly one disposition: `BUILT`,
`BUILT_WITH_DERIVED_VALUE`, or `EXCLUDED_INCOMPLETE`.

### Stage 7 — Macro generation (`macro_generator.py` + `macro_audit.py`)
Numbered VBA macros (`00_setup` … `ZZZ_export_stl`, `RUN_ALL.vba`), each statically
audited before it is written (banned APIs fail generation). Reliability layers added
over the period: a **macro echo check** (every emitted literal must round‑trip to the
build plan for the *same* feature), **template‑based emission** (a primitive can only
reference one feature's data), **open‑edge overshoot**, **label/payload agreement**,
and a **notch‑orientation guard**. Two new outputs also emit here:
**overview‑word validation** (does the macro package agree with what Stage 1.5 said the
sheet shows?) and a **C# companion package** (`macros_csharp/`).

### Stage 8 — CadQuery pre‑validation (`cq_prevalidate.py`)
Builds the *same* geometry headlessly from `build_plan.json`, checks
watertightness/volume/hole counts against the MM constraints, and **aborts the
SolidWorks build** with the exact failing constraint if anything is wrong. This period
it learned to build slots (rounded profile), revolves, and counterbore/countersink
stacks — closing the gap where it was previously blind to those.

### Stage 9 — SolidWorks COM build (`solidworks_builder.py` + `model_validator.py`)
Windows‑only COM build of the `.sldprt`, STL export, and a mass/bbox check. Features are
named deterministically right after creation; per‑feature outcomes are written so a
failure surfaces as the exact feature, never a generic exit code. New this period: a
real **slot decomposition** path (rectangle + corner fillets, aligned to the actual
body), **countersink** geometry, and shared orientation‑preserving base sizing so the
COM build matches the VBA and CadQuery builds.

### Stages 10 – 10.8 — Verification, correction, and escalation
- **`constraint_verify.py`** — measures the built STL with trimesh and grades every MM
  constraint PASS/FAIL. A run with MM constraints is READY only when all pass.
- **`reconciliation.py` (10.5)** — diffs the build against the **raw** extraction
  (never the downstream artifacts), re‑runs *only* the resolver (no paid API) up to a
  cap to recover anything missing, and splices recoveries into the plan.
- **`feature_verify.py` (10.6)** — measures *every* planned feature (position, size,
  through/blind) and now also **anchor fidelity** (measured‑vs‑anchor, catching
  "right hole, measured from the wrong edge").
- **`reconciliation.geometric_correction_loop` (10.7)** — bounded build→measure→correct
  loop; corrects systematic transforms once, re‑emits one‑off misplacements from the
  *resolver‑derived* position, never fabricates.
- **`human_assist.py` (10.8)** — the exit ramp: only after the automated ladder is
  exhausted does a feature become a narrow question with a pre‑populated default that
  still ships. Never blocks READY.

### Stages 11 – 13 — Final gates, review, and the learning loop
`overview_check.py` re‑examines the overview drawing alone; `requirements_check.py`
grades the operator notes; `engineering_review.py` writes the single severity‑ranked
human report; and `learning_loop.py` writes a plain‑text failure report per run to
`Learning Loop/` with a paste‑ready "fixes" brief — the training signal that drove most
of this period's fixes.

### The provider layer (`ai_provider.py`) — new this period
Every LLM call site talks through one uniform contract. `AI_PROVIDER=openai` swaps the
model to **GPT‑5.6** behind an adapter that preserves that contract exactly; unset keeps
Claude. This is the `MTI_Codex` branch's headline capability.

---

# PART B — Everything that changed, in order

The two weeks fall into five arcs. Each subsection lists the load‑bearing commits.

## Arc 1 (Jul 8–9) — Marked‑view intake, then the pivot to whole‑sheet reading
Early in the window the UI had a per‑view **crop + color‑group markup** workflow, a
locked `(0,0)` origin datum, and GD&T datum points feeding extraction.

- `feat(ui): reference-region markup` · `move all preprocessing markup to Sheet 1` ·
  `markup workspace layout` · `marked-view feeds Claude extraction for hole placement`
- `locked (0,0) origin datum + "Add marked drawing to Part Setup"`
- `GD&T datum points (incl. datum holes) + Tab-3 correction re-run`
- **The pivot:** `refactor(ui): model-driven whole-sheet analysis; remove Tab-1
  crop+markup` — the model now reads every view from the sheet itself, no human
  markup preprocessing. This is why the current UI has no region‑markup tools.

## Arc 2 (Jul 9–10) — The learning loop and the first big fix cycles
The **iterative learning loop** landed and immediately paid off.

- `feat(pipeline): iterative learning loop — per-run failure reports` (+ the
  `Save all flags to Learning Loop` button and the `push-learning-loop.ps1` backup).
- `learning-loop 2026-07-09 cycle` (batches 1 & 2): count reconciliation, TYP handling,
  a missing‑dimension gate, position/profile/illegible routing, overview taxonomy.
- `learning-loop 2026-07-10 cycle — 10 generalizing fixes + 2 regressions closed`.
- Structural upgrades: **circular geometry is always circle+extrude, never a revolved
  rectangle**; **Stage 10.5 reconciliation pass + full repo audit + silent‑drop fixes**;
  the **canonical seven‑stage build sequencer**; and the **closed‑loop geometric
  accuracy layer (Phases A–D)** with robust sketch entry that fixed multi‑hole builds.

## Arc 3 (Jul 11–12) — Reliability workstreams and Stage‑7 hardening
- **Workstream 1** `deferred feature retry queue` — a hard feature failure is
  quarantined and retried after the solid is complete, never a silent skip.
- **Workstream 2** `tiled high-res extraction` — the zoom‑pass escalation.
- **Workstream 3** `reference-geometry datum skeleton` — named `REF_DATUM_*` /
  `REF_AXIS_*` / `REF_PT_*` reference geometry built before any feature.
- **Human‑assist escalation layer** — stop silent re‑looping; ask a narrow question.
- **Canonical slot / U‑notch decomposition rule** — a slot is *never* one arc sketch;
  it is a mandatory rectangle cut + deferred corner fillets.
- **Commit‑to‑extraction mode** — build every extracted feature; no placeholders.
- **Per‑instance hole placement + datum chaining** (A001271E) + macro dedup.
- **Stage‑7 hardening** — the macro **echo check**, template‑based emission, and the
  open‑edge / label / notch emission invariants.
- **Pipeline Explainer** — a dual‑provider (local qwen + Claude API) read‑only chat over
  one run's artifacts, plus the **Tab‑3 visual‑summary** feature/build‑plan tables.

## Arc 4 (Jul 13–17) — Coordinate normalization, palette, and the dimensioning overhaul
- `centralized coordinate normalization` (`coordinate_normalize.py`) — the ONE place
  semantic drawing anchors become global CAD coordinates; killed the 158‑C top/bottom
  notch bug (`y = parent_height − depth` lives here).
- UI restyle to the **Draw2Part "Paper Room" palette** (deep‑cream engineering‑paper
  theme) with the Explainer and run‑output table restored on top of it.
- `chore: reorganize repository layout` — the seven test‑drawing sets grouped under
  `test_drawings/`, loose docs into `docs/`, the script into `scripts/`, demo
  extractions into `samples/`, and generated artifacts un‑tracked.
- `feat(macros): overview-word macro validation + C# companion output` — the package is
  validated against Stage 1.5's words, and a byte‑equivalent **C# build program**
  (`macros_csharp/`) is emitted alongside the VBA.
- `feat(overview): dimension_locations sentence` in Stage 1.5.
- **The dimensioning‑architecture overhaul** (`position_solver.py`): every positioned
  feature carries a `PositionAnchor` (chain / baseline / ordinate / coordinate /
  polar‑BSC / datum‑frame) and absolute coordinates become a *derived* output of one
  solver — the drawing's own dimensioning scheme now survives to the macro. Correction
  propagation (`movers`), datum‑frame selection, and anchor‑fidelity verification came
  with it. `main` was fast‑forwarded to include all of the above via
  `Merge branch 'MTI_Finalized'`.

## Arc 5 (Jul 20–22) — The `MTI_Codex` branch: OpenAI, geometry correctness, human verification
- `feat(MTI_Codex): OpenAI (GPT-5.6) as a gated alternative provider` — `ai_provider.py`
  swaps the whole pipeline to GPT‑5.6 behind `AI_PROVIDER=openai`, preserving the
  Anthropic‑shaped call contract so no call site changed; accurate GPT‑5.6 pricing added
  to the token/cost ledger. Default stays Claude, so `main` is untouched.
- `fix(ui): masthead names the active engine` — shows "ChatGPT (GPT‑5.6)" on this branch.
- **`feat(geometry): correct slot/U-notch modeling across all builders + audit fixes`** —
  the big correctness pass driven by a full geometry audit. All three builders now build
  the *same* shape for every feature. Highlights: CadQuery now builds slots (the exact
  rounded profile, with each corner arc centre proven `(r, r)` inset from the sharp
  corner), revolves, and cbore/csk; the COM builder routes slots through the
  decomposition and aligns to the real body; a shared **orientation‑preserving base
  sizer** fixed parts that built rotated 90°; **pattern‑of‑slot expansion** turns a
  patterned notch into explicit per‑instance slots; the **position solver is now
  authoritative** (was advisory); **countersinks build real conical geometry**;
  **partial‑arc bolt patterns** are supported; revolve profiles are validated; a
  valueless "break all sharp edges" note no longer gates READY; METHODS.md reconciled.
  Verified end‑to‑end on **16247** (a flat bar with two `.531 R` U‑notches 16.5 apart):
  both notches build as rectangle + rounded corners on every path, READY 4/4.
- **`feat(ui): Human Verification tab (SHEET 3)`** — a dedicated tab (Run Outputs moved
  to SHEET 4) that turns each engineering flag into a concise, drawing‑specific
  confirmation question (phrased by GPT‑5.6, cached, deterministic fallback) with a
  fill‑in box. The answers compile into one must‑meet CORRECTION block and feed back
  through specs‑first extraction + Stage 2.5 on re‑run — clearing flags at the source so
  future runs carry fewer of them.

---

## The 16247 case study — why the geometry work matters

16247 is a flat bar, `2.00 × 19.25 × .280`, with **two U‑notches** cut into one edge,
each ending in a `.531 R` radius, `16.500` apart. It is the exact shape the "rectangle
cut then a rounded corner on that rectangle" concern was about, and it exercised nearly
every weakness at once:

- **Before:** the base built rotated 90° (the sizer picked "two largest" ignoring
  orientation), the lower notch landed *outside* the solid on the COM path, and the
  upper notch — extracted as a *linear pattern* of the first — crashed the pattern
  feature. Result: NOT READY, missing geometry.
- **After:** the base builds `2 × 19.25` correctly; both notches build as a mandatory
  rectangle cut plus tangent `.531 R` corner arcs on the VBA, CadQuery, and COM paths;
  the pattern is expanded into an explicit second slot; reconciliation is READY 4/4; the
  `.sldprt` and `.stl` are produced. The one honest caveat: the COM corner *fillet*
  defers to a warning (sharp‑cornered but correctly positioned) when SolidWorks
  edge‑selection can't isolate the corner edges — the VBA and CadQuery paths apply the
  rounding, and the slot rectangle is always correct.

---

## Quality bar

- **Tests:** 45 suites; the full run is green at **804 passing** on `MTI_Codex` (779 on
  `main` before the OpenAI/geometry/verification work). New suites this period include
  the position solver, coordinate normalization, macro echo, overview‑macro validation,
  C# macro output, slot geometry, the AI‑provider adapter, and human‑verification
  questions.
- **Golden macros:** a byte‑exact snapshot of the generated VBA package guards against
  unintended output drift; every change above kept it byte‑identical unless the change
  was deliberately to macro text.
- **The learning loop** writes a failure report for every run, and most of this period's
  fixes are traceable to a specific loop report — the pipeline improves run over run.
- **Provider isolation:** the OpenAI swap is gated by an env var, so `main` and every
  other branch stay on Claude with zero behavior change; the branch can be merged or
  reverted cleanly.

---

## Where things stand (2026‑07‑22)

- **`main`** — the Claude‑powered pipeline with the full reliability + dimensioning
  architecture (through the Jul‑20 merge).
- **`MTI_Finalized`** — the same, and the branch most of the reliability work landed on.
- **`MTI_Codex`** *(current)* — everything on `main` **plus** the GPT‑5.6 provider, the
  cross‑builder geometry‑correctness pass, and the Human Verification tab. The web UI on
  this branch runs on GPT‑5.6 and shows the new SHEET 3.

The pipeline today reads a drawing, commits every ambiguity to a defensible number,
builds the same geometry three independent ways with a pre‑flight abort, verifies the
result against the drawing *and* its own dimensioning scheme, and — when something is
genuinely uncertain — asks the operator one short question instead of guessing silently.
