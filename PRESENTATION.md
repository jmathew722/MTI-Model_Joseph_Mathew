# MTI 2D→3D SolidWorks Pipeline

### From a 2D engineering drawing to a verified 3D SolidWorks part — automatically, traceably, and never silently wrong

---

## 1. What this system does

The pipeline converts **2D engineering drawings** (PDF, PNG/JPG scans, DWG/DXF, eDrawings) into
**SolidWorks 2024 parts**: a real `.SLDPRT`, an `.STL` for instant 3D preview, a complete
**VBA macro package** that rebuilds the part on any SolidWorks machine, and a plain-English
**engineering review** of every assumption made along the way.

**Guiding principle:**

> *A complete approximate model is always the correct outcome; an incomplete model is always the wrong outcome.*

The pipeline never blocks on an ambiguous drawing and never silently drops a feature. Every
unclear value is resolved to a defensible number, flagged with a severity tier, and handed to a
human with a specific recommendation.

---

## 2. The web application — one screen, four sheets

The UI (`FastAPI` + a single-page front end, styled as a drafting-room "blueprint" workspace)
is organized as **four tabs, modeled on drawing sheets**. Work flows left to right:
**crop → set up → run → inspect**.

The header shows the extraction engine (Claude Sonnet 5), the deliverables
(`.SLDPRT · .STL · VBA`), and a live **API-status pill** (key present → live extraction;
no key → demo mode from saved extractions, zero cost).

### SHEET 1 · Drawing Crop & Preprocessing Markup

The full drawing intake surface, with two modes on a top mode bar:

- **✂ Crop views** — load a multi-view drawing sheet and **crop each orthographic view**
  (front, side, top, …) into its own image. Crops are queued and pulled into Sheet 2 with
  one click — no file juggling. The uncropped sheet itself is preserved as the
  **Full Overview View**: it powers the Sheet-2 drawing viewer, the Stage 1.5 holistic
  analysis, and the post-build overview cross-verification.
- **✎ Mark regions** — the human preprocessing markup layer: drag colored highlight boxes
  over ambiguous regions (a hole's dimension callout, its X- and Y-offset dimension lines,
  its center) *before* extraction runs. **Color = feature group** (15-color palette); each
  box can carry a role tag (center / x-dimension / y-dimension / tolerance / other) and a
  transcribed value ("2.500 ± .005"). Boxes are resizable/deletable, stored in normalized
  0–1 coordinates, persisted per part as `reference_regions.json`, and the color groups
  surface **live** as a "Marked reference regions" subsection inside the Overview Analysis
  panel. **The markup feeds extraction:** the drawing + boxes are composited into
  `full_marked_view.jpg` and passed to Claude alongside a text legend of every feature group,
  so the model places holes per the operator's boxes — ground truth for correct hole
  placement, and a base for future OCR cross-checking / low-confidence fallback. A
  **⊕ Set origin** tool locks a **(0,0) datum at the bottom-left of the top view** (a cyan
  crosshair, drawn into the composite and stated in the extraction legend) so every model
  shares one consistent orientation relative to the drawing, and **＋ Add to Part Setup**
  pushes the composited marked drawing into Sheet 2 as a view you can assign and save.

### SHEET 2 · Part Setup & 3D Model

Where a part is assembled from inputs and where the finished 3D model is inspected.
Organized as three input groups across the top, and a two-panel viewer below.

**Inputs (top strip):**

1. **Add images** — upload a single drawing (PDF · JPG · DWG · DXF · eDrawings; DWG/DXF are
   converted server-side through an engine chain with caching), upload a **whole parts folder**
   (subfolders = parts, each with its views and spec `.txt`), or **pull the queued crops**
   from Sheet 1. Multi-sheet PDFs get a sheet picker.
2. **Assign view types** — every image gets a view type from a dropdown (Front, Back, Left,
   Right, Top, Bottom, **Full Overview View**), with rotation for sideways scans. Front plus
   one more orthographic view is required.
