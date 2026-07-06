# MTI 2D → 3D SolidWorks Pipeline — Run Guide

Convert 2D engineering drawings into **SolidWorks 2024 parts** —
extraction (Claude `claude-sonnet-5`) → ambiguity resolution → verification →
VBA macros + a real `.sldprt`, with token costs logged, a severity-ranked
engineering review per part, and all deliverables copied to your Downloads folder.

This page is the **operator run guide**. The deep technical docs live in
[`2D-3D-CAD-Test-Generation/README.md`](2D-3D-CAD-Test-Generation/README.md).
To have an agent run + verify a batch for you, use
[`RUN_PROMPT.md`](RUN_PROMPT.md).

---

## What one run does (the whole flow)

```
drawing(s) → image prep → Sonnet 5 extract → Stage 2.5 resolve → verify
           → VBA macros + build_plan.json + resolved_extraction.json
           → .sldprt (SolidWorks COM) → final checks (overview cross-check
           + human requirements grading) → engineering review → token ledger
           → copy to ~/Downloads
```

- **Stage 2.5 (chief-engineer pass):** every ambiguous / under-dimensioned value
  is resolved to a defensible number and flagged — the build never blocks on
  ambiguity.
- **Final checks (after the build):** the part's **overview drawing is
  re-examined** and diffed against the build (a feature clearly shown but
  missing is CRITICAL and gates READY), and any **operator must-meet notes**
  are graded met/partial/unmet/not_applicable (an unmet line gates READY).
  The model and macros are still produced — only the status changes; override
  with `--skip-overview-check` / `--skip-requirements-check`.
- **`<Part>_engineering_review.txt`:** one ranked list per part of every
  assumption and skipped/manual feature, CRITICAL first, in plain language.
- **`.sldprt` is built by default** when SolidWorks 2024 is available (Windows).
  Off Windows / no SolidWorks, you still get macros + reports and a clear note.
- **Deliverables are copied to `~/Downloads/SolidWorksModel_Parts`** as the last
  step of every run.

---

## Prerequisites

| Need | For |
|------|-----|
| Python 3.10+ | the whole pipeline |
| `ANTHROPIC_API_KEY` | Sonnet 5 extraction (skipped on cache hits) |
| SolidWorks 2024 (Windows) | building the `.sldprt` (optional — macros work anywhere) |

DWG needs **nothing extra to install**: a built-in engine chain converts it
(ezdwg → SolidWorks translator → ODA only if already present) — even 1990s R13
files open. DXF/PDF/JPG/PNG and eDrawings (.edrw/.eprt/.easm, static preview)
also work out of the box.

## One-time setup

```powershell
cd 2D-3D-CAD-Test-Generation
python setup.py                      # checks Python, installs deps, creates .env
# then edit .env and set:  ANTHROPIC_API_KEY=sk-ant-...
```

---

## Web UI — the primary path

```powershell
cd 2D-3D-CAD-Test-Generation\webapp
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8092
# then open http://127.0.0.1:8092/
```

Three tabs, one flow, no folder editing:

1. **Tab 1 · Drawing Crop — open the drawing.** The full-tab cropper opens
   PDF, JPG/PNG, **and DWG/DXF/eDrawings directly** (CAD formats convert
   server-side automatically and appear in the cropper). Draw a box around
   each view and **Queue View**. Whatever is open here is mirrored to Tab 2's
   input-document box automatically. (Uploading on Tab 2 works too.)
2. **Tab 2 · Part Setup & 3D Model — orient, name, save.** Pull the queued
   crops (or upload), give each image an orientation (Front / Back / Top /
   Bottom / Left / Right / Isometric; ⟳ rotates sideways scans). Front + one
   more orthographic view are required to save. Optionally type **must-meet
   notes** (one requirement per line) — each line is graded against the built
   part and an unmet line blocks READY. The left box shows the input document
   with a format badge and sync indicator; the middle panel shows the part's
   **overview drawing**; the right box is the interactive 3D STL viewer.
3. **Tab 3 · Pipeline & Results — run.** Select the part, **▶ Pull & Run
   Pipeline**. Progress shows per stage with a live console and Cancel;
   results fill the sub-tabs live: extraction, build plan, verification,
   **Engineering Flags** (severity-ranked), **Token / Cost**, and **Files**.
   Outputs also land in `UI_Output/<Part>/` and
   `~/Downloads/SolidWorksModel_Parts/<Part>/` — same files everywhere.

Full UI docs: [`2D-3D-CAD-Test-Generation/README.md`](2D-3D-CAD-Test-Generation/README.md).

---

## CLI — the advanced / batch path (the command you use for test batches)

Each **part** is a subfolder of view images (front + side, optionally top). Layout:

```
MyTest/
├── PART-A/
│   ├── PART-A_front_view.png
│   ├── PART-A_side_view.png
│   ├── PART-A.png              (overview — extraction context AND the final
│   │                            cross-check: features it shows must be in the build)
│   └── notes.txt               (optional must-meet notes, one per line — graded
│                                met/partial/unmet; an unmet line gates READY)
├── PART-B/
│   └── ...
```

