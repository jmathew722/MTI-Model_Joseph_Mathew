# MTI Pipeline Web UI — Design

**Date:** 2026-06-29
**Status:** Approved (design), pending implementation
**Location:** `project/2D-3D-CAD-Test-Generation/webapp/`

## Goal

A web front-end for the existing 2D→3D SolidWorks pipeline (a CLI tool, `main.py`),
styled like the sister repo's `2D-3D-CAD-Test-Generation` app: drag-drop a drawing,
run the pipeline, watch a live log, and download the results. The user must be able
to **open and run it** with one command, with zero cost/setup via a demo mode.

## Constraints & context

- The pipeline is a procedural CLI (`python main.py --drawing … --output …`). It needs
  `ANTHROPIC_API_KEY` for live extraction (slow, paid Claude Vision calls).
- It has a free, no-API path: `--from-json <extraction>.json --output …` regenerates
  macros from a saved extraction. The repo ships `extraction_115C/116C/117C.json`.
- On macOS there is no SolidWorks, so output is **VBA macros + verification report +
  JSON** (no `.sldprt`). The CLI already handles this gracefully.
- The pipeline's own `requirements.txt` must stay untouched.

## Approach (chosen)

**Subprocess-drive `main.py`.** The web server shells out to the existing CLI rather
than importing/refactoring pipeline internals. This keeps the proven entrypoint
untouched and is the most robust path. The server captures console output and streams
it to the browser, then lists the produced files.

Rejected alternative: importing pipeline functions in-process (like the sister repo's
`app.py`). The MTI `main.py` is argparse/console-coupled; refactoring it for import
would risk the working CLI for no proportional benefit.

## Components

All new files live in `project/2D-3D-CAD-Test-Generation/webapp/`:

| File | Purpose |
|------|---------|
| `app.py` | FastAPI server: serves UI, launches pipeline subprocesses, streams logs, serves results |
| `index.html` | Single-file UI (HTML+CSS+JS, no build step), styled like the 2D-3D-CAD app |
| `requirements-ui.txt` | UI-only deps: `fastapi`, `uvicorn[standard]`, `python-multipart` |
| `run.sh` | One command: create/use a venv, install UI deps, launch uvicorn |
| `runs/` | Per-run working dirs (gitignored): uploaded drawing + pipeline `--output` |

## Backend — endpoints

- `GET /` → serve `index.html`.
- `GET /api/status` → `{ "live": bool }` — whether `ANTHROPIC_API_KEY` is set. Drives the
  header status pill ("Live extraction" vs "Demo only") and enables/disables Run.
- `GET /api/samples` → list of available demo extractions (`extraction_115C/116C/117C`).
- `POST /api/run` (multipart: `file`) → save upload into a new `runs/<id>/input/`, spawn
  `python main.py --drawing <path> --output runs/<id>/output` (no `--validate-only`, so it
  runs full Phase 1 + macro generation), return `{ "id": <id> }`. 400 if no API key.
- `POST /api/demo` (JSON: `{ "sample": "117C" }`) → spawn
  `python main.py --from-json extraction_<sample>.json --output runs/<id>/output`,
  return `{ "id": <id> }`. No API key needed.
- `GET /api/runs/{id}/log` → Server-Sent Events stream of the subprocess's combined
  stdout/stderr; emits a terminal `event: done` with the exit code when the process ends.
- `GET /api/runs/{id}/files` → JSON list of result files (name, size, category:
  report / macro / json / other), discovered by walking `runs/<id>/output`.
- `GET /api/runs/{id}/download/{name}` → download a single result file.
- `GET /api/runs/{id}/zip` → download all results for the run as a single zip.

Run state is kept in an in-memory dict keyed by run id: `{ process, log_buffer, status }`.
Run ids are `uuid4` hex strings.

## Frontend — layout (mirrors the 2D-3D-CAD app)

- **Header:** title "MTI 2D→3D Pipeline", subtitle, and a status pill on the right —
  green "Live extraction" when the key is set, amber "Demo only" otherwise.
- **Drop zone:** dashed box, "Drop an engineering drawing here / or click to browse ·
  JPG, PNG, WEBP, PDF". Shows the selected filename.
- **Actions:** primary **Run extraction** button (disabled + tooltip when no API key),
  and a **Try demo (no API key)** control with a small dropdown to pick 115C/116C/117C.
- **Live log panel:** monospace, auto-scrolling, fed by the SSE stream; turns the run
  red on non-zero exit, green on success.
- **Results panel:** renders the verification report text prominently, then a list of
  generated files grouped by category (macros, JSON, report) each with a download link,
  plus a **Download all (zip)** button.

No frontend framework or build step — single `index.html` with inline CSS/JS and `fetch`
+ `EventSource`, matching the sister app's self-contained style.

## How to run

```bash
cd project/2D-3D-CAD-Test-Generation/webapp
./run.sh            # sets up venv + UI deps, launches uvicorn on 127.0.0.1:8092
```

Open `http://127.0.0.1:8092`. With no `ANTHROPIC_API_KEY`, click **Try demo** to run a
saved extraction end-to-end (macros + report) for free.

## Error handling

- Upload with no API key → 400 + UI explains demo mode.
- Pipeline non-zero exit → log panel shows full output in red; results panel shows
  whatever files were produced (often none) with the exit code.
- Unsupported file type → rejected client-side (accept list) and server-side (extension
  check) before spawning.
- Missing sample json for demo → 404 with a clear message.

## Testing / verification

- Smoke: `GET /` returns 200 with the UI; `GET /api/status` returns valid JSON.
- Demo run: `POST /api/demo {sample:"117C"}` then poll `/files` → expect macro/report
  files; verify via the live log reaching `done` with exit 0.
- Visual: headless-Chrome screenshot of the loaded UI and of a completed demo run.
- The existing pipeline's pytest suite is unaffected (no pipeline files changed).

## Out of scope

- `.sldprt` COM build (Windows/SolidWorks only) — UI just notes it's skipped.
- Auth, multi-user, persistence beyond `runs/` on disk.
- Editing/annotating extractions in the browser.
