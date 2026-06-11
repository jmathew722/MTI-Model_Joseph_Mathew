# 2D → 3D SolidWorks Pipeline — Design

**Date:** 2026-06-11
**Owner:** Joe Mathew — iNDustry Labs / MTI Welding
**Target runtime:** SolidWorks 2024, Python 3.10+, Windows (extraction half is cross-platform)

## Goal

Take a 2D engineering drawing (image or PDF), extract every dimension / tolerance /
view / feature with the Claude Vision API, validate it into a strict schema, then
drive the SolidWorks 2024 COM API to build and verify the parametric 3D part.

## Architecture

Linear pipeline, one module per stage, orchestrated by `main.py`:

```
drawing file
  → utils/image_prep.py        prepare_image()      → base64 image(s)
  → pipeline/extractor.py      extract_drawing_data() → dict (Claude Vision)
  → pipeline/schema.py         DrawingData            → validated Pydantic model
  → pipeline/validator.py      validate_drawing_data()→ build-readiness checks
  → pipeline/solidworks_builder.py build_model()      → .sldprt  (Windows only)
  → pipeline/model_validator.py validate_model()      → validation report
```

Support modules: `utils/unit_converter.py` (all units → meters for the SW API),
`utils/logger.py` (structured logging).

### Module boundaries

- **image_prep** — accepts jpg/jpeg/png/pdf/tif/tiff. PDF→image via `pdf2image`,
  fallback `PyMuPDF`. Resize to ≤2576px longest edge, RGB-normalize, contrast-enhance
  low-quality scans, invert dark-background drawings, reject <100×100px, warn on blank
  pages. Returns base64 PNG.
- **schema** — Pydantic v2 models (`DrawingData`, `Dimension`, `Feature`, `View`,
  `GeometricTolerance`). Single source of truth: used both as the Claude structured-output
  schema and for post-extraction validation.
- **extractor** — `claude-opus-4-8` via `client.messages.parse()` with
  `output_config.format` = the `DrawingData` JSON schema. Guarantees schema-valid JSON
  (no regex fallback). SDK handles API retry/backoff. Domain-specific re-query when
  `confidence < 0.7`.
- **validator** — build-readiness checks beyond type validation: ≥1 base feature,
  no zero/negative dims, build_order feature deps satisfied, first feature is an
  extrude_boss, unit consistency, sketch-definability heuristic. Hard-fails with a
  clear report before SolidWorks runs.
- **unit_converter** — `to_meters(value, unit)` / `to_radians(deg)`. Every value
  through `to_meters()` before any SW call, asserted.
- **solidworks_builder** — `win32com`/`pythoncom` imported *lazily inside functions*
  so the module imports on macOS and only errors on actual use. Per-feature builders
  (extrude_boss, extrude_cut, hole, fillet, chamfer, pattern), `check_rebuild_errors`
  after every feature, fillet/chamfer wrapped in try/except, partial-model save +
  periodic auto-save on crash.
- **model_validator** — mass-property / bounding-box checks against extracted dims.

## Key decisions (deviations from the original spec)

1. **Model `claude-opus-4-8`** (current most-capable), not `claude-opus-4-5`.
2. **Structured outputs** (`messages.parse` + Pydantic) replace the prompt-only
   "return raw JSON" + regex-fallback approach. Schema validity is guaranteed.
3. **2576px** image prep (Opus 4.8 high-res vision) instead of 1568px — fine
   dimension text legibility.
4. **SDK-native retry** (`max_retries`) instead of a hand-rolled backoff loop; keep
   the confidence-based re-query.
5. **Cross-platform import safety** — non-SW pipeline + tests run on macOS; SW half
   activates on Windows. `setup.py` reports SW/pywin32 checks as SKIPPED off-Windows.

## Error handling

Every SW API call checks its return value for `None`. `check_rebuild_errors()` after
every feature. Fillet/chamfer failures are caught and demoted to warnings. Partial
model saved on any mid-build crash; auto-save every 3 features.

## Testing

pytest suite: `test_unit_converter.py`, `test_validator.py`, `test_extractor.py`
(Claude API mocked). The platform-independent half (image_prep, extractor logic,
validator, units, schema) is run and verified on macOS. The SolidWorks builder and
model_validator are verified by inspection only until run on Windows + SolidWorks 2024.

## Known limitations

- Checkpoint **resume** (re-entering a partial build) is not implemented — only
  partial-save-on-crash + periodic auto-save. Documented, not built, due to the
  brittleness of persisting/restoring SW feature-tree state.
- SolidWorks half is untestable in this (macOS) environment.
