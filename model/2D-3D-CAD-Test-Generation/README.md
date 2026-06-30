# 2D → 3D SolidWorks Pipeline

Convert a 2D engineering drawing (image or PDF) into a parametric SolidWorks 2024
part in two phases:

- **Phase 1 — Extraction & Verification** (runs on any OS): extract every
  dimension, tolerance, view, hole callout, and geometric relationship with the
  Claude Vision API, then arithmetically verify it (dimensional closure, pattern
  envelopes, unit consistency, ambiguity flags). Output: a `VERIFICATION REPORT`.
- **Stage 2.5 — Ambiguity Resolution** (the *chief-engineer pass*, any OS, on by
  default): every ambiguous or under-dimensioned value is resolved to the best
  defensible number (arithmetic-chain closure → geometric validity → conservative
  geometry → last resort), and every feature is marked **build**. Nothing is left
  unresolved and the build no longer blocks on ambiguity — instead each assumption
  is annotated with a confidence **flag tier** (HIGH/MEDIUM/LOW/CRITICAL) and an
  actionable human note. *A complete approximate model beats an incomplete one.*
- **Phase 2 — Build**: generate numbered **SolidWorks VBA macros** (default) that
  you run inside SolidWorks on any machine — *no Python needed there* — or drive
  SolidWorks directly over COM (`--engine com`, Windows only). Each macro surfaces
  its assumption flags (NOTE / MsgBox / confirmation dialog) by tier.

## Pipeline

```
drawing → image_prep → extractor (Claude) → resolver (Stage 2.5) → verification → macro generator → macros/*.vba
                                                  │                                                       (run in SolidWorks)
                                                  └→ (--engine com) solidworks_builder → .sldprt
```

| Stage | Module | Runs on |
|-------|--------|---------|
| Image prep | `utils/image_prep.py` | any OS |
| Extraction | `pipeline/extractor.py` (`claude-sonnet-4-6`, forced tool call) | any OS |
| Schema | `pipeline/schema.py` (Pydantic v2; views, hole callouts, relationships, ambiguity) | any OS |
| **Ambiguity resolution** | `pipeline/resolver.py` (resolved_value + flag tier per dim; never blocks) | any OS |
| Verification | `pipeline/validator.py` (closure, envelopes, advisory report) | any OS |
| **VBA macros** | `pipeline/macro_generator.py` | any OS (macros run on any SolidWorks machine) |
| COM build | `pipeline/solidworks_builder.py` | Windows + SolidWorks 2024 |
| Model check | `pipeline/model_validator.py` | Windows + SolidWorks 2024 |

## Setup

```bash
python setup.py                 # checks Python, installs deps, creates .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
# Extract + verify only (no SolidWorks needed):
python main.py --drawing path/to/drawing.pdf --validate-only --debug

# Full Phase 1 + VBA macro package (runs anywhere):
python main.py --drawing path/to/drawing.pdf --output ./output

# Regenerate macros from a saved extraction (no API call):
python main.py --from-json debug_extraction.json --output ./output

# Batch a whole folder (drawings are extracted; *_extraction.json are free):
python main.py --batch ./DrawingPDFs --output ./output   # writes output/batch_summary.csv

# Multi-view: each part is a folder of SEPARATE per-view images, built per plane:
python main.py --views-folder ./Drawings --output ./output  # writes output/multiview_summary.csv

# Direct COM build (Windows + SolidWorks 2024):
python main.py --drawing path/to/drawing.pdf --engine com

# Tests:
pytest tests/ -v
```

## Multi-view input (separate image per view)

When each orthographic view is a **separate image**, use `--views-folder`. Each
part is a subfolder of view images; every view is sketched on its own SolidWorks
plane and the part is built from them:

```
Drawings/
├── 115-C/                     ← one part = one folder
│   ├── 01_front.png           front      → Front Plane (base profile + depth)
│   ├── 02_top.png             top        → Top Plane
│   ├── 03_side.png            side/right → Right Plane
│   ├── 04_second_side.png     left       → Right Plane (opposite face)   [optional]
│   └── 05_bottom.png          bottom     → Top Plane (opposite face)     [optional]
├── 116-C/
│   └── ...
```

- **Views are always processed in this exact order:** front, top, side,
  second_side, bottom. Only the **front** view is required (it defines the base
  profile, extruded to the depth read from the top/side view); the rest are optional.
