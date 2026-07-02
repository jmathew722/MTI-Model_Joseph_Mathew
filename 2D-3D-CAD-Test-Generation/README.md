# 2D → 3D SolidWorks Pipeline

Convert a 2D engineering drawing (image or PDF) into a parametric SolidWorks 2024
part — through a **web UI** or the **command line**.

- **Web UI** (`webapp/`): preprocess a drawing and crop its views in the embedded
  DrawingCrop tool (Tab 1), add one or more **parts**, run the full pipeline on a
  single selected part, watch it live, and inspect every output — including a 3D
  **STL viewer** — in dedicated tabs.
- **CLI** (`main.py`): the same pipeline as a scriptable command (single drawing,
  batch folder, or multi-view part folders).

The pipeline runs in phases:

- **Phase 1 — Extraction & Verification** (any OS): extract every dimension,
  tolerance, view, hole callout, and geometric relationship with the Claude Vision
  API, then arithmetically verify it (dimensional closure, pattern envelopes, unit
  consistency, ambiguity flags). Output: a `VERIFICATION REPORT`.
- **Stage 2.5 — Ambiguity Resolution** (the *chief-engineer pass*, any OS, on by
  default): every ambiguous or under-dimensioned value is resolved to the best
  defensible number (arithmetic-chain closure → geometric validity → conservative
  geometry → last resort), and every feature is marked **build**. Nothing is left
  unresolved and the build never blocks on ambiguity — each assumption is annotated
  with a confidence **flag tier** (HIGH/MEDIUM/LOW/CRITICAL) and a human note.
  *A complete approximate model beats an incomplete one.*
- **Phase 2 — Build**: generate numbered **SolidWorks VBA macros** (default, any
  OS — run them on any SolidWorks machine, no Python needed there), or drive
  SolidWorks directly over COM (`--engine com`, Windows only). When SolidWorks is
  available a real **`.sldprt`** is built, and now an **`.stl`** is exported
  alongside it (or via the final macro) for the 3D viewer.

## Pipeline

```
drawing → image_prep → extractor (Claude) → resolver (Stage 2.5) → verification → macro generator → macros/*.vba
                                                  │                                                       (run in SolidWorks)
                                                  └→ (COM, when SolidWorks is available) solidworks_builder → .sldprt → .stl
```

| Stage | Module | Runs on |
|-------|--------|---------|
| Image prep | `utils/image_prep.py` | any OS |
| Extraction | `pipeline/extractor.py` (`claude-sonnet-4-6`, forced tool call) | any OS |
| Schema | `pipeline/schema.py` (Pydantic v2; views, hole callouts, relationships, ambiguity) | any OS |
| **Ambiguity resolution** | `pipeline/resolver.py` (resolved_value + flag tier per dim; never blocks) | any OS |
| Verification | `pipeline/validator.py` (closure, envelopes, advisory report) | any OS |
| **VBA macros** | `pipeline/macro_generator.py` (incl. `ZZZ_export_stl.vba`) | any OS (macros run on any SolidWorks machine) |
| COM build | `pipeline/solidworks_builder.py` (`.sldprt` + `export_stl` → `.stl`) | Windows + SolidWorks 2024 |
| Model check | `pipeline/model_validator.py` | Windows + SolidWorks 2024 |

---

## Web UI (`webapp/`)

A single-page, tabbed front end that drives `main.py` as a subprocess (the proven
CLI is untouched), streams its console live, and renders every output file.

### Launch

```bash
cd webapp
./run.sh                 # creates a venv, installs deps, serves http://127.0.0.1:8092
# or, if the venv already exists:
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8092
```

Then open <http://127.0.0.1:8092/>. Live extraction needs `ANTHROPIC_API_KEY` in
`../.env` (see Setup); without it, use the **Run demo** button, which runs the
pipeline from a saved extraction (no API call).

### Tabs

1. **Image Preprocessing** — the DrawingCrop photo app, embedded **verbatim and
   unmodified**. Open a PDF or image, draw bounding boxes, and queue each as a
   named view crop. (DWG is not yet supported — it needs an external converter;
   use PDF or an image.) Nothing is analyzed here; it only displays and crops.
