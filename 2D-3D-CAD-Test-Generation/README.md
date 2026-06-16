# 2D → 3D SolidWorks Pipeline

Convert a 2D engineering drawing (image or PDF) into a parametric SolidWorks 2024
part in two phases:

- **Phase 1 — Extraction & Verification** (runs on any OS): extract every
  dimension, tolerance, view, hole callout, and geometric relationship with the
  Claude Vision API, then arithmetically verify it (dimensional closure, pattern
  envelopes, unit consistency, ambiguity flags). Output: a `VERIFICATION REPORT`
  with **READY TO BUILD / BLOCKED** status. BLOCKED = nothing gets built.
- **Phase 2 — Build**: generate numbered **SolidWorks VBA macros** (default) that
  you run inside SolidWorks on any machine — *no Python needed there* — or drive
  SolidWorks directly over COM (`--engine com`, Windows only).

## Pipeline

```
drawing → image_prep → extractor (Claude) → verification gate → macro generator → macros/*.vba
                                                  │                                    (run in SolidWorks)
                                                  └→ (--engine com) solidworks_builder → .sldprt
```

| Stage | Module | Runs on |
|-------|--------|---------|
| Image prep | `utils/image_prep.py` | any OS |
| Extraction | `pipeline/extractor.py` (`claude-sonnet-4-6`, forced tool call) | any OS |
| Schema | `pipeline/schema.py` (Pydantic v2; views, hole callouts, relationships, ambiguity) | any OS |
| Verification | `pipeline/validator.py` (closure, envelopes, READY/BLOCKED report) | any OS |
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

# Direct COM build (Windows + SolidWorks 2024):
python main.py --drawing path/to/drawing.pdf --engine com

# Tests:
pytest tests/ -v
```

Flags: `--drawing` or `--from-json` (one required), `--output`, `--page N`,
`--debug`, `--engine vba|com` (default `vba`), `--validate-only`.

## Output package (engine `vba`)

```
output/<PartNumber>/
├── <PartNumber>_extraction.json          # full Phase 1 extraction (saved even when BLOCKED)
├── <PartNumber>_verification_report.txt  # READY TO BUILD / BLOCKED + Phase-4 readiness score
├── <PartNumber>_build_plan.json          # ordered steps + skipped/needs-review + audit summary
├── <PartNumber>_audit_report.json        # static self-validation of the generated macros
├── macros/                               # 00_setup … ZZ_final_verify + README.md
└── logs/                                 # build_log.txt appended by the macros
```

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
- **Units:** SolidWorks API works in meters. Python COM path: every value
  through `to_meters()` + `assert_meters()`. VBA path: every value written as
  `<drawing value> * UNIT_FACTOR` for traceability.
- **Verification gate:** ambiguous dimensions (`resolution_required`),
  non-closing dimension chains, and infeasible patterns **block** the build.
- **Macro discipline:** one macro per feature, named features, per-step
  PASS/FAIL logging, stop-on-first-failure, fillets/chamfers last (interactive
  edge selection with extracted values baked in).
- **Prohibited features** (loft, sweep, boundary, shell, draft, surfacing,
  helical threads): never generated — flagged in the build plan and skipped.
  Threads are cosmetic only.

## Limitations

- Feature/hole **positions** are only as good as the drawing callouts: when a
  position isn't dimensioned from the origin, macros center geometry and mark it
  `POSITION ASSUMED` — verify before trusting the model.
- Revolves and feature-level patterns are emitted as TODO-marked skeletons
  (`needs_review` in the build plan) rather than guessed API calls.
- The COM build path (`--engine com`) and `ZZ_final_verify` macro are exercised
  only on Windows + SolidWorks 2024.
- Checkpoint *resume* for the COM path is not implemented (partial-save +
  auto-save only).
