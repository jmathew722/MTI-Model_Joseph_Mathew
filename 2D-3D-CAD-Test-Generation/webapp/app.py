"""FastAPI web front-end for the 2D->3D SolidWorks pipeline.

Drives the existing CLI (`main.py`) as a subprocess so the proven entrypoint is
untouched. Serves a single-file UI, streams the pipeline's console output live,
and serves the result files (VBA macros, verification report, JSON).

Run:
    cd webapp && ./run.sh        # sets up a venv + deps, launches uvicorn :8092
"""
from __future__ import annotations

import io
import os
import sys
import time
import uuid
import shlex
import shutil
import zipfile
import threading
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# webapp/ lives inside the project dir; the CLI + samples live one level up.
WEBAPP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEBAPP_DIR.parent
RUNS_DIR = WEBAPP_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)
# Multi-part working area: parts/<session>/<part>/ holds one part's crop JPEGs
# (the --views-folder for that part); parts/<session>/.thumbs/<part>.jpg holds
# the source thumbnail (kept OUT of the views folder so it is never treated as a
# view); the pipeline writes into parts/<session>/<part>/output/.
PARTS_DIR = WEBAPP_DIR / "parts"
PARTS_DIR.mkdir(exist_ok=True)
# Human-facing deliverables: every UI run copies its finished outputs here into a
# folder named after the part, so they are easy to find and open (as opposed to
# the buried per-session working dir under parts/). Re-running a part refreshes
# its folder with the latest outputs.
DELIVER_DIR = PROJECT_DIR / "UI_Output"
DELIVER_DIR.mkdir(exist_ok=True)
# Same per-part deliverables are also mirrored into the user's Downloads folder so
# they can be found outside the project tree.
DOWNLOADS_DIR = Path.home() / "Downloads" / "SolidWorksModel_Parts"

load_dotenv(PROJECT_DIR / ".env")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".tif", ".tiff", ".bmp"}

# Photo-app crop name -> (canonical order index, pipeline view name). The pipeline's
# view_ingest classifies views from the filename; naming the uploaded crops this way
# guarantees correct classification + processing order.
VIEW_MAP: dict[str, tuple[int, str]] = {
    "front": (1, "front"),
    "top": (2, "top"),
    "side": (3, "side"),
    "right": (3, "side"),
    "left": (4, "second_side"),
    "back": (4, "second_side"),
    "bottom": (5, "bottom"),
}

app = FastAPI(title="MTI 2D->3D Pipeline UI")

# Static assets: the verbatim photo app (Tab 1) and the vendored 3D-viewer libs.
app.mount("/photoapp", StaticFiles(directory=str(WEBAPP_DIR / "photoapp"), html=True), name="photoapp")
app.mount("/vendor", StaticFiles(directory=str(WEBAPP_DIR / "vendor")), name="vendor")

# id -> {"lines": list[str], "done": bool, "exit": int|None, "output": Path}
RUNS: dict[str, dict] = {}


def _has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _samples() -> list[str]:
    """Saved extractions usable for a no-API demo run (e.g. '117C')."""
    names = []
    for p in sorted(PROJECT_DIR.glob("extraction_*.json")):
        stem = p.stem  # extraction_117C
        names.append(stem.replace("extraction_", "", 1))
    return names


def _flatten_copy(out_dir: Path, dest: Path) -> Path | None:
    """Copy a finished run's outputs into ``dest/`` (flat, openable).

    The pipeline writes all of a part's artifacts into a single subfolder of
    ``out_dir`` (named after the part); its contents are copied up so the delivered
    folder holds the files directly (sldprt, stl, macros/, reports, json), plus the
    loose ``token_usage_log.txt`` / summary csv. SolidWorks lock/autosave junk is
    skipped. An existing ``dest`` is replaced. Returns the folder, or None on error."""
    def _skip(name: str) -> bool:
        return name.startswith("~$") or name.startswith("AUTOSAVE_")

    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)

        for sub in sorted(p for p in out_dir.iterdir() if p.is_dir() and p.name != ".extraction_cache"):
            for item in sub.iterdir():
                if _skip(item.name):
                    continue
                target = dest / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("~$*", "AUTOSAVE_*"))
                else:
                    shutil.copy2(item, target)
        # Loose top-level files (token log, multiview_summary.csv).
        for f in out_dir.iterdir():
            if f.is_file() and not _skip(f.name):
                shutil.copy2(f, dest / f.name)
        return dest
    except Exception:
        return None


