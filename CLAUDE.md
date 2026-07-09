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
1.5. `overview_analysis.py` — Stage 1.5 "Holistic Overview Analysis": the FULL uncropped sheet (the Tab-1 "FULL OVERVIEW VIEW" image / `00_full.jpg`) goes to Claude Sonnet 5 with a RELATIONAL prompt (views on the sheet, cross-view feature correspondences like through-vs-blind, overall 3D shape, cross-view conflicts with severity+recommendation, symmetry, global notes like "(6) HLS" with `resolved_count`) — it does NOT re-extract dimensions. Output `overview_analysis.json`; fed to Stage 2.5 as **priority tier 2** (tier 0 = must-meet specs, tier 1 = per-view extraction owns dimension values, tier 2 = overview owns cross-view relationships); every resolution/flag records `resolved_by_tier`. Conflicts + a deterministic callout-count cross-check become flags (`source: overview_analysis`, e.g. the A050211E 5-visible-vs-"(6) HLS" case → CRITICAL). Purely additive: no key / failure → stage skipped, pipeline unchanged; `--from-json` reuses a sibling `overview_analysis.json`. Token cost is its own ledger line, `stage: stage_1_5_overview_analysis`. Shown in the UI as the collapsible "Overview Analysis" panel under the Full Overview View render.
2. `extractor.py` — one Claude Vision call per part (all views labeled, forced tool call, Pydantic v2 schema in `schema.py`). **Specs-first:** operator must-meet specifications are injected into the extraction prompt so the model actively looks for those features from the start; the spec text is part of the cache key (changed specs force a fresh extraction). Raw extraction JSON is always saved; an on-disk cache (`<output>/.extraction_cache/`) makes identical re-runs free. Token/USD costs append to `token_usage_log.txt` via `usage_log.py`.
3. `vector_extract/` + `hole_resolution.py` — exact hole positions from the original vector file (DXF/DWG entities via ezdxf, vector-PDF Bézier circles via PyMuPDF, HoughCircles raster fallback). **Precedence rule: vector geometry owns position, the vision callout owns semantics (diameter/thread/depth); disagreement keeps both and flags CRITICAL.** Each hole carries `position_source` and `position_confidence`.
4. `must_meet.py` — Stage 2.6 "Spec Reconciliation": the operator's MUST-MEET text (`must_meet_spec.txt`, the amber box in the UI; legacy `notes.txt` still works) is parsed into structured `MM-xxx` constraints (`must_meet_constraints.json`) via a dedicated Claude call with a deterministic regex fallback (no key needed). **Constraints are priority tier 0 — they override vision-extracted values on any conflict**; every conflict goes to `lessons_learned.jsonl` (`resolution: spec_override`), never silently dropped. Missing geometry parameters are derived from extraction (bolt-circle fit: radius = mean √(x²+y²) about the hole centroid; radii disagreeing > 0.005 in = CRITICAL). Exception: a spec hole COUNT contradicting explicitly dimensioned drawing positions keeps the drawing's geometry, flags CRITICAL, and lets the MM check fail with measured-vs-required (`spec_vs_drawing_disagreement`).
5. `resolver.py` — Stage 2.5, the core design decision: **the pipeline never blocks on ambiguity**. Every unclear dimension gets a numeric `resolved_value` chosen from extracted candidates (never fabricated) via a deterministic tree (spec-driven → arithmetic chain → geometric validity → conservative geometry → last-resort default), tagged HIGH/MEDIUM/LOW/CRITICAL. **Specs-first:** an operator must-meet spec value that clarifies an ambiguous reading takes precedence (Step 0, `assumption_basis="spec_driven"`). Stage 2.6's derived values + conflicts land in `resolved_extraction.json` under `must_meet`.
6. `validator.py` — arithmetic/envelope verification, advisory by default (`--strict-gate` makes it blocking).
7. `macro_generator.py` + `macro_audit.py` — numbered VBA macros (`00_setup` … `ZZZ_export_stl`, `RUN_ALL.vba`); prohibited features (loft/sweep/shell/etc.) become `NN_Fxxx_MANUAL_*.vba` steps; every macro is statically audited before writing (banned APIs fail generation). **Circular-pattern reliability layer:** a hole group routed to `circular_pattern` (spec says so, or the drawing dimensions polar-style; requires a concentric bore to derive the axis) emits three macros — seed hole (`Fxxx_SeedHoleCut`) → named reference axis (`PatternAxisN`, `InsertAxis2` from the bore cylindrical face) → the pattern via the single `CreateCircularPatternSafe` VBA helper (version-pinned `FeatureCircularPattern5` signature from the installed sldworks.tlb, fallback to `...4`; axis Mark=1, seed Mark=4; Nothing-check + hard stop). `total_instances` INCLUDES the seed — asserted once in the canonical build-plan schema (`circular_pattern` step; generation REFUSES if any canonical field is null). Every feature outcome is appended to `logs/macro_result.json` (JSON Lines) by the macros.
8. `cq_prevalidate.py` — CadQuery pre-validation: builds the same geometry headlessly from `build_plan.json` (single source of truth; circular patterns via `.polarArray(radius, seedAngle, 360, count)` + `cutThruAll`), checks watertightness/volume/hole counts against the MM constraints, and writes `prevalidation.stl` + `prevalidation_report.json` + a per-run `prevalidate.py`. **A failed check aborts the SolidWorks build** and surfaces the exact constraint (`MM-001 FAILED: …`). Graceful no-op when cadquery isn't installed.
9. `solidworks_builder.py` + `model_validator.py` — Windows-only COM build of `.sldprt` + STL export + mass/bbox check. Same circular-pattern trio as the VBA path (`build_circular_pattern_holes`); features are renamed deterministically right after creation; per-feature outcomes are written to `macro_result.json` so a failure surfaces as the exact feature, never a generic exit code. Everything upstream runs on any OS.
10. `constraint_verify.py` — post-build must-meet verification: the built STL is measured with trimesh (cross-section circle fitting; through-all = the hole appears near both faces) and every MM constraint is graded PASS/FAIL with measured vs required into `constraint_verification.json`. **A run with MM constraints is only READY when every constraint passes**; each failure is appended to `lessons_learned.jsonl` with the responsible VBA snippet.
11. Final checks that gate READY (status only — outputs are still produced): `overview_check.py` re-examines the part's overview drawing alone and diffs it against the build (missing visible feature = CRITICAL); `requirements_check.py` grades operator must-meet notes (`notes.txt` / `--requirements`) met/partial/unmet — an unmet line gates READY. (Those same notes were already applied specs-first in stages 2 & 4; this is the final re-grade against the built part.)
12. `engineering_review.py` — the single severity-ranked human report (`<Part>_engineering_review.txt`), regenerated after the COM build so skipped features are included. This is the canonical "what needs human attention" surface.

