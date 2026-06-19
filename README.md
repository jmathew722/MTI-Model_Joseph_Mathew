# MTI 2D → 3D SolidWorks Pipeline — Run Guide

Convert a folder of 2D engineering drawings into **SolidWorks 2024 parts** —
extraction → ambiguity resolution → verification → VBA macros + a real `.sldprt`,
with token costs logged and all deliverables copied to your Downloads folder.

This page is the **operator run guide**. The deep technical docs live in
[`2D-3D-CAD-Test-Generation/README.md`](2D-3D-CAD-Test-Generation/README.md).
To have an agent run + verify a batch for you, use
[`RUN_PROMPT.md`](RUN_PROMPT.md).

---

## What one run does (the whole flow)

```
drawings → image prep → Claude Vision extract → Stage 2.5 resolve → verify
         → VBA macros + build_plan.json + resolved_extraction.json
         → .sldprt (SolidWorks COM)  → token ledger  → copy to ~/Downloads
```

- **Stage 2.5 (chief-engineer pass):** every ambiguous / under-dimensioned value
  is resolved to a defensible number and flagged HIGH/MEDIUM/LOW/CRITICAL — the
  build never blocks on ambiguity.
- **`.sldprt` is built by default** when SolidWorks 2024 is available (Windows).
  Off Windows / no SolidWorks, you still get macros + reports and a clear note.
- **Deliverables are copied to `~/Downloads/SolidWorksModel_Parts`** as the last
  step of every run.

---

## Prerequisites

| Need | For |
|------|-----|
| Python 3.10+ | the whole pipeline |
| `ANTHROPIC_API_KEY` | Claude Vision extraction (skipped on cache hits) |
| SolidWorks 2024 (Windows) | building the `.sldprt` (optional — macros work anywhere) |

## One-time setup

```powershell
cd 2D-3D-CAD-Test-Generation
python setup.py                      # checks Python, installs deps, creates .env
# then edit .env and set:  ANTHROPIC_API_KEY=sk-ant-...
```

---

## Run a full test (the command you use every time)

Each **part** is a subfolder of view images (front + side, optionally top). Layout:

```
MyTest/
├── PART-A/
│   ├── PART-A_front_view.png
│   ├── PART-A_side_view.png
│   └── PART-A.png              (overview — optional, ignored if unclassified)
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
├── <Part>_model_check.txt           # mass/bbox validation + any skipped features
├── <Part>_extraction.json           # raw Claude extraction (verbatim)
├── <Part>_resolved_extraction.json  # Stage 2.5: resolved_value + flag tier per dim
├── <Part>_verification_report.txt   # READY / completeness score
├── <Part>_build_plan.json           # self-contained steps (dims, positions, flags)
├── <Part>_audit_report.json         # static self-validation of the macros
└── macros/                          # 00_setup … ZZ_final_verify, RUN_ALL.vba, README.md
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

- **Summary table / `multiview_summary.csv`** — per part: status, readiness %,
  macro count, features needing review, features skipped.
- **`*_build_plan.json` → `resolution_summary`** — counts of assumptions and the
  plain-English narrative; per-step `flags[]` list every MEDIUM/LOW/CRITICAL
  assumption with an actionable note.
- **`*_model_check.txt`** — if the `.sldprt` skipped a feature (e.g. an
  interactive fillet, or a hole with degenerate/edge-case data), it's listed here
  with the reason. The macros still build those features.