- **Naming is flexible** — the view is detected from keywords in the filename
  (`front`/`top`/`side`/`right`/`left`/`second`/`bottom`, or a leading `01`–`05`).
- All of a part's views go to Claude in **one** call, labeled by view, so each
  feature's sketch plane comes from the view it was read in. A feature visible in
  several views is extracted once (no double-counting).
- If the folder holds images directly (no subfolders), it's treated as one part.
- Output per part is the usual `output/<Part>/` package (extraction, verification,
  per-plane `macros/` incl. `RUN_ALL.vba`), plus `output/multiview_summary.csv`.

Flags: `--drawing`, `--from-json`, `--batch`, or `--views-folder` (one required), `--output`,
`--page N`, `--debug`, `--engine vba|com` (default `vba`), `--validate-only`,
`--no-sldprt` (skip the default `.sldprt` build; emit macros + text only),
`--no-export` (skip copying outputs to `~/Downloads/SolidWorksModel_Parts`),
`--no-resolve` (skip Stage 2.5 and use the legacy verification behavior),
`--strict-gate` (restore the v2 hard gate: a failing verification BLOCKS the run).

## Stage 2.5 — Ambiguity resolution (chief-engineer pass)

The owner's directive: **a complete approximate model is always the correct
outcome; an incomplete model is always the wrong outcome.** So by default the
pipeline never blocks on ambiguity. `pipeline/resolver.py` works through every
value flagged `value_unclear` / `resolution_required` / unknown-position and
resolves it with a deterministic decision tree:

1. **Arithmetic chain** — pick the only candidate reading that closes a dimension
   chain within tolerance → tier **HIGH**.
2. **Geometric validity** — eliminate readings that don't fit the part envelope
   (wall thickness, cut depth ≤ solid, fits inside parent) → tier **MEDIUM**.
3. **Conservative geometry** — among survivors prefer the smallest/shallowest →
   tier **LOW**.
4. **Last resort** — derive from an adjacent dimension, default a missing depth to
   through-all, a missing radius to the general tolerance, or center on the parent
   → tier **CRITICAL**.

Every dimension ends with a numeric `resolved_value`; every feature is marked
`build_status: build`. Each assumption carries `assumption_basis`,
`assumption_confidence`, `flag_tier`, and an ID-naming `human_note`. The numbers
are **chosen from what was extracted** (candidate readings, chains, adjacent
dimensions) — never fabricated. The macro generator turns each flag into VBA by
tier: **HIGH** → a `' NOTE` comment, **MEDIUM** → `MsgBox vbInformation`, **LOW**
→ `MsgBox vbExclamation`, **CRITICAL** → a banner + a confirmation dialog the
operator must acknowledge (Cancel logs and stops the macro).

The `build_plan.json` is **self-contained**: a header states the coordinate
convention (`lower_left_corner_of_base_solid`, +X right, +Y up,
`unit_factor_to_meters`), every step carries its dimensions in *both* drawing
units and meters, hole/pattern `positions_xy` in both, its `flags[]`,
`requires_input` / `auto_select_strategy` / `expected_edge_count` for
fillets/chamfers, and per-step `assumption_made` / `assumption_confidence` /
`flag_tier`. A `resolution_summary` block gives the counts and a plain-English
narrative — so a macro generator can build any step without reading the
extraction JSON.

## Output package (engine `vba`)

```
output/<PartNumber>/
├── <PartNumber>.sldprt                     # the 3D model — built by default when SolidWorks is available
├── <PartNumber>_model_check.txt            # mass/bounding-box validation + any skipped features
├── <PartNumber>_extraction.json            # RAW Phase 1 extraction, verbatim (saved even when BLOCKED)
├── <PartNumber>_resolved_extraction.json   # Stage 2.5: every dim's resolved_value + flag tier + human note
├── <PartNumber>_verification_report.txt    # verification report + Phase-4 readiness score
├── <PartNumber>_build_plan.json            # SELF-CONTAINED steps (drawing+meters dims, positions_xy, flags) + resolution summary
├── <PartNumber>_audit_report.json          # static self-validation of the generated macros
├── macros/                                 # 00_setup … ZZ_final_verify, RUN_ALL.vba, README.md
└── logs/                                   # build_log.txt appended by the macros
```