From inside `2D-3D-CAD-Test-Generation`, point `--views-folder` at the test folder
and send `--output` into a stable `output/` inside it (so the extraction cache
persists and re-runs are free):

```powershell
cd 2D-3D-CAD-Test-Generation
python main.py --views-folder ..\MyTest --output ..\MyTest\output
```

That single command resolves, verifies, writes macros + `.sldprt`, logs tokens,
and copies everything to `~/Downloads/SolidWorksModel_Parts`. It prints a summary
table and ends with `N/N READY`.

> Example used in this repo: `python main.py --views-folder ..\Test2 --output ..\Test2\output`

### Optional final step — one zip to grab/share

The pipeline copies parts to `~/Downloads/SolidWorksModel_Parts`; bundle them:

```powershell
Compress-Archive -Path "$env:USERPROFILE\Downloads\SolidWorksModel_Parts\*" `
  -DestinationPath "$env:USERPROFILE\Downloads\SolidWorksModel_Parts.zip" -Force
```

---

## What each part folder contains (output)

```
<Part>/
├── <Part>.SLDPRT                    # the 3D model (when SolidWorks is available)
├── <Part>.STL                       # STL export (3D viewer)
├── <Part>_engineering_review.txt    # severity-ranked review: CRITICAL → HIGH → MEDIUM → LOW
├── <Part>_model_check.txt           # mass/bbox validation + any skipped features
├── <Part>_extraction.json           # raw Claude extraction (verbatim)
├── <Part>_resolved_extraction.json  # Stage 2.5: resolved_value + flag tier per dim
├── <Part>_verification_report.txt   # READY/NOT READY + Overview Verification +
│                                    #   Human Requirements Compliance sections
├── <Part>_requirements.json         # must-meet notes graded met/partial/unmet/not_applicable
├── <Part>_build_plan.json           # self-contained steps (dims, positions, flags, review)
├── <Part>_audit_report.json         # static self-validation of the macros
└── macros/                          # 00_setup … ZZ_final_verify, RUN_ALL.vba, README.md
                                     # unsupported features appear as NN_Fxxx_MANUAL_*.vba
```

Plus, at the output root: `multiview_summary.csv`, `token_usage_log.txt`
(running API cost), and `.extraction_cache/` (internal — not exported).

**To build a part by hand on any SolidWorks machine:** copy the part folder over
and follow `macros/README.md` — run the numbered macros in order (or `RUN_ALL.vba`).

---

## Flags

| Flag | Effect |
|------|--------|
| `--views-folder DIR` | multi-view mode: each subfolder is a part (use this for test batches) |
| `--drawing FILE` | single drawing (PDF/PNG/JPG/TIFF) instead of a folder |
| `--batch DIR` | a flat folder of drawings / `*_extraction.json` |
| `--from-json FILE` | rebuild from a saved extraction — **no API cost** |
| `--output DIR` | where outputs go (keep stable to reuse the cache) |
| `--requirements FILE` | explicit must-meet notes file (else `notes.txt` in the part folder is auto-discovered) |
| `--skip-overview-check` | skip the final overview cross-check (auto-skips with a note when no overview exists) |
| `--skip-requirements-check` | skip grading the must-meet notes |
| `--no-resolve` | skip Stage 2.5 (legacy behavior) |
| `--strict-gate` | BLOCK on a failing verification instead of building with assumptions |
| `--no-sldprt` | macros + reports only, skip the `.sldprt` build |
| `--no-export` | don't copy to `~/Downloads/SolidWorksModel_Parts` |
| `--no-extract-cache` | force a fresh extraction (re-spends tokens) |

## Tests

```powershell
cd 2D-3D-CAD-Test-Generation
python -m pytest tests/ -q
```

---

## Reading the results

- **`*_engineering_review.txt` — read this first.** Every assumption, ambiguity
  resolution, and skipped/manual feature in one ranked list, most urgent first —
  plus the two final-check sections: **Overview Verification** (features the
  overview drawing shows, diffed against the build) and **Human-Specified
  Requirements** (your notes graded met/partial/unmet/not_applicable). Each
  item states what was ambiguous, the decision made, why, and what it affects.
  Also shown in the UI's Engineering Flags tab as labeled groups.
- **Summary table / `multiview_summary.csv`** — per part: status (READY, or
  **NOT READY** when a CRITICAL overview gap / unmet requirement gated it —
  the model and macros are still produced), readiness %, macro count, features
  needing review, features skipped.
- **`*_build_plan.json` → `resolution_summary` / `engineering_review`** — the
  same review as structured data, plus per-step `flags[]` with actionable notes.
- **`*_model_check.txt`** — if the `.sldprt` skipped a feature (e.g. an
  interactive fillet, or a hole with degenerate/edge-case data), it's listed here
  with the reason. The macros still contain those features as steps.
- **`token_usage_log.txt`** — tokens and dollar cost per API call, per part, and
  the running total.
