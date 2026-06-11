# 2D → 3D SolidWorks Pipeline

Convert a 2D engineering drawing (image or PDF) into a parametric SolidWorks 2024
part: extract dimensions with the Claude Vision API, validate them into a strict
schema, build the 3D model via the SolidWorks COM API, and verify the result.

## Pipeline

```
drawing → image_prep → extractor (Claude) → schema/validator → solidworks_builder → model_validator → .sldprt
```

| Stage | Module | Runs on |
|-------|--------|---------|
| Image prep | `utils/image_prep.py` | any OS |
| Extraction | `pipeline/extractor.py` (`claude-opus-4-8`, structured outputs) | any OS |
| Schema | `pipeline/schema.py` (Pydantic v2) | any OS |
| Validation | `pipeline/validator.py` | any OS |
| Units | `utils/unit_converter.py` (everything → meters) | any OS |
| Build | `pipeline/solidworks_builder.py` (COM) | **Windows + SolidWorks 2024** |
| Model check | `pipeline/model_validator.py` | **Windows + SolidWorks 2024** |

The SolidWorks half imports cleanly everywhere but only runs on Windows; the
extraction/validation half runs and is tested on any platform.

## Setup

```bash
python setup.py                 # checks Python, installs deps, creates .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
# Extract + validate only (no SolidWorks needed — runs anywhere):
python main.py --drawing path/to/drawing.pdf --validate-only --debug

# Full pipeline (Windows + SolidWorks 2024):
python main.py --drawing path/to/drawing.pdf --output ./output

# Tests:
pytest tests/ -v
```

Flags: `--drawing` (required), `--output`, `--page N` (multi-page PDFs),
`--debug` (saves `debug_extraction.json`), `--validate-only`.

## Key design notes

- **Model:** `claude-opus-4-8` with **structured outputs** — the response is
  forced to conform to the `DrawingData` Pydantic schema, so JSON validity is
  guaranteed (no regex repair).
- **Units:** every linear value passes through `to_meters()` and is gated by
  `assert_meters()` before reaching the COM API (SolidWorks works in meters).
- **Robustness:** rebuild errors checked after every feature; fillets/chamfers
  wrapped in try/except and demoted to warnings; partial model saved on crash;
  auto-save every 3 features.

## Limitations

- Checkpoint **resume** (re-entering a partial build) is not implemented — only
  partial-save-on-crash + periodic auto-save.
- The SolidWorks build/validate stages are verified by inspection on non-Windows
  machines; run them on Windows + SolidWorks 2024 to exercise the COM paths.
- Hole Wizard variants (counterbore/countersink/threaded) fall back to a simple
  circular cut until richer callout data is available.
