# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MTI 2D→3D pipeline: converts 2D engineering drawings (PDF/PNG/JPG/DWG/DXF/eDrawings) into SolidWorks 2024 parts via Claude Vision extraction → ambiguity resolution → verification → VBA macros → `.sldprt`/`.stl` (COM, Windows-only). All code lives in `2D-3D-CAD-Test-Generation/`; sibling top-level folders (`Test2/`, `E2ETest/`, `DrawingPDFs/`, etc.) are test drawing sets.

## Commands

`python` is often NOT on PATH on this machine — use the webapp venv interpreter:
`2D-3D-CAD-Test-Generation\webapp\.venv\Scripts\python.exe`.

All commands run from `2D-3D-CAD-Test-Generation/`:

```powershell
# Tests (conftest.py adds the project root to sys.path)
python -m pytest tests/ -q
python -m pytest tests/test_multiview.py -q          # single suite

# Full pipeline on a batch of part folders (the standard test-batch command)
python main.py --views-folder ..\Test2 --output ..\Test2\output

# Single drawing; extract+verify only (no SolidWorks, cheapest live check)
python main.py --drawing path\to\drawing.pdf --validate-only --debug

# Rebuild from a saved extraction — zero API cost
python main.py --from-json <Part>_extraction.json --output .\output

# Web UI (FastAPI on http://127.0.0.1:8092/) — creates .venv + installs pinned deps on first run
cd webapp; .\run.ps1
```

Useful `main.py` flags: `--no-sldprt` (macros/reports only), `--no-export` (skip copy to `~/Downloads/SolidWorksModel_Parts`), `--no-extract-cache` (force paid re-extraction), `--skip-overview-check` / `--skip-requirements-check` (bypass the final READY gates), `--strict-gate` (block on failing verification). Exit codes: 0 = all parts READY, 8 = completed but not all READY, 2 = bad args.

Setup: `python setup.py`, then put `ANTHROPIC_API_KEY` in `.env` (gitignored). Optional env: `EXTRACTION_MODEL` (default `claude-sonnet-5`), `SOLIDWORKS_TEMPLATE_PATH` (must point at a real `.prtdot`), `MAX_IMAGE_LONG_EDGE`.

**Web UI dev gotcha:** after editing `webapp/app.py` or pipeline code, restart the uvicorn server on 8092 — it otherwise serves stale endpoints.

## Architecture

### Pipeline (`2D-3D-CAD-Test-Generation/pipeline/`, orchestrated by `main.py`)

Stage order, one module per stage:

1. `utils/image_prep.py` — normalize/downscale input images.
2. `extractor.py` — one Claude Vision call per part (all views labeled, forced tool call, Pydantic v2 schema in `schema.py`). **Specs-first:** operator must-meet specifications are injected into the extraction prompt so the model actively looks for those features from the start; the spec text is part of the cache key (changed specs force a fresh extraction). Raw extraction JSON is always saved; an on-disk cache (`<output>/.extraction_cache/`) makes identical re-runs free. Token/USD costs append to `token_usage_log.txt` via `usage_log.py`.
3. `vector_extract/` + `hole_resolution.py` — exact hole positions from the original vector file (DXF/DWG entities via ezdxf, vector-PDF Bézier circles via PyMuPDF, HoughCircles raster fallback). **Precedence rule: vector geometry owns position, the vision callout owns semantics (diameter/thread/depth); disagreement keeps both and flags CRITICAL.** Each hole carries `position_source` and `position_confidence`.
4. `resolver.py` — Stage 2.5, the core design decision: **the pipeline never blocks on ambiguity**. Every unclear dimension gets a numeric `resolved_value` chosen from extracted candidates (never fabricated) via a deterministic tree (spec-driven → arithmetic chain → geometric validity → conservative geometry → last-resort default), tagged HIGH/MEDIUM/LOW/CRITICAL. **Specs-first:** an operator must-meet spec value that clarifies an ambiguous reading takes precedence (Step 0, `assumption_basis="spec_driven"`).
5. `validator.py` — arithmetic/envelope verification, advisory by default (`--strict-gate` makes it blocking).
6. `macro_generator.py` + `macro_audit.py` — numbered VBA macros (`00_setup` … `ZZZ_export_stl`, `RUN_ALL.vba`); prohibited features (loft/sweep/shell/etc.) become `NN_Fxxx_MANUAL_*.vba` steps; every macro is statically audited before writing (banned APIs fail generation).
7. `solidworks_builder.py` + `model_validator.py` — Windows-only COM build of `.sldprt` + STL export + mass/bbox check. Everything upstream runs on any OS.
8. Final checks that gate READY (status only — outputs are still produced): `overview_check.py` re-examines the part's overview drawing alone and diffs it against the build (missing visible feature = CRITICAL); `requirements_check.py` grades operator must-meet notes (`notes.txt` / `--requirements`) met/partial/unmet — an unmet line gates READY. (Those same notes were already applied specs-first in stages 2 & 4; this is the final re-grade against the built part.)
9. `engineering_review.py` — the single severity-ranked human report (`<Part>_engineering_review.txt`), regenerated after the COM build so skipped features are included. This is the canonical "what needs human attention" surface.

Multi-view input (`--views-folder`): each part is a folder of per-view images; view role comes from filename keywords (`front`/`side`/`top`…) or a leading `01`–`05`; a file named `full`/`overview`/`isometric` or matching the folder name is overview context only (not built as a plane). Front + one other orthographic view required.

### Web UI (`2D-3D-CAD-Test-Generation/webapp/`)

`app.py` (FastAPI, port 8092) is a wrapper that **runs `main.py` as a subprocess** (`--views-folder <part> --output <part>/output`) and streams its console — the CLI is the single source of truth for pipeline behavior. Frontend is a single `index.html` with three tabs (crop → part setup/3D viewer → run/results); `bridge.js` hooks the vendored DrawingCrop app (`photoapp/`, sources untouched) into the server. All JS assets are vendored (`vendor/`: Three.js, STLLoader; `photoapp/`: pdf.js) — no CDN, and Python deps are pinned; keep it that way for reproducible clones. Saved parts land in `webapp/parts/<session>/<part>/` in the exact `--views-folder` layout, so the CLI can run them unchanged. DWG/DXF/eDrawings conversion goes through `/api/convert-dwg` (engine chain: ezdwg → SolidWorks translator → ODA), cached in `.convert_cache/` and logged to `conversion_log.jsonl`.

Subprocess stdout must be decoded as UTF-8 with replacement (the pipeline's `rich` output crashed the cp1252 reader once); don't reintroduce locale decoding.

### Outputs

Per part under `<output>/<Part>/`: `.SLDPRT`, `.STL`, `_engineering_review.txt` (read first), `_extraction.json` (raw, never lost), `_resolved_extraction.json`, `_verification_report.txt`, `_requirements.json`, `_build_plan.json` (self-contained steps + flags), `_audit_report.json`, `_model_check.txt`, `macros/`. Successful runs are also copied to `UI_Output/<Part>/` (gitignored) and `~/Downloads/SolidWorksModel_Parts/<Part>/`.

## Conventions

- Guiding principle (from the README): *a complete approximate model is always the correct outcome; an incomplete model is always the wrong outcome* — resolve and flag, never block or silently drop.
- Numbers are chosen from extracted candidates, never invented; compliance grades are never fabricated (non-geometric notes → `not_applicable`).
- Extraction JSON is backward-compatible: new fields are additive; old JSONs must keep loading (`--from-json`).
- Never commit `.env` or a real API key; `.env.template` stays keyless.