Multi-view input (`--views-folder`): each part is a folder of per-view images; view role comes from filename keywords (`front`/`side`/`top`…) or a leading `01`–`05`; a file named `full`/`overview`/`isometric` or matching the folder name is overview context only (not built as a plane). Front + one other orthographic view required.

### Web UI (`2D-3D-CAD-Test-Generation/webapp/`)

`app.py` (FastAPI, port 8092) is a wrapper that **runs `main.py` as a subprocess** (`--views-folder <part> --output <part>/output`) and streams its console — the CLI is the single source of truth for pipeline behavior. Frontend is a single `index.html` with four sheet-tabs (Sheet 1 crop → Sheet 2 part setup/3D viewer → Sheet 3 pipeline run controls + live Overview Analysis panel → Sheet 4 run-outputs dock with the ten sub-tabs); `bridge.js` hooks the vendored DrawingCrop app (`photoapp/`, sources untouched) into the server. Sheet 2's "Select Model" dropdown and Sheet 4's "Select Run" dropdown are both backed by the single persistent `/api/run-history` endpoint (disk-scan of `webapp/parts/*/*/output` — survives restarts and sessions); each run's console transcript is persisted to `ui_console.log` in the run output so Sheet 4's Console works for historical runs. Picking a model on Sheet 2 also swaps the left panel to that part's overview drawing; Sheet 4's "✕ Clear all models" button (`POST /api/run-history/clear`) deletes every stored run output but never the saved part inputs or the delivered `UI_Output/`/Downloads copies. **Reference-region markup:** Sheet 1 has a mode bar (✂ Crop views ↔ ✎ Mark regions; the vendored cropper iframe stays untouched) whose markup surface lets a reviewer drag colored highlight boxes over the drawing before extraction (15-color palette defined once in `REGION_PALETTE`; color = feature group, e.g. a hole's center + X-dimension + Y-dimension share one color; optional role tag center/x-dimension/y-dimension/tolerance/other + transcribed value). Boxes store NORMALIZED 0-1 coordinates, persist per part as `reference_regions.json` in the part INPUT folder (GET/POST `/api/parts/{s}/{p}/regions`, survives clear-all-models), and the color groups render live as the "Marked reference regions" subsection inside the Overview Analysis panel. **The markup feeds extraction:** the client composites the drawing + boxes into `full_marked_view.jpg` (POST `/api/parts/{s}/{p}/marked-view`, removed when regions are cleared); `view_ingest` exposes it as `PartViews.marked_view` (never a sketch view), and `extract_drawing_data_multiview` includes it as an extra whole-part context image plus a text legend built from `reference_regions.json` (`main._region_legend_text`), so Claude places holes per the operator's boxes. The marked image + legend are part of the multiview extraction cache key (changed markup → fresh extraction). Downstream OCR cross-check / low-confidence fallback can build on the same data. All JS assets are vendored (`vendor/`: Three.js, STLLoader; `photoapp/`: pdf.js) — no CDN, and Python deps are pinned; keep it that way for reproducible clones. Saved parts land in `webapp/parts/<session>/<part>/` in the exact `--views-folder` layout, so the CLI can run them unchanged. DWG/DXF/eDrawings conversion goes through `/api/convert-dwg` (engine chain: ezdwg → SolidWorks translator → ODA), cached in `.convert_cache/` and logged to `conversion_log.jsonl`.

Subprocess stdout must be decoded as UTF-8 with replacement (the pipeline's `rich` output crashed the cp1252 reader once); don't reintroduce locale decoding.

### Outputs

Per part under `<output>/<Part>/`: `.SLDPRT`, `.STL`, `_engineering_review.txt` (read first), `_extraction.json` (raw, never lost), `overview_analysis.json` (Stage 1.5 holistic cross-view read), `_resolved_extraction.json`, `_verification_report.txt`, `_requirements.json`, `_build_plan.json` (self-contained steps + flags), `_audit_report.json`, `_model_check.txt`, `macros/`, plus the must-meet layer: `must_meet_spec.txt` (the operator's raw text, persisted with the run), `must_meet_constraints.json` (MM-001…), `prevalidation.stl` + `prevalidation_report.json` + `prevalidate.py` (CadQuery pre-check), `constraint_verification.json` (post-build PASS/FAIL, measured vs required), `macro_result.json` (feature → success/fail). `<output>/lessons_learned.jsonl` accumulates spec-override conflicts and constraint failures across runs. Successful runs are also copied to `UI_Output/<Part>/` (gitignored) and `~/Downloads/SolidWorksModel_Parts/<Part>/`.

## Conventions

- Guiding principle (from the README): *a complete approximate model is always the correct outcome; an incomplete model is always the wrong outcome* — resolve and flag, never block or silently drop.
- Numbers are chosen from extracted candidates, never invented; compliance grades are never fabricated (non-geometric notes → `not_applicable`).
- Extraction JSON is backward-compatible: new fields are additive; old JSONs must keep loading (`--from-json`).
- Never commit `.env` or a real API key; `.env.template` stays keyless.