2. **Drawing vs 3D Model** — a split panel: the selected part's source drawing on
   the left, an interactive **Three.js STL viewer** (rotate / zoom / pan) on the
   right. Both populate automatically; the STL loads once the pipeline produces it.
3. **Extraction JSON · Resolved Extraction · Build Plan · Verification · Model
   Check · VBA Macros · Console** — each fills with the corresponding output file's
   contents the moment the pipeline writes it.

### Multi-part workflow (Tab 1)

The photo app works on one drawing at a time, so parts are committed out of it one
at a time:

1. Load a drawing, queue its view crops, click **➕ Add current part** — the crops
   (+ a source thumbnail) are saved as a part subfolder in the session.
2. Repeat for each drawing to build up a **part list** (cards with thumbnail, name,
   and view count).
3. **Select exactly one part** (its card highlights).
4. Click **▶ Run Pipeline** (enabled only when a part is selected and it has ≥1
   saved view). It runs scoped to **just that subfolder** — never the parent:

   ```
   python main.py --views-folder <part_folder> --output <part_folder>/output
   ```

   The button shows a **Running…** state, a live stdout **console** streams on the
   tab, and a success/failure message appears when it finishes.
5. The output tabs (3+) update for the part that ran. **Switching the selected
   part** shows that part's outputs if they exist, or *"Waiting for pipeline
   output…"* placeholders if it hasn't been run.

The photo app's files are served byte-for-byte from `webapp/photoapp/`; a small
parent-side `bridge.js` reads its queued crops without modifying it. Three.js and
pdf.js are vendored under `webapp/vendor/` and `webapp/photoapp/` — **no CDN or
uploads at runtime**.

> On macOS/Linux (no SolidWorks) the `.sldprt`, `.stl`, and Model Check are not
> produced; those tabs show a "requires Windows + SolidWorks" note, and the STL
> **export macro** is still generated so it can be run on a SolidWorks machine.

---

## Setup

```bash
python setup.py                 # checks Python, installs deps, creates .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

## Run (CLI)

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
plane and the part is built from them. This is exactly what the Web UI's per-part
Run uses (with the single selected part's folder).

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
  (The Web UI names committed crops this way automatically.)
- All of a part's views go to Claude in **one** call, labeled by view, so each
  feature's sketch plane comes from the view it was read in.
- If the folder holds images directly (no subfolders), it's treated as one part.
- Output per part is the usual `output/<Part>/` package, plus `multiview_summary.csv`.

Flags: `--drawing`, `--from-json`, `--batch`, or `--views-folder` (one required), `--output`,
`--page N`, `--debug`, `--engine vba|com` (default `vba`), `--validate-only`,
`--no-sldprt` (skip the default `.sldprt`/`.stl` build; emit macros + text only),
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
2. **Geometric validity** — eliminate readings that don't fit the part envelope →
   tier **MEDIUM**.
3. **Conservative geometry** — among survivors prefer the smallest/shallowest →
   tier **LOW**.
4. **Last resort** — derive from an adjacent dimension, default a missing depth to
   through-all, a missing radius to the general tolerance, or center on the parent
   → tier **CRITICAL**.

Every dimension ends with a numeric `resolved_value`; every feature is marked
`build_status: build`. Each assumption carries `assumption_basis`,
`assumption_confidence`, `flag_tier`, and an ID-naming `human_note`. The numbers
are **chosen from what was extracted** — never fabricated. The macro generator
turns each flag into VBA by tier: **HIGH** → a `' NOTE` comment, **MEDIUM** →
`MsgBox vbInformation`, **LOW** → `MsgBox vbExclamation`, **CRITICAL** → a banner +
a confirmation dialog the operator must acknowledge (Cancel logs and stops).

The `build_plan.json` is **self-contained**: coordinate convention, every step's
dimensions in *both* drawing units and meters, positions in both, `flags[]`,
fillet/chamfer edge strategy, and per-step assumption tiers, plus a
`resolution_summary` — so a macro generator can build any step without re-reading
the extraction JSON.

## Output package (engine `vba`)