def _deliver_run(out_dir: Path, part_name: str) -> dict:
    """Deliver a finished run's outputs to BOTH the project ``UI_Output/<part>/``
    and the user's ``~/Downloads/SolidWorksModel_Parts/<part>/`` so they are easy
    to find and open. Returns the paths that were written."""
    part = _sanitize(part_name)
    project = _flatten_copy(out_dir, DELIVER_DIR / part)
    downloads = _flatten_copy(out_dir, DOWNLOADS_DIR / part)
    return {
        "project": str(project) if project else None,
        "downloads": str(downloads) if downloads else None,
    }


def _start_run(cmd: list[str], output_dir: Path, run_id: str | None = None,
               deliver_name: str | None = None) -> str:
    """Spawn the pipeline CLI and stream its output into RUNS[id]['lines'].

    When ``deliver_name`` is given, a successful run's outputs are also copied into
    ``UI_Output/<deliver_name>/`` so they are easy to find and open."""
    run_id = run_id or uuid.uuid4().hex[:12]
    output_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "lines": [], "done": False, "exit": None, "output": output_dir,
        "proc": None, "started": time.time(), "finished": None, "cancelled": False,
        "deliver_name": deliver_name, "delivered": None, "delivered_downloads": None,
    }
    RUNS[run_id] = state

    state["lines"].append(f"$ {' '.join(shlex.quote(c) for c in cmd)}")

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Keep rich output plain & wide so the log panel reads cleanly.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("COLUMNS", "120")

    def _worker():
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                # Force UTF-8 with replacement: the pipeline's rich console emits
                # UTF-8 box-drawing output. Without this, text mode defaults to the
                # Windows locale codec (cp1252), which raises UnicodeDecodeError on
                # the first non-cp1252 byte, kills this reader thread, stops draining
                # the pipe, fills the OS buffer, and hangs the child forever.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as e:  # spawn failure
            state["lines"].append(f"[launch error] {type(e).__name__}: {e}")
            state["exit"] = 127
            state["done"] = True
            state["finished"] = time.time()
            return
        state["proc"] = proc
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                state["lines"].append(line.rstrip("\n"))
        except Exception as e:
            # A reader failure must never leave the child undrained (that would
            # fill the pipe buffer and hang it). Record it, then kill + reap below.
            state["lines"].append(f"[reader error] {type(e).__name__}: {e}")
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            state["exit"] = proc.wait()
            # Deliver clean, openable copies for a successful, non-cancelled run:
            # one in the project (UI_Output/) and one in the user's Downloads folder.
            if state["exit"] == 0 and not state["cancelled"] and deliver_name:
                paths = _deliver_run(output_dir, deliver_name)
                state["delivered"] = paths.get("project")
                state["delivered_downloads"] = paths.get("downloads")
                if paths.get("project"):
                    state["lines"].append(f"[delivered outputs to] {paths['project']}")
                if paths.get("downloads"):
                    state["lines"].append(f"[delivered outputs to] {paths['downloads']}")
            state["done"] = True
            state["finished"] = time.time()

    threading.Thread(target=_worker, daemon=True).start()
    return run_id


def _cancel_run(run_id: str) -> None:
    """Terminate a run's process (and any children it spawned)."""
    state = RUNS[run_id]
    if state["done"]:
        return
    proc = state.get("proc")
    if proc is None:
        raise HTTPException(409, "Run has not started yet")
    state["cancelled"] = True
    state["lines"].append("[cancel requested by user]")
    try:
        if os.name == "nt":
            # taskkill /T also kills any child processes the pipeline spawned.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            proc.terminate()
    except Exception as e:
        state["lines"].append(f"[cancel error] {type(e).__name__}: {e}")