3. **Name & save** — the part name becomes the folder name; parts are saved in the exact
   layout the CLI consumes, so the UI and command line are always interchangeable.
   The amber **Must-Meet Specifications** box lives here: free-text, human-authored,
   *authoritative* requirements ("there must be 6 holes … done using a circular pattern …
   all holes through all"). Saved as `must_meet_spec.txt` with the part and enforced through
   the entire pipeline (see §4, tier 0).

**Viewer (bottom split):**

- **Left — Full Overview View**: the complete original drawing (zoom/pan/reset), so the
  reviewer always sees the source of truth next to the result. (Reference-region markup is
  drawn on Sheet 1's ✎ Mark regions mode — see Sheet 1 above.)
- **Right — 3D Model (STL)**: orbit/zoom/pan viewer with a **"Select Model" dropdown** —
  pick *any* part that has ever completed a run (this session or a prior one) and its model
  loads instantly, **no pipeline re-run required**; the left panel simultaneously switches to
  **that part's Full Overview View drawing**, so the source sheet and the model on screen
  always correspond. Shows the CadQuery **PRE-VALIDATED** preview (badged) until the real
  SolidWorks build replaces it.
- **Must-Meet checklist strip**: every MM-xxx constraint rendered ✓/✕ with
  *measured vs required* values — pre-validation results first, post-build verification once
  the SolidWorks model exists.

### SHEET 3 · Pipeline

The action surface — where runs are launched and watched live.

**Controls:** saved-part cards (thumbnail, view count, "ran" badge) → **▶ Pull & Run
Pipeline** for the selected part, **▶▶ Run All Parts** for the whole session, Cancel, and a
**Run demo** that replays saved extractions with no API key. A stage strip + progress bar
tracks the live run ([STAGE] markers streamed from the CLI, starting with the Stage 1.5
overview-analysis chip), with the full live console below.

**Overview Analysis panel** (collapsible): the model's Stage 1.5 *holistic read of the whole
sheet*, shown **live during the run** next to the stage strip — it auto-expands and populates
the moment `overview_analysis.json` is written. Overall 3D shape in one sentence, the views
detected, **cross-view conflicts inline** (red badge with the conflict count), cross-view
correspondences, global notes, and symmetry findings. The reviewer sees the model's
understanding of the part *while it is being built*, before drilling into per-view details.

### SHEET 4 · Run Outputs

The inspection surface — every artifact of **any** completed run, past or present.

A **"Select Run" dropdown** at the top lists every completed run across every part and every
session (`PartName — run @ timestamp`); switching it reloads all ten sub-tabs in place. The
run that just finished on Sheet 3 is **auto-selected** here the moment it completes. A
**"✕ Clear all models"** button (with confirmation) wipes every stored run output in one
action — both dropdowns empty, while saved part inputs and the delivered copies in
`UI_Output/` and `~/Downloads` are kept.

**Run-outputs dock** — one sub-tab per artifact, filled live as files are written (each tab
shows a ✓ once its file exists for the selected run):

| Sub-tab | What it shows |
|---|---|
| **Extraction JSON** | The raw Claude Vision extraction — never lost, re-runnable free via `--from-json` |
| **Resolved Extraction** | Stage 2.5 output: every dimension's `resolved_value`, assumption basis, flag tier, and **`resolved_by_tier`** (which priority tier decided it) |
| **Build Plan** | The self-contained `build_plan.json` — single source of truth for both the VBA macros and the CadQuery pre-validation |
| **Verification** | Arithmetic/envelope report + must-meet constraint story + final-check results |
| **Engineering Flags** | The severity-ranked human review (CRITICAL → LOW): every assumption, cross-view conflict, skipped feature, and requirement grade — What / Decision / Why / Affects |
| **Model Check** | Post-build mass/bounding-box validation (Windows + SolidWorks) |
| **VBA Macros** | Every numbered macro, viewable in-browser (`00_setup` … `ZZZ_export_stl`, `RUN_ALL.vba`) |
| **Token / Cost** | The API cost ledger — per-stage line items and running totals (see §5) |
| **Files** | Every output file with sizes and download links, plus delivery paths |
| **Console** | The pipeline log — live-streamed during a run, and **persisted with each run** so historical consoles replay too |

**Shared run history:** Sheet 2's model dropdown and Sheet 4's run dropdown read from **one
persistent, disk-backed run inventory** (`/api/run-history`) — a run that appears in one
always appears in the other, and both survive server restarts and new browser sessions.

**Delivery:** successful runs are copied to `UI_Output/<Part>/` and
`~/Downloads/SolidWorksModel_Parts/<Part>/` automatically — deliverables land in one
well-known place without hunting through run folders.

---

## 3. The pipeline — stage by stage (fully up to date)

The web UI runs the CLI (`main.py`) as a subprocess, so **the command line is the single
source of truth** — everything below applies identically to UI and CLI runs.

```
drawing/views ─► image prep
                    │
                    ▼
      ★ Stage 1.5 · Holistic Overview Analysis (full sheet, relational)
                    │
                    ▼
      Stage 2 · Per-view extraction (Claude Sonnet 5, specs-first)
                    │
      Stage 2.2 · Vector hole extraction (exact positions from PDF/DXF/DWG)
                    │
      Stage 2.6 · Spec Reconciliation (must-meet text → MM-xxx constraints)
                    │
                    ▼
      Stage 2.5 · Ambiguity Resolution (tier 0 → 1 → 2, never blocks)
                    │
      Stage 3 · Verification (advisory; --strict-gate to block)
                    │
      Stage 4 · VBA macro generation + static audit
                    │
      Stage 4.5 · CadQuery pre-validation (headless geometry check)
                    │
      Stage 5 · SolidWorks COM build → .SLDPRT + .STL
                    │
      Stage 6 · Post-build constraint verification (measure the real STL)
                    │
      Final checks · Overview cross-check + requirements grading → READY gate
                    │
                    ▼
      Engineering Review (the one report a human reads first)
```

**1 · Image prep** — normalize, orient, and downscale every input image.

**★ 1.5 · Holistic Overview Analysis** *(new)* — before any cropped view is extracted, the
**full uncropped sheet** goes to Claude Sonnet 5 with a dedicated *relational* prompt. It does
NOT re-extract dimensions; it answers what a single crop never can:

- How many views are on the sheet, and what is each one?
- Which features **correspond across views** — e.g. "the 3.880 DIA bore in the front view
  matches full-height hidden lines in the side view → a **through**-bore, not blind"?
- What is the **overall 3D shape** implied by combining all views?
- Is anything **visible in one view but absent or contradicted in another**? Each conflict is
  flagged with a severity and a concrete recommendation.
- Does the part have **symmetry** that should constrain patterning — verified against the
  dimensioning style, not assumed?
- Which **global notes** ("FINISH ALL OVER", "(6) HLS") govern what, including resolved counts?

Output: `overview_analysis.json` in every run folder, surfaced in the Sheet-2 panel, and fed
into Stage 2.5 as priority **tier 2**. The stage is purely additive — no API key or any
failure simply skips it — and its token cost is a **separate ledger line**
(`stage_1_5_overview_analysis`).

*Why it exists:* the A050211E failure class. A callout says **(6) HLS** but only 5 holes are
clearly rendered in the cropped front view. A per-view pass builds a 5-hole part silently;
the holistic pass flags it **CRITICAL** — "check for an occluded hole behind the title block"
— before a wrong part is ever built.

**2 · Per-view extraction** — one Claude Vision call per part covering all labeled views
(forced tool call, strict Pydantic schema). **Specs-first:** the operator's must-meet text is
injected into the prompt so the model actively hunts for those features from the start. Raw
extraction is always saved; an on-disk cache makes identical re-runs free.

**2.2 · Vector hole extraction** — exact hole positions read from the original vector file
(DXF/DWG entities, vector-PDF circle paths, Hough fallback for scans). Precedence rule:
**vector geometry owns position; the vision callout owns semantics** — disagreement keeps both
and flags CRITICAL.

**2.6 · Spec Reconciliation** — the must-meet text is parsed into structured **MM-xxx
constraints** (Claude call with a deterministic regex fallback — works without a key).
Constraints are **priority tier 0**: they override vision-extracted values on any conflict, and
every override is logged to `lessons_learned.jsonl`, never silently applied.

**2.5 · Ambiguity Resolution** — the "chief engineer" pass. Every unclear dimension gets a
numeric `resolved_value` chosen from *extracted candidates* (numbers are never invented) via a
deterministic decision tree, tagged HIGH / MEDIUM / LOW / CRITICAL. When sources disagree the
priority order is fixed — and now **recorded on every resolution and flag** (§4).

**3 · Verification** — arithmetic chain closure, envelope checks; advisory by default so an
imperfect drawing still yields a model (`--strict-gate` restores hard blocking).

**4 · VBA macro generation + audit** — numbered macros for every feature; unsupported features
become explicit MANUAL-step macros (never dropped); every macro is statically audited before it
is written. Circular patterns use a hardened three-macro sequence (seed hole → named reference
axis → version-pinned pattern call) with hard-stop error checks.

**4.5 · CadQuery pre-validation** — the same `build_plan.json` is built **headlessly** and
checked for watertightness, volume, and every MM constraint *before SolidWorks is touched*.
A failure surfaces the exact constraint ("MM-001 FAILED: …") and the pre-validated STL still
appears in the viewer.

**5 · SolidWorks COM build** — the real `.SLDPRT` + `.STL` (Windows + SolidWorks 2024).
Every feature outcome is written to `macro_result.json`, so a failure names the exact feature —
never a generic exit code. Everything upstream runs on any OS.

**6 · Post-build constraint verification** — the built STL is *measured* (cross-section circle
fitting, through-hole detection) and every MM constraint is graded **PASS/FAIL with measured vs
required** values. A run with constraints is only READY when every one passes.

**Final checks** — the overview drawing is re-examined against the built part (a visible
feature missing from the build = CRITICAL), and every must-meet line is graded
met / partial / unmet. These gate the READY status — **outputs are always still produced**.

**Engineering Review** — one severity-ranked report (`<Part>_engineering_review.txt`),
regenerated after the build, that folds in every assumption, Stage 1.5 conflict, skipped
feature, and requirement grade. This is the canonical "what needs human attention" surface.

---

## 4. The priority-tier model — why the answer is always traceable

When information sources disagree, the pipeline resolves by a **fixed, recorded priority**:

| Tier | Source | Authoritative on |
|---|---|---|
| **Tier 0** | Operator must-meet specifications | Everything — human-authored intent wins |
| **Tier 1** | Per-view extraction (+ vector geometry) | Individual dimension **values** and exact positions |
| **Tier 2** | Stage 1.5 holistic overview analysis | **Cross-view relationships** — through vs blind, symmetry, counts, whole-part consistency |

Every resolved dimension and every flag now carries **`resolved_by_tier`**
(`tier0_spec` / `tier1_per_view` / `tier2_overview`), so for any number in the final model
you can answer *"why this value?"* directly from the run artifacts. Tier 2 never overwrites a
tier-1 value — it adds flags a cropped view could never raise.

---

## 5. Cost transparency

Every paid API call lands in a per-output-folder ledger (`token_usage_log.txt` / `.jsonl`)
with token counts, cache savings, and USD cost — now **broken out per stage**:

- `extraction` — the per-view vision call
- `stage_1_5_overview_analysis` — the holistic full-sheet pass *(its own cost center)*
- `stage_2_6_spec_reconciliation` — must-meet parsing
- `final_overview_check` — the post-build cross-verification

Caching is aggressive: identical re-runs are **free** (on-disk extraction cache), prompt
prefixes and images use API-side prompt caching, and `--from-json` rebuilds a part with
**zero** API cost — including reuse of a saved `overview_analysis.json`. The Sheet-3
**Token / Cost** tab shows per-part and per-session totals live.

---

## 6. What a run delivers

Per part, in the run folder (mirrored to `UI_Output/` and `~/Downloads/SolidWorksModel_Parts/`):

```
<Part>/
├── <Part>.SLDPRT / <Part>.STL              # the model
├── <Part>_engineering_review.txt           # read this first
├── <Part>_extraction.json                  # raw extraction (never lost)
├── overview_analysis.json                  # ★ Stage 1.5 holistic cross-view read
├── <Part>_resolved_extraction.json         # every value + tier + flag + note
├── <Part>_build_plan.json                  # single source of truth for builds
├── <Part>_verification_report.txt          # arithmetic + must-meet + final checks
├── must_meet_spec.txt / must_meet_constraints.json   # the operator's authority, persisted
├── prevalidation.stl / prevalidation_report.json     # CadQuery pre-check
├── constraint_verification.json            # post-build PASS/FAIL, measured vs required
├── macro_result.json                       # per-feature build outcomes
└── macros/                                 # 00_setup … ZZZ_export_stl + RUN_ALL.vba
```

---

## 7. Reliability guarantees worth stating out loud

- **Never blocks, never silent** — every ambiguity resolves to a flagged, defensible number;
  every skipped feature becomes an explicit MANUAL step.
- **Numbers are chosen, never invented** — resolved values come only from extracted candidates
  or the operator's specs.
- **Human authority is absolute** — must-meet specs are tier 0, enforced from the extraction
  prompt through post-build measurement, and every override is logged.
- **Cross-view sanity is now first-class** — the model reasons about the drawing as *one
  coherent object* before per-view extraction, catching count/through-bore/symmetry errors at
  the cheapest possible point.
- **Everything is re-runnable and auditable** — raw extractions persist, re-runs are free,
  and every value, flag, and dollar is traceable to the stage and tier that produced it.
- **331 automated tests** (plus golden-macro comparisons) run the whole decision tree,
  including the A050211E 5-vs-6-hole scenario end-to-end.

---

## 8. Suggested live demo flow (5 minutes)

1. **Sheet 1:** load a drawing PDF, crop the front and side views.
2. **Sheet 2:** pull the crops, assign view types, type a must-meet spec
   ("6 holes, circular pattern, all through"), save the part. Use the **Select Model**
   dropdown to show a *previous* run's model loading instantly — no re-run.
3. **Sheet 3:** ▶ Run. Narrate the stage strip; point at the **Overview Analysis** panel as
   it auto-expands mid-run — the model's one-sentence read of the part and any cross-view
   conflicts, live next to the console.
4. **Sheet 2:** watch the **pre-validated STL** appear, then the SolidWorks model replace it;
   show the must-meet checklist flip to ✓ with measured values.
5. **Sheet 4:** the finished run is auto-selected. Open **Engineering Flags** — show a
   CRITICAL item's What / Decision / Why / Affects and the tier that resolved it. Then switch
   the **Select Run** dropdown to an older run to show all ten tabs reload for it.
6. Finish on **Token / Cost** — the run's exact cost, per stage.
