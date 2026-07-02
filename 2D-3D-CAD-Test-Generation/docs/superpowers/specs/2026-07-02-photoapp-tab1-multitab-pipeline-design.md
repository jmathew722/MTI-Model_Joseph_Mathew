# Photo App as Tab 1 + Multi-Tab Pipeline Outputs — Design

**Date:** 2026-07-02
**Component:** `project/2D-3D-CAD-Test-Generation/webapp/`
**Status:** Approved for planning

## Goal

Turn the existing pipeline web UI into a **multi-tab interface** for the MTI
2D→3D SolidWorks pipeline:

- **Tab 1 — Image Preprocessing:** the DrawingCrop "photo app", copied in
  verbatim and embedded unmodified. The user loads a drawing (PDF or image),
  draws bounding boxes, and queues each box as a cropped image. A **"Run
  SolidWorks Pipeline"** button at the bottom triggers `main.py` with the saved
  crops as the input views folder and runs the full pipeline.
- **Tab 2 — Drawing vs 3D Model Viewer:** a split panel. Left shows the full
  original drawing (as a JPEG — the same source loaded in Tab 1); right is an
  interactive Three.js STL viewer showing the STL the pipeline exports for the
  current part. Both panels populate automatically when the pipeline finishes;
  no manual file loading. The viewer supports rotate / zoom / pan.
- **Remaining tabs — Pipeline Outputs:** one tab per pipeline artifact, empty on
  load, each populating with the file's readable contents the moment the
  pipeline writes it.

Also modify the **pipeline to export an STL** alongside its other outputs (see
"Pipeline modification" below) so Tab 2's viewer has a model to show.

## Constraints (from the request)

1. **Copy the photo app UI and all its files exactly as-is** into the project.
2. **Do not modify the photo app UI or its logic.** Its source files on disk are
   byte-for-byte identical to the original.
3. Tab 1 must **only display** the loaded file — no extraction/analysis happens
   until the user clicks Run.
4. The Run button wires to the **existing `main.py`** (no reimplementation of the
   pipeline).
5. Output tabs are **empty until produced** and fill **as files are written**.

## Decisions (resolved during brainstorming)