```
output/<PartNumber>/
├── <PartNumber>.sldprt                     # the 3D model — built by default when SolidWorks is available
├── <PartNumber>.stl                        # STL export of the model (for the 3D viewer) — with the .sldprt
├── <PartNumber>_model_check.txt            # mass/bounding-box validation + any skipped features
├── <PartNumber>_extraction.json            # RAW Phase 1 extraction, verbatim (saved even when BLOCKED)
├── <PartNumber>_resolved_extraction.json   # Stage 2.5: every dim's resolved_value + flag tier + human note
├── <PartNumber>_verification_report.txt    # verification report + Phase-4 readiness score
├── <PartNumber>_build_plan.json            # SELF-CONTAINED steps + resolution summary
├── <PartNumber>_audit_report.json          # static self-validation of the generated macros
├── macros/                                 # 00_setup … ZZ_final_verify, ZZZ_export_stl, RUN_ALL.vba, README.md
└── logs/                                   # build_log.txt appended by the macros
```

**The `.sldprt` and `.stl` are outputs of every SolidWorks-enabled run.** Whenever
the pipeline runs on a machine with SolidWorks 2024 over COM (any mode:
`--drawing`, `--batch`, `--views-folder`), each READY part is built into a real
`.sldprt` **and** exported to `.stl` (same base name) in its own folder — no
separate step. The build is non-strict: a fragile feature is skipped and recorded
in `_model_check.txt` rather than failing the part. If SolidWorks is unavailable
the run still produces the text reports + macros, and the final macro
**`ZZZ_export_stl.vba`** (also included in `RUN_ALL.vba`) exports the `.stl` when
the macros are run on a SolidWorks machine. Pass `--no-sldprt` to emit macros only.
BLOCKED parts are never built.

**Final step — Downloads delivery.** As the last step of every run, all part
outputs are copied into `~/Downloads/SolidWorksModel_Parts` so the deliverables
land in one well-known place (updated in place on re-runs). Pass `--no-export` to
skip.

The extraction JSON is written for **every** run so a paid extraction is never
lost — patch it and regenerate with `--from-json` (no API cost). Before any macro
is written, every `.vba` is **statically self-validated**
(`pipeline/macro_audit.py`): banned/nonexistent APIs and structural defects fail
generation outright.

## Key design notes

- **Extraction:** `claude-sonnet-4-6` (override with `EXTRACTION_MODEL`) via a
  **forced tool call** validated against the Pydantic schema with one repair retry.
- **Token economy:** static prompt + tool schema and the image carry
  `cache_control`; an on-disk **extraction cache** (`<output>/.extraction_cache`,
  disable with `--no-extract-cache`) returns identical results with **zero** API
  calls. Per-extraction token usage is logged.
- **Units:** SolidWorks API works in meters. COM path: every value through
  `to_meters()` + `assert_meters()`. VBA path: every value written as
  `<drawing value> * UNIT_FACTOR` for traceability.
- **STL export:** `solidworks_builder.export_stl()` runs right after the `.sldprt`
  save (extension-driven `SaveAs3`); the macro path emits `ZZZ_export_stl.vba` that
  writes `<part>.stl` next to the saved part. Non-fatal — an STL failure never
  invalidates a good `.sldprt`.
- **Verification gate:** advisory by default (Stage 2.5 resolves and the build
  proceeds with annotated assumptions); `--strict-gate` / `--no-resolve` restore
  the v2 blocking behavior.
- **Macro discipline:** one macro per feature, named features, per-step PASS/FAIL
  logging, stop-on-first-failure, fillets/chamfers last.
- **Prohibited features** (loft, sweep, boundary, shell, draft, surfacing, helical
  threads): never generated — flagged and skipped. Threads are cosmetic only.

## Limitations

- Feature/hole **positions** are only as good as the drawing callouts: an
  undimensioned position is centered and flagged `POSITION ASSUMED` (tier LOW).
- Stage 2.5 **resolves rather than blocks**: a CRITICAL-tier value is a defensible
  default, not a confirmed reading. Review the `resolution_summary` and any
  MEDIUM/LOW/CRITICAL flags before relying on the model.
- Revolves and feature-level patterns are emitted as TODO-marked skeletons.
- The COM build path (`--engine com`), `ZZ_final_verify`, `ZZZ_export_stl`, the
  `.sldprt`/`.stl` outputs, and Tab 2's 3D viewer content are exercised only on
  Windows + SolidWorks 2024. **DWG input** is not yet supported (needs an external
  converter).
- Checkpoint *resume* for the COM path is not implemented (partial/auto-save only).
```