@app.get("/")
def index():
    return FileResponse(WEBAPP_DIR / "index.html")


@app.get("/bridge.js")
def bridge_js():
    return FileResponse(WEBAPP_DIR / "bridge.js", media_type="application/javascript")


# The verbatim photo app references pdf.js at ABSOLUTE root paths (/pdf.min.js,
# /pdf.worker.min.js). Serve them there so the file stays byte-for-byte unmodified.
@app.get("/pdf.min.js")
def pdf_js():
    return FileResponse(WEBAPP_DIR / "photoapp" / "pdf.min.js", media_type="application/javascript")


@app.get("/pdf.worker.min.js")
def pdf_worker_js():
    return FileResponse(WEBAPP_DIR / "photoapp" / "pdf.worker.min.js", media_type="application/javascript")


@app.get("/api/status")
def status():
    return {"live": _has_api_key(), "samples": _samples()}


@app.get("/api/samples")
def samples():
    return {"samples": _samples()}


@app.post("/api/run")
async def run(file: UploadFile = File(...)):
    if not _has_api_key():
        raise HTTPException(
            400,
            "No ANTHROPIC_API_KEY set — live extraction is unavailable. "
            "Use the demo instead (runs a saved extraction, no API call).",
        )
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_EXT)}")

    run_id = uuid.uuid4().hex[:12]
    run_root = RUNS_DIR / run_id
    in_dir = run_root / "input"
    out_dir = run_root / "output"
    in_dir.mkdir(parents=True, exist_ok=True)
    dest = in_dir / Path(file.filename).name
    dest.write_bytes(await file.read())

    cmd = [sys.executable, "main.py", "--drawing", str(dest), "--output", str(out_dir), "--no-export"]
    _start_run(cmd, out_dir, run_id=run_id)
    return {"id": run_id}


class DemoReq(BaseModel):
    sample: str | None = None


@app.post("/api/demo")
def demo(req: DemoReq):
    samples = _samples()
    if not samples:
        raise HTTPException(404, "No saved extraction_*.json samples found to demo.")
    sample = req.sample or samples[0]
    if sample not in samples:
        raise HTTPException(404, f"Sample '{sample}' not found. Available: {samples}")

    json_path = PROJECT_DIR / f"extraction_{sample}.json"
    run_id = uuid.uuid4().hex[:12]
    out_dir = RUNS_DIR / run_id / "output"
    cmd = [sys.executable, "main.py", "--from-json", str(json_path), "--output", str(out_dir), "--no-export"]
    _start_run(cmd, out_dir, run_id=run_id)
    return {"id": run_id}