- **Crop hand-off = runtime bridge, files untouched.** The photo app declares its
  state with `let` (`let savedCrops = []`), which is *not* exposed on `window`,
  so a parent page cannot read the crops from the iframe by property access. The
  photo app's files stay byte-identical; at Run time the parent **appends a small
  script into the same-origin iframe document** that reads the already-queued
  crops (via the same code path the app's own "Download ZIP" uses) and posts them
  to the parent. No photo-app source file is edited.
- **DWG = deferred.** DWG is a binary CAD format needing an external converter
  (e.g. ODA File Converter), not present on this machine. Ship **PDF + image**
  input now (already works end-to-end) and show a clear "DWG requires a
  converter" message when a `.dwg` is selected. Real DWG conversion is a
  follow-up.
- **Build on the existing `webapp/`**, which already drives `main.py` as a
  subprocess, streams its console over SSE, and serves result files.
- **JPEG crops.** The photo app produces PNG blobs; the Run flow converts each to
  **JPEG** before upload, honoring "save each box as a JPEG." (The pipeline
  accepts PNG or JPEG, so this is purely to meet the stated requirement.)
- **Source image for Tab 2.** The same bridge that reads the crops also renders
  the photo app's loaded full drawing (`img`) to a **JPEG** and uploads it as the
  run's `source.jpg`, so Tab 2's left panel shows exactly what was loaded in
  Tab 1 with no extra user action.
- **Three.js "no external dependencies" = vendored locally.** Three.js, its
  `STLLoader`, and `OrbitControls` are copied into `webapp/vendor/` and served as
  static files (like pdf.js). Nothing is fetched from a CDN at runtime and the
  STL is never uploaded anywhere — the browser loads the STL's bytes from the
  run's local output folder and renders it client-side.
- **STL export in the pipeline.** After the COM build saves the `.sldprt`, the
  pipeline also exports `<part>.stl` into the same part folder (SolidWorks
  `SaveAs3` is extension-driven). When SolidWorks is unavailable (macros-only
  path), the macro package gains a **final numbered macro** that exports the STL
  when the user runs the macros in SolidWorks. STL filename == part name so the
  UI locates it automatically.

## Architecture

```
webapp/
  app.py            (extended: add /api/run-views, /api/runs/{id}/outputs,
                     source.jpg + .stl serving; keep the rest)
  index.html        (replaced: tabbed shell — Tab 1 iframe, Tab 2 viewer, output tabs)
  photoapp/         (NEW — verbatim copy of Photoapp/web, never edited)
    index.html
    pdf.min.js
    pdf.worker.min.js
  vendor/           (NEW — Three.js + STLLoader + OrbitControls, served static)
    three.min.js
    STLLoader.js
    OrbitControls.js
  bridge.js         (NEW — parent-side; injected into the iframe at Run time)
  runs/<id>/
    source.jpg                   (full original drawing captured at Run time — Tab 2 left)
    input/<part>/NN_<view>.jpg   (crops written here)
    output/<part>/
      <part>_extraction.json, <part>_resolved_extraction.json,
      <part>_verification_report.txt, macros/build_plan.json, macros/*.vba,
      <part>.sldprt, <part>.stl   (.sldprt/.stl only when SolidWorks is available)
```

### Data flow

```
[Tab 1: photoapp iframe]  user loads PDF/image, draws boxes, queues crops
        │  click "Run SolidWorks Pipeline"
        ▼
[parent] inject bridge.js into iframe → read savedCrops (PNG blobs) + stem() + full img
        │  convert each crop PNG → JPEG; render full drawing → source.jpg
        │  map crop name → canonical view + order prefix
        ▼
POST /api/run-views  (multipart: part name + source.jpg + N jpeg crop files)
        │  backend writes runs/<id>/source.jpg and runs/<id>/input/<part>/NN_<view>.jpg
        ▼
main.py --views-folder runs/<id>/input --output runs/<id>/output --no-export
        │  (existing _start_run subprocess + SSE console stream)
        │  extraction → Stage 2.5 → verification → macros → .sldprt → .stl
        ▼
[output tabs] poll /api/runs/{id}/outputs every ~1s → fill each tab as its file appears
[Tab 2]  left = /api/runs/{id}/source.jpg (immediately);
         right = Three.js viewer loads /api/runs/{id}/model.stl once present
```

## Components

### 1. Verbatim photo-app copy — `webapp/photoapp/`

- `Photoapp/web/index.html` copied unchanged.
- The two pdf.js assets it references (`/pdf.min.js`, `/pdf.worker.min.js`) are
  fetched once and vendored beside it so PDFs render offline. The photo app
  references them at absolute `/pdf.min.js`; served from `webapp/photoapp/`, the
  iframe's base makes `/photoapp/pdf.min.js` the correct path. **The one accepted
  reconciliation:** serve these assets at the paths the unmodified HTML expects
  (a static mount), *not* by editing the HTML. If a clean static mount can't
  satisfy the absolute paths, we host the iframe from a subpath so `/pdf.min.js`
  resolves — still no edit to the file.

### 2. Runtime bridge — `webapp/bridge.js` (parent-owned)

- Not part of the photo app. Loaded by the parent `index.html`.
- On Run: `const w = iframe.contentWindow; const doc = w.document;` then append a
  `<script>` element whose body reads `savedCrops` / `stem()` (they are in the
  iframe's global lexical scope, reachable by a script added to that same
  document) and `postMessage`s `{stem, crops:[{name, dataURL(jpeg)}]}` back to the
  parent.
- Guard rails: if `savedCrops` is empty → surface "Queue at least one view first."
  If the injected read throws (photo-app internals changed) → clear error, no
  silent failure.

### 3. Parent shell — `webapp/index.html` (replaces current)

- Reuses the existing UI's design tokens/CSS for visual consistency.
- **Tab bar:** `Image Preprocessing` · `Drawing vs 3D Model` · `Extraction JSON`
  · `Resolved Extraction` · `Build Plan` · `Verification Report` · `Model Check`
  · `VBA Macros` · `Console`. A `.sldprt` download surface appears in the shell
  when present.
- **Tab 1:** full-height `<iframe src="/photoapp/">` + a bottom bar with the
  **Run SolidWorks Pipeline** button and run status. `.dwg` selection anywhere
  routes to the "DWG requires a converter" message.
- **Tab 2 (split panel):** left `<img>` = `/api/runs/<id>/source.jpg`; right =
  Three.js `WebGLRenderer` canvas with `OrbitControls` (rotate/zoom/pan) and
  `STLLoader`. On run completion the source image shows immediately; the viewer
  polls for the STL and loads it (centered, auto-framed, lit) when it appears.
  Empty states: left "Run a drawing in Tab 1 first"; right "STL appears after a
  SolidWorks build (or after running the export macro on a SolidWorks machine)."
- **Output tabs:** each renders its file as readable text in a `<pre>` (JSON
  pretty-printed; reports/macros as-is). Empty state = "Not produced yet." Model
  Check / .sldprt empty state notes "requires Windows + SolidWorks."

### 4. Backend — `webapp/app.py` (extended)

- **`POST /api/run-views`** — accepts the part name + `source.jpg` + JPEG crops.
  Writes `source.jpg` to `runs/<id>/` and crops to
  `runs/<id>/input/<part>/NN_<view>.jpg` where `NN` is the canonical order index
  and `<view>` is the crop's mapped view name (see mapping below), then calls the
  existing `_start_run` with
  `main.py --views-folder <input> --output <output> --no-export`. Returns `{id}`.
- **`GET /api/runs/{id}/source.jpg`** — serves the captured full drawing for
  Tab 2's left panel (404 until the run is created).
- **`GET /api/runs/{id}/model.stl`** — serves the part's exported `.stl` bytes
  for the Three.js viewer (404 until the pipeline writes it). Located by the
  `<part>.stl` name within the run's output folder.
- **`GET /api/runs/{id}/outputs`** — returns, for each known artifact category,
  `{present, name, text|null, download}`. Categories map to filename patterns:
  - `extraction` → `*_extraction.json` (excluding `*_resolved_extraction.json`)
  - `resolved` → `*_resolved_extraction.json`
  - `build_plan` → `macros/build_plan.json` (or `**/build_plan*.json`)
  - `verification` → `*_verification_report.txt`
  - `model_check` → model-validation output when present (COM/Windows only)
  - `macros` → `macros/*.vba` (list; concatenated or per-file text)
  - `sldprt` → `*.sldprt` (download only; no text)
  Text is inlined for files under a size cap; larger files are download-only.
- Existing `/api/runs/{id}/log` (SSE console), `/download`, `/zip` kept as-is.

### View-name mapping (crop name → pipeline view)

The pipeline's `view_ingest.classify_view` reads the view from the filename.
The Run flow names each uploaded crop to guarantee correct classification and
order, using the photo app's chosen preset/custom name:

| Photo-app crop name        | Written filename | Pipeline view |
|----------------------------|------------------|---------------|
| front                      | `01_front.jpg`   | front         |
| top                        | `02_top.jpg`     | top           |
| side / right               | `03_side.jpg`    | side          |
| left / back                | `04_second_side.jpg` | second_side |
| bottom                     | `05_bottom.jpg`  | bottom        |
| other (detail/section/custom) | `<name>.jpg`  | unclassified → pipeline warns & skips (front is the only required view) |

## Pipeline modification — STL export

The pipeline gains one new capability: every part that yields a `.sldprt` also
yields a `<part>.stl` in the same folder, and the macros-only path emits a macro
that does the same when run in SolidWorks.

- **New helper `export_stl(sw_doc, name, output_dir) -> Path`** in
  `pipeline/solidworks_builder.py`. Mirrors `save_model`: resolves an absolute
  `<output_dir>/<safe_name>.stl` and calls `sw_doc.SaveAs3(path, 0, 1)`
  (SolidWorks picks the STL translator from the `.stl` extension). Returns the
  path; raises `SolidWorksError` on a non-zero result.
- **Call sites (COM path):** invoke `export_stl` immediately after the `.sldprt`
  is saved — in `build_model` (solidworks_builder) and in
  `build_sldprt_for_part` (batch), so both the single-drawing and multi-view/batch
  builds produce the STL beside the `.sldprt`, using the **same part name**.
- **Macros-only path:** `pipeline/macro_generator.py` appends a final numbered
  macro after `ZZ_final_verify.vba` — e.g. `ZZ_export_stl.vba` (sorts last) —
  that takes the active document and `SaveAs3`es `<part>.stl` next to it (path
  derived from `swModel.GetPathName`). The step is added to `build_plan.json`,
  the `README.md` run order, and the single-run `RUN_ALL.vba` so it can't drift.
- **Non-fatal:** an STL-export failure is logged/annotated like the existing
  `.sldprt` failure handling; it never aborts a run that already produced the
  reports, macros, and `.sldprt`.
- **macOS reality:** COM export needs Windows + SolidWorks, so no `.stl` is
  produced on this machine; Tab 2's viewer shows its empty-state note, and the
  export macro is still generated as text (visible in the VBA Macros tab).

## Error handling

- **No API key:** extraction needs `ANTHROPIC_API_KEY`. If unset, `/api/run-views`
  returns a clear message (mirrors the existing `/api/run` behavior) and the UI
  shows it in the console tab.
- **No front view queued:** allowed to run (pipeline warns), but the UI warns
  first since the base profile needs a front view.
- **Pipeline non-zero exit:** surfaced in the Console tab with the exit code; any
  artifacts already written still populate their tabs.
- **`.sldprt` / model check unavailable (macOS):** tabs show the "requires
  Windows + SolidWorks" note rather than staying blankly empty.
- **DWG selected:** "DWG requires a converter — use PDF or an image."

## Testing

- **Backend unit tests** (pytest, alongside existing webapp/pipeline tests):
  - `/api/run-views` writes crops to the correct `NN_<view>.jpg` paths and spawns
    the expected `main.py --views-folder` command (subprocess mocked).
  - `/api/runs/{id}/outputs` categorizes a fixture output dir correctly
    (extraction vs resolved vs build_plan vs verification vs macros vs sldprt vs
    stl) and respects the inline size cap.
  - `source.jpg` and `model.stl` endpoints serve when present and 404 otherwise.
  - View-name mapping table (crop name → filename/view) is unit-tested directly.
- **Pipeline STL tests** (pytest, SolidWorks COM mocked):
  - `export_stl` resolves `<output_dir>/<part>.stl` and calls `SaveAs3` with it;
    a non-zero result raises `SolidWorksError`.
  - `build_model` / `build_sldprt_for_part` call `export_stl` after saving the
    `.sldprt` (same part name).
  - `generate_macro_package` emits a final STL-export macro that sorts after
    `ZZ_final_verify.vba` and is present in `build_plan.json`, `README.md`, and
    `RUN_ALL.vba`.
- **Photo-app integrity check:** a test asserts `webapp/photoapp/index.html` is
  byte-identical to the source `Photoapp/web/index.html` (guards "do not modify").
- **Manual/e2e (driven with the run skill):** load a sample PDF in Tab 1, queue a
  front crop, click Run, confirm the console streams and the Extraction / Resolved
  / Verification / Build Plan / Macros tabs fill as files are written, and Tab 2's
  left panel shows the source drawing. (STL viewer is validated with a sample STL
  fixture served through `model.stl`, since no `.stl` is built on macOS.)

## Out of scope (YAGNI)

- DWG conversion (deferred — clear message only).
- Editing/curating crops after queueing beyond what the photo app already does.
- Multi-part-per-run UI (the pipeline supports it; Tab 1 produces one part at a
  time — a single part folder per run is enough here).
- Building `.sldprt` / `.stl` on macOS (impossible without Windows + SolidWorks;
  surfaced as a note — the STL export code + macro still ship).
- Tuning STL export options (binary vs ASCII, deviation/angle resolution): use
  SolidWorks' current defaults; no options UI.
- Overlaying/aligning the 2D drawing and 3D model in Tab 2 — they sit side by
  side, not registered to each other.
