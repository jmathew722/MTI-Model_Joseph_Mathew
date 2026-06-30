# 2D → 3D SolidWorks Pipeline — Design

**Date:** 2026-06-11 (v1) · **Updated:** 2026-06-12 (v2 — two-phase + VBA macro generation) · 2026-06-19 (v3 — Stage 2.5 ambiguity resolver + self-contained build plan)
**Owner:** Joe Mathew — iNDustry Labs / MTI Welding
**Target runtime:** SolidWorks 2024, Python 3.10+ (Phase 1 + macro generation run on any OS; macros run on any SolidWorks machine with zero installs)

## v3 summary (2026-06-19 — never block on ambiguity)

Adds **Stage 2.5 — Ambiguity Resolution** (`pipeline/resolver.py`) between
extraction and verification, inverting the v2 BLOCKED philosophy per the owner's
directive: *a complete approximate model is always the correct outcome; an
incomplete model is always the wrong outcome.*

- **Resolver (deterministic, any OS):** every dimension flagged
  `value_unclear`/`resolution_required`/unknown-position is resolved to a numeric
  `resolved_value` via a fixed decision tree — (1) arithmetic-chain closure, (2)
  geometric validity vs the part envelope, (3) conservative geometry (smallest/
  shallowest), (4) last resort (adjacent dim / through-all / general-tol radius /
  parent-center). Every feature is marked `build_status="build"`. Each assumption
  carries `assumption_basis`, `assumption_confidence`, a `flag_tier`
  (HIGH/MEDIUM/LOW/CRITICAL) and an ID-naming `human_note`. Numbers are chosen
  from extracted candidates — never fabricated. Implemented deterministically (not
  a second LLM call): the algorithm is fully specified and a CAD pipeline needs
  reproducible, testable, value-by-rule resolution.
- **Gate is advisory by default.** Verification still runs and reports, but the
  resolver clears the soft-block conditions so the build proceeds.
  `--strict-gate` / `--no-resolve` restore v2 hard-blocking.
- **Schema discipline.** The resolver writes a rich annotated
  `<Part>_resolved_extraction.json` but keeps a `schema_clean()` twin (canonical
  fields only) so the strict `extra="forbid"` `DrawingData` still validates the
  data that drives verification + build. The raw `<Part>_extraction.json` is
  preserved verbatim.
- **Self-contained build plan.** `build_plan.json` gains a coordinate-convention
  header and, per step, dims in drawing units AND meters, `positions_xy` in both,
  `flags[]` with per-tier `macro_behavior`, the fillet/chamfer edge-selection
  contract, and per-step assumption metadata, plus a top-level
  `resolution_summary`. A macro generator can build any step from the step object
  alone.
- **Macro behavior by tier.** HIGH → `' NOTE` comment; MEDIUM → `MsgBox
  vbInformation`; LOW → `MsgBox vbExclamation`; CRITICAL → banner comment + a
  `vbOKCancel` confirmation dialog that exits the macro on Cancel.

## v2 summary (approved 2026-06-12)

Two-phase workflow extending v1 in place:

- **Phase 1 — Extraction & Verification** (any OS): image prep → Claude Vision
  (`claude-sonnet-4-6`, forced tool call) → expanded schema (views with
  dimension/feature visibility, hole callouts, GD&T/datum fields, ambiguity
  flags with candidate values, relationship map with dimension chains) →
  verification gate producing the spec-format `VERIFICATION REPORT` with
  **READY TO BUILD / BLOCKED**. BLOCKED stops everything.
- **Phase 2 — VBA macro generation** (default engine, any OS): emits
  `output/<Part>/` with `_extraction.json`, `_verification_report.txt`,
  `_build_plan.json`, numbered `macros/*.vba` (00_setup … ZZ_final_verify),
  and `logs/`. Macros run inside SolidWorks (no Python on that machine),
  log PASS/FAIL per step, stop on first failure. Prohibited features
  (loft/sweep/shell/…) are never generated — flagged and skipped. Threads are
  cosmetic-only. Fillets/chamfers are an interactive select-edges-then-run
  macro with the extracted values baked in. The v1 Python-COM builder remains
  available via `--engine com`.
- `--from-json` regenerates macros from a saved extraction without an API call.

Key v2 constraints carried from debugging (commit cdef0e1): strict structured
outputs fail on this schema (use forced tool call + Pydantic repair retry);
schema fields use non-null defaults; SolidWorks COM needs late-bound Dispatch +
NULL VARIANTs.

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