@app.get("/api/runs/{run_id}/log")
def log(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")

    state = RUNS[run_id]

    def gen():
        import time
        idx = 0
        while True:
            lines = state["lines"]
            while idx < len(lines):
                # SSE: escape newlines are not needed (each item is one line).
                yield f"data: {lines[idx]}\n\n"
                idx += 1
            if state["done"] and idx >= len(state["lines"]):
                yield f"event: done\ndata: {state['exit']}\n\n"
                return
            time.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    if RUNS[run_id]["done"]:
        return {"ok": True, "already_done": True}
    _cancel_run(run_id)
    return {"ok": True}


def _categorize(name: str) -> str:
    low = name.lower()
    if low.endswith((".vba", ".bas", ".swp", ".swb")):
        return "macro"
    if low.endswith(".json"):
        return "json"
    if "verif" in low or "report" in low or low.endswith((".txt", ".md")):
        return "report"
    return "other"


def _result_files(run_id: str) -> list[Path]:
    state = RUNS.get(run_id)
    if not state:
        return []
    out: Path = state["output"]
    if not out.exists():
        return []
    files = [p for p in out.rglob("*") if p.is_file() and ".extraction_cache" not in p.parts]
    return sorted(files)


@app.get("/api/runs/{run_id}/files")
def files(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    out: Path = RUNS[run_id]["output"]
    items = []
    inline = None
    for p in _result_files(run_id):
        rel = p.relative_to(out).as_posix()
        cat = _categorize(p.name)
        items.append({"name": rel, "size": p.stat().st_size, "category": cat})
        # Inline the first small text report so the UI can render it.
        if inline is None and cat == "report" and p.suffix.lower() in {".txt", ".md"} and p.stat().st_size < 200_000:
            try:
                inline = {"name": rel, "text": p.read_text(encoding="utf-8", errors="replace")}
            except Exception:
                pass
    return {"done": RUNS[run_id]["done"], "exit": RUNS[run_id]["exit"], "files": items, "report": inline}


@app.get("/api/runs/{run_id}/download/{name:path}")
def download(run_id: str, name: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    out: Path = RUNS[run_id]["output"]
    target = (out / name).resolve()
    if out.resolve() not in target.parents and target != out.resolve():
        raise HTTPException(403, "Path outside run output")
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(str(target), filename=target.name)


@app.get("/api/runs/{run_id}/zip")
def zip_run(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    out: Path = RUNS[run_id]["output"]
    files = _result_files(run_id)
    if not files:
        raise HTTPException(404, "No result files to zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(out).as_posix())
    buf.seek(0)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="results_{run_id}.zip"'},
    )


# ── Multi-view run from photo-app crops (Tab 1 "Run SolidWorks Pipeline") ──────

def _sanitize(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name.strip())
    return keep.strip("_") or "part"


def _crop_filename(crop_name: str, seq: int) -> str:
    """Map a photo-app crop name to a pipeline-classifiable filename.

    Known views get an ordered ``NN_<view>.jpg`` name so view_ingest classifies
    and orders them; anything else keeps its own (sanitized) name and is left for
    the pipeline to warn about / skip (only the front view is required)."""
    key = crop_name.strip().lower().replace(" ", "_")
    if key in VIEW_MAP:
        order, view = VIEW_MAP[key]
        return f"{order:02d}_{view}.jpg"
    return f"{_sanitize(crop_name)}.jpg"


@app.post("/api/run-views")
async def run_views(
    part: str = Form("drawing"),
    source: UploadFile | None = File(None),
    crops: list[UploadFile] = File(...),
):
    if not _has_api_key():
        raise HTTPException(
            400,
            "No ANTHROPIC_API_KEY set — extraction is unavailable. Set it in "
            "the project .env and restart the UI.",
        )
    if not crops:
        raise HTTPException(400, "No cropped views were provided.")

    part_name = _sanitize(part)
    run_id = uuid.uuid4().hex[:12]
    run_root = RUNS_DIR / run_id
    in_dir = run_root / "input" / part_name
    out_dir = run_root / "output"
    in_dir.mkdir(parents=True, exist_ok=True)

    # Full original drawing: kept for Tab 2's left panel AND fed into extraction as
    # whole-part context (saved in the views folder as the "full" overview view).
    if source is not None:
        src_bytes = await source.read()
        (run_root / "source.jpg").write_bytes(src_bytes)
        (in_dir / OVERVIEW_FILENAME).write_bytes(src_bytes)

    used: set[str] = set()
    for i, up in enumerate(crops, start=1):
        stem = Path(up.filename or f"view{i}").stem
        fname = _crop_filename(stem, i)
        # Avoid clobbering when two crops map to the same view.
        if fname in used:
            fname = f"{Path(fname).stem}_{i}{Path(fname).suffix}"
        used.add(fname)
        (in_dir / fname).write_bytes(await up.read())

    cmd = [
        sys.executable, "main.py",
        "--views-folder", str(run_root / "input"),
        "--output", str(out_dir),
        "--no-export",
    ]
    _start_run(cmd, out_dir, run_id=run_id, deliver_name=part_name)
    return {"id": run_id, "part": part_name, "views": sorted(used)}


@app.get("/api/runs/{run_id}/source.jpg")
def run_source(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    p = RUNS[run_id]["output"].parent / "source.jpg"
    if not p.is_file():
        raise HTTPException(404, "No source image for this run")
    return FileResponse(str(p), media_type="image/jpeg")


def _find_stl(out: Path) -> Path | None:
    stls = sorted(out.rglob("*.stl")) if out.exists() else []
    return stls[0] if stls else None


@app.get("/api/runs/{run_id}/model.stl")
def run_stl(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    p = _find_stl(RUNS[run_id]["output"])
    if p is None:
        raise HTTPException(404, "No STL for this run yet")
    return FileResponse(str(p), media_type="model/stl", filename=p.name)


def _first(out: Path, patterns: list[str], exclude: list[str] | None = None) -> Path | None:
    exclude = exclude or []
    for pat in patterns:
        for p in sorted(out.rglob(pat)):
            if any(ex in p.name for ex in exclude):
                continue
            if p.is_file():
                return p
    return None


_INLINE_CAP = 400_000


def _cat(p: Path | None) -> dict:
    if p is None:
        return {"present": False}
    try:
        text = p.read_text(encoding="utf-8", errors="replace") if p.stat().st_size < _INLINE_CAP else None
    except Exception:
        text = None
    return {"present": True, "name": p.name, "text": text}


def _categorize_output(out: Path) -> dict:
    """Categorise a pipeline output dir into the shape the output tabs consume.
    Shared by run-id-scoped and per-part outputs so both render identically."""
    extraction = _first(out, ["*_extraction.json", "*extraction*.json"], exclude=["resolved"])
    resolved = _first(out, ["*_resolved_extraction.json", "*resolved*.json"])
    build_plan = _first(out, ["*build_plan*.json", "**/build_plan*.json"])
    verification = _first(out, ["*verification_report*.txt", "*verification*.txt"])
    model_check = _first(out, ["*model_check*.txt", "*model_validation*.txt", "*validation_report*.txt"])

    macros_files = []
    if out.exists():
        for m in sorted(out.rglob("*.vba")):
            try:
                txt = m.read_text(encoding="utf-8", errors="replace") if m.stat().st_size < _INLINE_CAP else "(too large to inline)"
            except Exception:
                txt = "(unreadable)"
            macros_files.append({"name": m.relative_to(out).as_posix(), "text": txt})

    stl = _find_stl(out)
    has_any = any(x is not None for x in (extraction, resolved, build_plan, verification, model_check)) or bool(macros_files)
    return {
        "has_any": has_any,
        "stl_mtime": int(stl.stat().st_mtime) if stl else 0,
        "categories": {
            "extraction": _cat(extraction),
            "resolved": _cat(resolved),
            "build_plan": _cat(build_plan),
            "verification": _cat(verification),
            "model_check": _cat(model_check),
            "macros": {"present": bool(macros_files), "files": macros_files},
            "sldprt": {"present": bool(_first(out, ["*.sldprt"]))},
            "stl": {"present": stl is not None, "name": stl.name if stl else None},
        },
    }


@app.get("/api/runs/{run_id}/outputs")
def run_outputs(run_id: str):
    """Categorised pipeline artifacts for the output tabs. Each category reports
    presence + inline text so a tab can render the file as soon as it is written."""
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    state = RUNS[run_id]
    out: Path = state["output"]
    payload = _categorize_output(out)
    payload.update({
        "done": state["done"],
        "exit": state["exit"],
        "source": (out.parent / "source.jpg").is_file(),
        "started": state.get("started"),
        "finished": state.get("finished"),
        "cancelled": state.get("cancelled", False),
        "delivered": state.get("delivered"),
        "delivered_downloads": state.get("delivered_downloads"),
    })
    return payload


# ── Multi-part working set (Tab 1 part selector + per-part run) ────────────────

def _safe_session(session: str) -> str:
    s = "".join(c for c in (session or "") if c.isalnum() or c in "-_")
    if not s:
        raise HTTPException(400, "Invalid session id")
    return s


def _session_dir(session: str) -> Path:
    return PARTS_DIR / _safe_session(session)


# The full drawing is stored in the views folder under this name so view_ingest
# classifies it as the "full" overview view (whole-part extraction context). It is
# not one of the orthographic views, so it is excluded from the view count/list.
OVERVIEW_FILENAME = "00_full.jpg"


def _list_parts(sdir: Path) -> list[dict]:
    if not sdir.is_dir():
        return []
    parts = []
    for pdir in sorted(p for p in sdir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        views = sorted(v.name for v in pdir.glob("*.jpg") if v.name != OVERVIEW_FILENAME)
        out = pdir / "output"
        parts.append({
            "name": pdir.name,
            "n_views": len(views),
            "views": views,
            "has_output": out.is_dir() and _categorize_output(out)["has_any"],
        })
    return parts


@app.post("/api/session")
def new_session():
    sid = uuid.uuid4().hex[:12]
    (PARTS_DIR / sid).mkdir(parents=True, exist_ok=True)
    return {"session": sid}


@app.post("/api/parts")
async def add_part(
    session: str = Form(...),
    part: str = Form("drawing"),
    source: UploadFile | None = File(None),
    crops: list[UploadFile] = File(...),
):
    if not crops:
        raise HTTPException(400, "No cropped views were provided.")
    sdir = _session_dir(session)
    sdir.mkdir(parents=True, exist_ok=True)
    part_name = _sanitize(part)
    pdir = sdir / part_name
    # Re-adding a part replaces its crops (fresh set), but keeps any prior output.
    if pdir.exists():
        for old in pdir.glob("*.jpg"):
            old.unlink()
    pdir.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    for i, up in enumerate(crops, start=1):
        stem = Path(up.filename or f"view{i}").stem
        fname = _crop_filename(stem, i)
        if fname in used:
            fname = f"{Path(fname).stem}_{i}{Path(fname).suffix}"
        used.add(fname)
        (pdir / fname).write_bytes(await up.read())

    if source is not None:
        src_bytes = await source.read()
        thumbs = sdir / ".thumbs"
        thumbs.mkdir(exist_ok=True)
        (thumbs / f"{part_name}.jpg").write_bytes(src_bytes)
        # Also feed the FULL drawing into extraction as whole-part context: saved in
        # the views folder as an overview view (00_full.jpg -> classified "full").
        (pdir / OVERVIEW_FILENAME).write_bytes(src_bytes)

    return {"session": _safe_session(session), "part": part_name, "parts": _list_parts(sdir)}


@app.get("/api/parts")
def list_parts(session: str):
    return {"session": _safe_session(session), "parts": _list_parts(_session_dir(session))}


@app.get("/api/parts/{session}/{part}/thumb.jpg")
def part_thumb(session: str, part: str):
    p = _session_dir(session) / ".thumbs" / f"{_sanitize(part)}.jpg"
    if not p.is_file():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(str(p), media_type="image/jpeg")


@app.get("/api/parts/{session}/{part}/outputs")
def part_outputs(session: str, part: str):
    pdir = _session_dir(session) / _sanitize(part)
    if not pdir.is_dir():
        raise HTTPException(404, "Unknown part")
    payload = _categorize_output(pdir / "output")
    payload.update({"part": _sanitize(part), "ran": (pdir / "output").is_dir()})
    return payload


@app.get("/api/parts/{session}/{part}/model.stl")
def part_stl(session: str, part: str):
    p = _find_stl(_session_dir(session) / _sanitize(part) / "output")
    if p is None:
        raise HTTPException(404, "No STL for this part yet")
    return FileResponse(str(p), media_type="model/stl", filename=p.name)


@app.post("/api/run-part")
def run_part(session: str = Form(...), part: str = Form(...)):
    if not _has_api_key():
        raise HTTPException(
            400,
            "No ANTHROPIC_API_KEY set — extraction is unavailable. Set it in the "
            "project .env and restart the UI.",
        )
    pdir = _session_dir(session) / _sanitize(part)
    if not pdir.is_dir() or not any(pdir.glob("*.jpg")):
        raise HTTPException(400, "Selected part has no saved views.")
    out_dir = pdir / "output"
    # Scope the run to EXACTLY this one part's subfolder (not the parent).
    cmd = [
        sys.executable, "main.py",
        "--views-folder", str(pdir),
        "--output", str(out_dir),
        "--no-export",
    ]
    run_id = _start_run(cmd, out_dir, deliver_name=_sanitize(part))
    return {"id": run_id, "part": _sanitize(part)}