**The `.sldprt` is a required output of every run.** Whenever the pipeline runs
on a machine with SolidWorks 2024 available over COM (any mode: `--drawing`,
`--batch`, `--views-folder`), each READY part is built into a real `.sldprt` in
its own folder, alongside the text reports and VBA macros — no separate step. The
build is non-strict: a fragile feature (e.g. a fillet without selectable edges) is
skipped and recorded in `<PartNumber>_model_check.txt` rather than failing the
part. If SolidWorks is unavailable (non-Windows, not installed, no license) the
run still produces the text reports + macros and prints why the `.sldprt` was
skipped. Pass `--no-sldprt` to opt out and emit macros only. BLOCKED parts are
never built (verification gate).

**Final step — Downloads delivery.** As the last step of every run, all part
outputs (the `.sldprt` models and the text files) are copied into
`~/Downloads/SolidWorksModel_Parts` (Windows: `C:\Users\<you>\Downloads\SolidWorksModel_Parts`)
so the deliverables always land in one well-known place. The folder is created if
absent and updated in place on re-runs (the internal extraction cache is not
copied). Pass `--no-export` to skip this step.

The extraction JSON is written for **every** run, READY or BLOCKED, so a paid
extraction is never lost — patch it against the drawing and regenerate with
`--from-json` (no API cost). The verification report includes a **drawing
completeness score** (geometry / dimension / consistency / feature confidence and
an overall *macro readiness* %); set `MACRO_READINESS_THRESHOLD` (e.g. `0.95`) to
hard-gate low-readiness drawings. Before any macro is written, every `.vba` is
**statically self-validated** (`pipeline/macro_audit.py`): banned/nonexistent APIs
and structural defects fail generation outright.

Copy the folder to any SolidWorks machine (e.g. a school VDI — no installs
needed) and follow `macros/README.md`: run the macros in numbered order; each
logs PASS/FAIL and stops on failure.

## Key design notes

- **Extraction:** `claude-sonnet-4-6` (override with `EXTRACTION_MODEL`) via a
  **forced tool call** validated against the Pydantic schema with one repair
  retry. (Strict structured outputs reject this schema's nested arrays — don't
  switch back.)
- **Token economy:** the static system prompt + tool schema (~4.6k tokens) and the
  image carry `cache_control`, so within a batch nearly every call reads the prefix
  from cache (~10% cost) and a low-confidence re-query reads the image from cache.
  An on-disk **extraction cache** (`<output>/.extraction_cache`, disable with
  `--no-extract-cache`) returns an identical image+model result with **zero** API
  calls. The low-confidence re-query only fires when something specific was flagged
  to re-examine. Per-extraction token usage is logged. Image resolution is tunable
  with `MAX_IMAGE_LONG_EDGE` (default 2576) to A/B accuracy vs. tokens; set
  `EXTRACTION_CONFIDENCE_THRESHOLD` to tune the re-query gate.
- **Units:** SolidWorks API works in meters. Python COM path: every value
  through `to_meters()` + `assert_meters()`. VBA path: every value written as
  `<drawing value> * UNIT_FACTOR` for traceability.
- **Verification gate:** advisory by default — ambiguous dimensions, non-closing
  chains and infeasible patterns are reported but Stage 2.5 resolves them and the
  build proceeds with annotated assumptions. `--strict-gate` (or `--no-resolve`)
  restores the v2 behavior where these **block** the build.
- **Macro discipline:** one macro per feature, named features, per-step
  PASS/FAIL logging, stop-on-first-failure, fillets/chamfers last (interactive
  edge selection with extracted values baked in).
- **Prohibited features** (loft, sweep, boundary, shell, draft, surfacing,
  helical threads): never generated — flagged in the build plan and skipped.
  Threads are cosmetic only.

## Limitations

- Feature/hole **positions** are only as good as the drawing callouts: when a
  position isn't dimensioned from the origin, Stage 2.5 centers the geometry and
  flags it `POSITION ASSUMED` (tier LOW) — verify before trusting the model.
- Stage 2.5 **resolves rather than blocks**: a CRITICAL-tier value is a defensible
  default, not a confirmed reading. Always review the `resolution_summary` and any
  MEDIUM/LOW/CRITICAL flags (the build plan lists them; the macros prompt for
  CRITICAL) before relying on the model.
- Revolves and feature-level patterns are emitted as TODO-marked skeletons
  (`needs_review` in the build plan) rather than guessed API calls.
- The COM build path (`--engine com`) and `ZZ_final_verify` macro are exercised
  only on Windows + SolidWorks 2024.
- Checkpoint *resume* for the COM path is not implemented (partial-save +
  auto-save only).
