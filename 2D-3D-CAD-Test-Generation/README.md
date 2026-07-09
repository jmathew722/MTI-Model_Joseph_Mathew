# 2D → 3D SolidWorks Pipeline

Convert a 2D engineering drawing (image or PDF) into a parametric **SolidWorks 2024**
part — through an interactive **web UI** or the **command line**. The pipeline reads
every dimension, tolerance, view, and hole callout with the Claude Vision API,
resolves any ambiguity to a defensible number, verifies it arithmetically, then
generates SolidWorks VBA macros and (on a SolidWorks machine) builds a real
`.sldprt` and exports an `.stl`.

> **Guiding principle:** *A complete approximate model is always the correct
> outcome; an incomplete model is always the wrong outcome.* The pipeline never
> blocks on ambiguity — it makes the best defensible engineering decision, annotates
> every assumption with a confidence tier, and leaves the human to verify.

---

## Table of contents

- [What you get](#what-you-get)
- [How it works (pipeline)](#how-it-works-pipeline)
- [Requirements](#requirements)
- [Setup](#setup)
- [Web UI](#web-ui)
- [Command line (CLI)](#command-line-cli)
- [Input formats](#input-formats)
- [Outputs & where they land](#outputs--where-they-land)
- [Configuration (.env / environment)](#configuration-env--environment)
- [Stage 2.5 — ambiguity resolution](#stage-25--ambiguity-resolution)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

---

## What you get

- **Web UI** (`webapp/`) — the primary path: open **one file** (PDF, JPG/PNG,
  DWG/DXF, or eDrawings) directly in the Tab 1 cropper or upload it on Tab 2,
  crop views out of a multi-view sheet, assign each image a **view type** from a
  dropdown (Front View / Back View / Left Side View / Right Side View / Top View
  / Bottom View / Full Overview View) with 90° rotation for scanned drawings,
  name the part, and run. Progress shows **per stage**
  (Extracting → Resolving → Verifying → Building macros → Building .sldprt →
  Exporting → Done) with a run timer and Cancel. Results land live in tabs: the 3D
  **STL viewer**, verification, a severity-ranked **Engineering Flags** review,
  **Token / Cost**, and a **Files** tab linking every output.
- **CLI** (`main.py`): the same pipeline as a scriptable command — the
  advanced/manual path for batch runs (a single drawing, a batch folder, or
  multi-view part folders).
- **Severity-ranked engineering review** (`<Part>_engineering_review.txt`): every
  assumption, ambiguity resolution, and skipped/manual feature, sorted
  CRITICAL → HIGH → MEDIUM → LOW, in plain language.
- **Deliverables in two easy places:** every successful web-UI run drops a clean,
  openable folder named after the part into both the project's `UI_Output/` and your
  `~/Downloads/SolidWorksModel_Parts/` (see [Outputs](#outputs--where-they-land)).

---

## How it works (pipeline)

```
drawing/views ─► image_prep ─► overview analysis (Stage 1.5, full sheet) ─► extractor (Claude Vision) ─► resolver (Stage 2.5)
                                                                   │
                                            ┌──────────────────────┘
                                            ▼
                                     verification ─► macro generator ─► macros/*.vba
                                            │                              (run on any SolidWorks machine)
                                            └─► (COM, when SolidWorks present) solidworks_builder ─► .sldprt ─► .stl ─► model check
```

| Stage | Module | Runs on |
|-------|--------|---------|
| Image prep | `utils/image_prep.py` | any OS |
| **Holistic overview analysis (Stage 1.5)** | `pipeline/overview_analysis.py` (`claude-opus-4-8` on the FULL uncropped sheet — cross-view correspondences, overall shape, conflicts, symmetry, global notes; NOT per-dimension extraction. Output `overview_analysis.json`, fed to Stage 2.5 as tier 2; own token-ledger line `stage_1_5_overview_analysis`; skipped gracefully without a key) | any OS |
| Extraction | `pipeline/extractor.py` (`claude-opus-4-8`, forced tool call; **must-meet specs injected into the prompt — specs-first**) | any OS |
| Schema | `pipeline/schema.py` (Pydantic v2: views, hole callouts, relationships) | any OS |
| **Vector hole extraction** | `pipeline/vector_extract/` + `pipeline/hole_resolution.py` (EXACT hole positions from DXF/DWG entities or vector-PDF paths; Hough fallback for scans; vision never overrides a vector coordinate) | any OS |
| **Ambiguity resolution (Stage 2.5)** | `pipeline/resolver.py` (numeric `resolved_value` + flag tier per dimension; a must-meet spec value that clarifies an ambiguity **takes precedence** → `spec_driven`; never blocks. Priority tiers when sources disagree: **tier 0** operator specs → **tier 1** per-view extraction (owns dimension values) → **tier 2** overview analysis (owns cross-view relationships); every resolution/flag records `resolved_by_tier`) | any OS |
| Verification | `pipeline/validator.py` (dimensional closure, envelopes, advisory report) | any OS |
| **Engineering review** | `pipeline/engineering_review.py` (severity-ranked human report) | any OS |
| **VBA macros** | `pipeline/macro_generator.py` (incl. `ZZZ_export_stl.vba`; unsupported features become numbered MANUAL-step macros) | any OS (macros run on any SolidWorks machine) |
| COM build | `pipeline/solidworks_builder.py` (`.sldprt` + STL export) | Windows + SolidWorks 2024 |
| Model check | `pipeline/model_validator.py` (mass/bounding-box, skipped features) | Windows + SolidWorks 2024 |

The **extraction & verification** phases run on any OS. The **`.sldprt`/`.stl`
build** and **model check** require Windows with SolidWorks 2024 (driven over COM).
On a machine without SolidWorks the pipeline still produces the extraction, the
resolved model, the verification report, and the full VBA macro package — run
`RUN_ALL.vba` (which ends with `ZZZ_export_stl.vba`) on any SolidWorks machine to
get the model and STL.

---

## Requirements

- **Python 3.10+** (developed/tested on 3.12).
- **Anthropic API key** — for live extraction ([console.anthropic.com](https://console.anthropic.com)).
- **SolidWorks 2024** (Windows only) — *optional*; required only to build the actual
  `.sldprt`/`.stl` and run the model check. Everything else works without it.
- For PDF input: a PDF rasterizer — **PyMuPDF** (bundled in `requirements.txt`) or
  **poppler** (for `pdf2image`).
- For **DWG** input: nothing extra to install. An engine chain converts DWG→DXF:
  **ezdwg** (bundled pip package, DWG R14-2018) → the **SolidWorks translator**
  (when SolidWorks is installed — covers even 1990s R13 files) → the ODA File
  Converter if present. Only when every engine fails does the upload error, and
  the message lists exactly what was tried. DXF needs no conversion at all.

Python dependencies (`requirements.txt`): `anthropic`, `pillow`, `pdf2image`,
`PyMuPDF`, `numpy`, `pydantic`, `python-dotenv`, `rich`, `pytest`, and `pywin32`
(Windows only). The web UI adds (`webapp/requirements-ui.txt`): `fastapi`,
`uvicorn[standard]`, `python-multipart`, `ezdxf`, `matplotlib` (the last two for
DWG/DXF rendering).

---

## Setup

### 1. Get the code and a Python interpreter

On Windows, if `python` opens the Microsoft Store or does nothing, Python isn't
really installed. Install it (once):

```powershell
winget install Python.Python.3.12
```

Open a new terminal afterward so `PATH` refreshes.

### 2. Create the `.env` with your API key

```bash
cp .env.template .env      # Windows: copy .env.template .env
```

Edit `.env` and set your key (this file is **gitignored** — never commit it; never
put a real key in `.env.template`):

```ini
ANTHROPIC_API_KEY=sk-ant-...
# Optional model override (default claude-opus-4-8):
# EXTRACTION_MODEL=claude-opus-4-8
# Windows only — a REAL SolidWorks part template (see Troubleshooting for the path):
SOLIDWORKS_TEMPLATE_PATH=C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\<a real .prtdot>
```

### 3. Install dependencies

**Automated (CLI):**

```bash
python setup.py            # checks Python, installs deps, creates .env
```

**Web UI (creates its own venv):**

```bash
cd webapp
./run.sh                   # macOS/Linux: venv + deps + serves :8092
```

**Windows, manual (what this machine uses):**

```powershell
cd webapp
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r ..\requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
```

---

## Web UI

A single-page, tabbed front end that drives `main.py` as a subprocess (the proven
CLI is untouched), streams its console live, and renders every output file. No CDN
or external uploads at runtime — Three.js and pdf.js are vendored locally.

### Launch

```bash
# macOS/Linux:
cd webapp && ./run.sh

# Windows (or double-click webapp\run.bat):
cd webapp
.\run.ps1

# Windows, venv already prepared:
.venv\Scripts\python.exe -m uvicorn app:app --app-dir . --host 127.0.0.1 --port 8092
```

Both launchers create `.venv` on first run and install the **pinned**
dependency set (`requirements.txt` + `requirements-ui.txt` use exact `==`
versions), and every frontend asset is vendored in the repo — so a fresh clone
reproduces this exact UI, byte for byte, on any machine.

Then open <http://127.0.0.1:8092/>. Live extraction needs `ANTHROPIC_API_KEY` in
`../.env`; without it, use **▶ Run demo**, which runs the pipeline from a saved
extraction (no API call).

### Tabs

1. **Drawing Crop** — the DrawingCrop photo app, embedded **verbatim** and
   filling the whole tab: load a multi-view sheet and crop each view out. Its
   Open button / drag-and-drop accepts **PDF, JPG/PNG, and DWG/DXF/eDrawings
   directly** — CAD formats are converted server-side automatically and the
   rendered drawing opens in the cropper (a bridge hook feeds them through
   `/api/convert-dwg`; the photo app's sources are untouched). Multi-sheet
   DWG/DXF switches you to Tab 2's sheet picker.
2. **Part Setup & 3D Model** — three labeled groups across the top:
   **1 · Add images** (upload one PDF/JPG/PNG/DWG/DXF/eDrawings file or pull the
   queued crops from Tab 1), **2 · Assign view types**, **3 · Name & save**.
   Below them, **two half-screen panels** side by side: the left **Full Overview
   View** panel shows the image tagged "Full Overview View" for the current part
   (fit-to-panel, scroll-zoom / drag-pan / double-click reset, with a clear
   *"No overview image provided for this part"* empty state) so a human can
   eyeball the whole drawing against the model; the right panel is the
   interactive **Three.js STL viewer** (drag-rotate, scroll-zoom,
   right-drag-pan) — the STL loads automatically once the pipeline produces it.
3. **Pipeline & Results** — the saved-parts picker and the primary
   **▶ Pull & Run Pipeline** button (plus demo run and Cancel), a slim status bar
   (stage strip, progress bar, run timer), a collapsible live-console strip, and
   the **Run outputs** dock with sub-tabs — *Extraction JSON · Resolved
   Extraction · Build Plan · Verification · Engineering Flags · Model Check ·
   VBA Macros · Token / Cost · Files · Console* — each fills the moment the
   pipeline writes the corresponding file. *Engineering Flags* renders the
   severity-ranked review (CRITICAL first); *Token / Cost* shows this part's API
   spend and the session total; *Files* links every output file plus the
   delivered folder paths.

### Run a part (upload → orient → name → run)

1. **Get images in** — two equal paths:
   - **Open on Tab 1** (cropper): any supported file — PDF, JPG/PNG, DWG/DXF,
     eDrawings — opens straight in the DrawingCrop tool (CAD converts
     server-side automatically). Crop each view, **Queue View**, then
     **⬇ Pull queued crops** on Tab 2.
   - **📄 Upload drawing on Tab 2** — one PDF (each page becomes an image),
     JPG/PNG, DWG/DXF (converted server-side; multi-sheet drawings offer a
     sheet picker, like PDF pages), or an **eDrawings** file (.edrw/.eprt/.easm
     — the embedded raster preview is extracted and clearly labeled a *static
     preview*, since no interactive eDrawings view exists server-side).
     Converted CAD drawings are also opened in the Tab 1 cropper automatically
     for view cropping.

   A format badge shows what was actually loaded (PDF / DWG / eDrawings / image),
   and every conversion is cached (`webapp/.convert_cache/`) and logged
   (`webapp/conversion_log.jsonl`: source format, tool, output).
2. **Assign view types.** Each image gets one of the eight canonical view types
   from a dropdown — **Front View / Back View / Left Side View / Right Side View
   / Top View / Bottom View / Full Overview View / Marked View**. Rotate 90° (⟳)
   for scanned drawings. Duplicate view types show a warning badge and need a
   confirm on save. The inline banner requires **Top + one more orthographic
   view** before saving is enabled — the top view anchors the bottom-left (0,0)
   origin convention, and the second view resolves depth. An image tagged
   **Full Overview View** becomes the canonical `00_full.jpg` overview
   (whole-part extraction context and the post-build overview cross-check) and
   appears live in the left panel below; an image tagged **Marked View** becomes
   `full_marked_view.jpg` — the annotated drawing Claude extraction consumes to
   batch each color group's X/Y coordinates with its hole placement and to read
   the locked origin for consistent model orientation.
3. **Name the part** (becomes the folder name), optionally type **must-meet
   specifications** (one requirement per line — applied from the start of
   extraction and Stage 2.5 resolution, then graded against the built part; an
   unmet line blocks READY; edit a saved part's notes in place with
   **💾 Update notes**), and **💾 Save part** — the images are written
   server-side in the exact `--views-folder` layout, so the folder works with
   the CLI unchanged. The untouched original upload is kept and delivered with
   the outputs.
4. **Select the part** and click **▶ Run Pipeline**. It runs scoped to just that
   part's folder: `main.py --views-folder <part> --output <part>/output`.

While it runs you get:

- a **per-stage strip** (Extracting → Resolving → Verifying → Building macros →
  Building .sldprt → Exporting → Done) plus a progress bar and run timer,
- a live **Console** stream (partial failures surface inline, per stage),
- a **✕ Cancel** button that terminates the run and its SolidWorks child processes.

On success the tabs fill in place (no reload) and the status line shows exactly
where the outputs were saved (see below).

---

## Command line (CLI)

```bash
# Extract + verify only (no SolidWorks needed):
python main.py --drawing path/to/drawing.pdf --validate-only --debug

# Full pipeline + VBA macro package (runs anywhere):
python main.py --drawing path/to/drawing.pdf --output ./output

# Regenerate macros from a saved extraction (no API call):
python main.py --from-json debug_extraction.json --output ./output

# Batch a whole folder (drawings are extracted; *_extraction.json are free):
python main.py --batch ./DrawingPDFs --output ./output       # → output/batch_summary.csv

# Multi-view: each part is a folder of SEPARATE per-view images, built per plane:
python main.py --views-folder ./Test2 --output ./Test2/output   # → output/multiview_summary.csv

# Direct COM build (Windows + SolidWorks 2024):
python main.py --drawing path/to/drawing.pdf --engine com
```

> On Windows, if `python` doesn't resolve, use the venv interpreter, e.g.
> `webapp\.venv\Scripts\python.exe main.py ...`.

### Flags

One source is required: `--drawing` · `--from-json` · `--batch` · `--views-folder`.

| Flag | Effect |
|------|--------|
| `--output DIR` | Output directory (default `./output`). |
| `--page N` | Page to use for multi-page PDFs (default 1). |
| `--source-file F` | Original vector drawing (PDF/DXF/DWG) for exact hole positions. Defaults to `--drawing` when it is already one of those; for `--views-folder`, any PDF/DXF/DWG inside the part folder is used. |
| `--debug` | Also save intermediate extraction JSON. |
| `--engine vba\|com` | `vba` (default, any OS) generates macros; `com` drives SolidWorks directly (Windows). |
| `--validate-only` | Extract + verify only; no macros, no SolidWorks. |
| `--requirements F` | Human must-meet notes file (one per line) — graded met/partial/unmet/not_applicable against the build; an unmet line gates READY. Auto-discovered as `notes.txt` in a part folder. |
| `--skip-overview-check` | Skip the final overview cross-check (auto-skips with a note when a part has no overview image). |
| `--skip-requirements-check` | Skip grading the human notes. |
| `--no-resolve` | Skip Stage 2.5 (legacy behavior). |
| `--strict-gate` | Restore the v2 hard gate: a failing verification BLOCKS the run. |
| `--no-sldprt` | Don't build the `.sldprt`/`.stl`; emit macros + text only. |
| `--no-export` | Don't copy outputs to `~/Downloads/SolidWorksModel_Parts`. |
| `--no-extract-cache` | Force a fresh (paid) extraction even if an identical image was seen. |

Exit codes: `0` success (all parts READY) · `8` completed but not every part was
READY · `2` bad path/arguments.

### Final checks (after the build, before READY)

Two checks run at the end of every part and can gate the status to **NOT
READY** (macros and the model are still produced — only the status changes):

- **Overview cross-check** — the part's overview/full drawing (the webapp's
  `00_full.jpg`, or any `*overview*`/`*full*`/part-named image in the folder)
  is re-examined ALONE with a focused vision pass listing every feature it
  shows, then diffed against the build. A feature clearly visible in the
  overview but missing from the build is **CRITICAL** (gates READY); a count
  mismatch is HIGH; a possible/ambiguous feature is MEDIUM. Features in the
  build but not visible in the overview are fine. The pass is cached and its
  cost is logged to the token ledger. Results land in a **"Overview
  Verification"** section of the engineering review and verification report.
- **Human requirements compliance** — the operator's must-meet specifications
  (web UI Part Setup textarea → `notes.txt`, or `--requirements FILE`) are split
  into one requirement per line, graded **met / partial / unmet / not_applicable**
  (compliance is never fabricated — non-geometric notes are listed as
  not_applicable for manual verification), persisted as
  `<Part>_requirements.json`, and reported in a **"Human-Specified
  Requirements Compliance"** section. An **unmet** requirement gates READY.
  This is the *final* re-grade against the built part — the same specs are
  enforced **specs-first**, before and during extraction/resolution (see below),
  so requirements shape the model from the start rather than only being checked
  at the end.

### Specs-first (must-meet specifications enforced from the start)

The operator's must-meet specifications are not merely checked at the end — they
are read **before** extraction and drive every stage:

- **Extraction** (`pipeline/extractor.py`): the spec lines are injected into the
  Claude Vision prompt so the model actively looks for and prioritizes the
  features/dimensions they name. The spec text is part of the extraction cache
  key, so changed specs force a fresh extraction (no spec-blind cached result).
- **Resolution** (`pipeline/resolver.py`, Stage 2.5): a spec value that clarifies
  an ambiguous/illegible dimension **takes precedence** over the generic decision
  tree; that dimension is flagged `assumption_basis="spec_driven"`. Numbers are
  still never fabricated — a spec only wins when it matches a candidate reading.
- **Early pre-check**: right after resolution (before the build) the specs are
  graded against the resolved model, so an unmet spec surfaces early in the run
  console (`[SPEC] …`), not only in the final gate.
- **Final gate**: the same specs are re-graded against the built part; an unmet
  line gates READY (above). The engineering review and verification report state
  the specs were *applied during extraction/resolution and verified against the
  build*, not just checked post-hoc.

---

## Input formats

### Single drawing (`--drawing`)

One image or PDF containing the whole drawing. Claude reads all views from the
single sheet.

### Multi-view part folders (`--views-folder`)

Each **part** is a subfolder of **separate per-view images**; each view is sketched
on its own SolidWorks plane. This is exactly what the web UI's per-part Run uses.

```
Test2/
├── A001271E/                       ← one part = one folder
│   ├── A001271E.png                full drawing   → whole-part CONTEXT (see below)
│   ├── A001271E_front_view.png     front          → Front Plane (base profile + depth)
│   └── A001271E_side_view.png      side           → Right Plane
├── 16247/
│   └── ...
```

- **Processing order is fixed:** front, top, side, second_side, bottom. Only the
  **front** view is required; the rest are optional.
- **Naming is flexible** — the view is detected from keywords in the filename
  (`front`/`top`/`side`/`right`/`left`/`second`/`bottom`) or a leading `01`–`05`.
  The web UI names committed crops this way automatically.
- **Full-drawing context:** a file named `full` / `overview` / `isometric`, **or a
  file whose name matches the part folder** (e.g. `A001271E.png` in folder
  `A001271E`), is classified as the `full` overview view. It is **not** built as a
  plane — it's sent to Claude as whole-part context so extraction sees the entire
  drawing in addition to the individual view crops. The web UI feeds the original
  full drawing in automatically (saved as `00_full.jpg` in the part folder).
- All of a part's views (plus the overview) go to Claude in **one** call, labeled by
  view, so each feature's sketch plane comes from the view it was read in.

---

## Outputs & where they land

### Per-part output package

```
<output>/<PartNumber>/
├── <PartNumber>.SLDPRT                    # the 3D model — built when SolidWorks is available
├── <PartNumber>.STL                       # STL export (for the 3D viewer)
├── <PartNumber>_engineering_review.txt    # SEVERITY-RANKED review: every assumption & manual step,
│                                          #   CRITICAL → HIGH → MEDIUM → LOW, plain language
├── <PartNumber>_model_check.txt           # mass/bounding-box validation + any skipped features
├── <PartNumber>_extraction.json           # RAW extraction, verbatim (saved even when BLOCKED)
├── <PartNumber>_resolved_extraction.json  # Stage 2.5: each dim's resolved_value + flag tier + note
├── <PartNumber>_verification_report.txt   # verification report + readiness score
├── <PartNumber>_requirements.json         # human must-meet notes, graded met/partial/unmet/not_applicable
├── <PartNumber>_build_plan.json           # SELF-CONTAINED steps + resolution_summary + engineering_review
├── <PartNumber>_audit_report.json         # static self-validation of the generated macros
├── macros/                                # 00_setup … ZZ_final_verify, ZZZ_export_stl, RUN_ALL.vba, README.md
│                                          #   unsupported features appear as NN_Fxxx_MANUAL_*.vba steps
└── logs/                                  # build_log.txt appended by the macros
```

The **engineering review** is the first thing to read after a run: one ranked list
of every assumption, ambiguity resolution, and skipped/manual feature, most urgent
first, each with what was ambiguous, the decision made, why, and what it affects.
It is regenerated after the `.sldprt` build so COM-skipped features are always in
it, and it renders in the web UI's **Engineering Flags** tab.

The **raw extraction JSON is written for every run** so a paid extraction is never
lost — patch it and regenerate with `--from-json` (no API cost). Every `.vba` is
statically self-validated before it's written (banned/nonexistent APIs fail
generation outright).

### Delivery locations

Every **successful** run copies the part's outputs — flat and openable — into two
places, one folder per part, refreshed on each re-run:

| Location | Path |
|----------|------|
| In the project | `2D-3D-CAD-Test-Generation/UI_Output/<Part>/` |
| In Downloads | `~/Downloads/SolidWorksModel_Parts/<Part>/` |

Each delivered folder contains the `.SLDPRT`, `.STL`, all JSON/report files, and the
`macros/` subfolder. SolidWorks lock/autosave junk (`~$*`, `AUTOSAVE_*`) is skipped.
`UI_Output/` is gitignored. (The CLI's own `--no-export`/Downloads behavior is
independent; the web-UI delivery above is always on for successful part runs.)

---

## Configuration (.env / environment)

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | **Required** for live extraction. |
| `EXTRACTION_MODEL` | Override the extraction model (default `claude-opus-4-8`). |
| `SOLIDWORKS_TEMPLATE_PATH` | Windows: a real blank part `.prtdot` template to base new parts on. |
| `MAX_IMAGE_LONG_EDGE` | Max image long edge in px before extraction (default `2576`, the high-res vision ceiling). Lower it to trade accuracy for fewer image tokens. |

**Token economy:** the static prompt, tool schema, and images carry `cache_control`,
and an on-disk **extraction cache** (`<output>/.extraction_cache`) returns identical
results with **zero** API calls on a re-run with the same images and output dir. Each
extraction's token usage and USD cost are appended to `token_usage_log.txt`.

---

## Exact hole positions — vector extraction

Hole placement no longer relies on vision coordinate estimation when a vector
source exists. `pipeline/vector_extract/` reads the ORIGINAL drawing file and
`pipeline/hole_resolution.py` merges what it finds into the extraction before
Stage 2.5:

- **DXF/DWG** (`ezdxf`): CIRCLE/ARC entities and block INSERTs give exact
  centers and radii; `$INSUNITS` supplies the unit conversion. DWG converts via
  the no-install engine chain (ezdwg → SolidWorks translator → ODA if present);
  if every engine fails the run falls back (flagged, never silent) to raster.
- **Vector PDF** (PyMuPDF): circles (including the standard 4-Bézier circle
  encoding) are fitted analytically with a circularity check; concentric pairs
  confirm counterbores; centerline crosses confirm hole centers. Scanned PDFs
  are detected and flagged `RASTER`.
- **Raster fallback** (OpenCV): HoughCircles candidates with sub-pixel
  refinement — positions are tagged `hough`, capped in confidence, and always
  flagged; they never masquerade as exact.

Precedence: **vector geometry owns POSITION** (vision never overrides it);
**the callout owns SEMANTICS** (diameter value, thread, depth, counterbore).
When they disagree the callout keeps the value, the vector keeps the position,
and the hole is flagged CRITICAL. The drawing scale must be anchored by at
least two agreeing dimension matches (part outline vs envelope callouts,
declared DXF units); one anchor flags HIGH; zero leaves positions with vision
(flagged). "N×" counts are verified against the number of matching circles.

Results land in the existing schema shape (`instance_positions` in drawing
units → `positions_xy_meters` in the build plan) plus two additive fields on
each hole callout and build-plan step: `position_source`
(`dxf_entity | pdf_vector | hough | vision`) and `position_confidence` (0..1).
Old extraction JSONs load unchanged. Vector-derived centers are bit-exact
across runs (regression-tested). See `HOLE_EXTRACTION_REPORT.md` for the
before/after verification on real and synthetic parts.

> Positioned-OCR note: eDOCr was evaluated for positioned dimension/GD&T text
> but its TensorFlow stack does not install cleanly beside this pipeline. Its
> role is covered by native positioned text (DXF TEXT/MTEXT, PDF words) and,
> for raster drawings, `vector_extract.raster_holes.callout_crops()` — targeted
> per-hole crops a caller can send to Claude Vision for "what is the callout at
> this location" reads.

---

## Stage 2.5 — ambiguity resolution

By default the pipeline never blocks on ambiguity. `pipeline/resolver.py` works
through every value flagged `value_unclear` / `resolution_required` /
unknown-position with a deterministic decision tree:

1. **Arithmetic chain** — the only candidate reading that closes a dimension chain
   within tolerance → tier **HIGH**.
2. **Geometric validity** — eliminate readings that don't fit the part envelope →
   tier **MEDIUM**.
3. **Conservative geometry** — among survivors, prefer the smallest/shallowest →
   tier **LOW**.
4. **Last resort** — derive from an adjacent dimension, default a missing depth to
   through-all, a missing radius to the general tolerance, or center on the parent →
   tier **CRITICAL**.

Every dimension ends with a numeric `resolved_value`; every feature is marked
`build_status: build`. Numbers are **chosen from what was extracted — never
fabricated**. Each assumption carries `assumption_basis`, `assumption_confidence`,
`flag_tier`, and an ID-naming `human_note`. The macro generator turns each flag into
VBA by tier: **HIGH** → a `' NOTE` comment; **MEDIUM** → `MsgBox vbInformation`;
**LOW** → `MsgBox vbExclamation`; **CRITICAL** → a banner + a confirmation dialog the
operator must acknowledge.

**Always read `<Part>_engineering_review.txt` before relying on a model.** It maps
the resolver's tiers into review severities (resolver CRITICAL → CRITICAL, resolver
LOW → HIGH, resolver MEDIUM → MEDIUM, confirmed-with-assumption → LOW) and adds
every macro-manual and COM-skipped feature, so nothing needing human attention is
scattered across files. A CRITICAL value is a defensible default, not a confirmed
reading. When `model_check.txt` lists skipped features, the `.sldprt` is **not**
feature-complete (the macros still contain those features as steps).

---

## Testing

```bash
pytest tests/ -v
# single suite:
pytest tests/test_multiview.py -q
```

---

## Troubleshooting

**`python` opens the Microsoft Store / “Python was not found”.** Python isn't
installed — only the Windows Store alias stub. Run `winget install Python.Python.3.12`,
open a new terminal, and re-run. In an existing shell whose `PATH` hasn't refreshed,
call the interpreter by full path (`...\Python312\python.exe` or the venv's
`.venv\Scripts\python.exe`).

**The web UI run hangs / the Console freezes and no outputs appear.** This was caused
by the server decoding the subprocess's stdout with the Windows locale codec
(cp1252); the pipeline's `rich` console emits UTF‑8 box‑drawing characters, which
crashed the reader thread and blocked the child on a full pipe. **Fixed** — the
subprocess is now read as UTF‑8 with replacement. If you see a hang again, make sure
you're running the current `webapp/app.py` and restart the server.

**`.sldprt` build failed: “Part template not found”.** `SOLIDWORKS_TEMPLATE_PATH`
points at a template that doesn't exist. The classic `...\templates\Part.prtdot` may
not be present; find a real one, e.g.:

```powershell
Get-ChildItem "C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates" -Recurse -Filter *.prtdot
```

Point `SOLIDWORKS_TEMPLATE_PATH` at one of those (mind the exact folder casing) and
restart the server so the subprocess inherits the new value.

**`WARNING Image appears nearly blank (mean pixel value 25x > 240)`.** Advisory, not
an error. Clean CAD drawings are ~99% white paper with thin line work, so a high mean
brightness is normal — the model still reads them fine. It only means the crop is
mostly whitespace; feeding the full drawing as overview context (the web UI does this
automatically) gives extraction the complete picture.

**SolidWorks build skips features / conflicts on re-run.** Leaving a part's document
open in SolidWorks can conflict with rebuilding that same part. Close the document in
SolidWorks before re-running that part. Skipped features are always listed in
`<Part>_model_check.txt` with a reason; the generated macros still build them.

---

## Limitations

- Feature/hole **positions** are only as good as the drawing callouts: an
  undimensioned position is centered and flagged `POSITION ASSUMED`.
- Stage 2.5 **resolves rather than blocks** — a CRITICAL-tier value is a defensible
  default, not a confirmed reading.
- **Prohibited features** (loft, sweep, boundary, shell, draft, surfacing, helical
  threads) are never built automatically — each becomes a numbered
  `NN_Fxxx_MANUAL_*.vba` step with the extracted values in its comments, and is
  flagged CRITICAL in the engineering review. Threads are cosmetic.
- Revolves without an extracted profile and feature-level patterns without a
  covered seed are emitted as TODO-marked macros (flagged HIGH).
- The COM build path, `.sldprt`/`.stl` outputs, model check, and Tab 2's 3D content
  are exercised only on Windows + SolidWorks 2024.
- **DWG input** needs no extra install: ezdwg (pip) covers DWG R14-2018, and the
  SolidWorks translator covers the rest (incl. R13) when SolidWorks is present.
  Only if every engine fails does the upload error, listing what was tried.
