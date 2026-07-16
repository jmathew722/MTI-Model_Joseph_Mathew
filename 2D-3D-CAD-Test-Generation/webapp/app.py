"""FastAPI web front-end for the 2D->3D SolidWorks pipeline.

Drives the existing CLI (`main.py`) as a subprocess so the proven entrypoint is
untouched. Serves a single-file UI, streams the pipeline's console output live,
and serves the result files (VBA macros, verification report, JSON).

Run:
    cd webapp && ./run.sh        # sets up a venv + deps, launches uvicorn :8092
"""
from __future__ import annotations

import io
import json
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
from fastapi import FastAPI, File, Form, Request, UploadFile, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# webapp/ lives inside the project dir; the CLI + samples live one level up.
WEBAPP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEBAPP_DIR.parent
# The webapp normally reaches the pipeline only by launching main.py as a
# subprocess (which runs from PROJECT_DIR). A few endpoints import pipeline
# modules directly (e.g. the Learning Loop export), so put PROJECT_DIR on the
# import path here too.
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
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

# Untouched uploads (PDF/JPG/DWG) are kept per part so the original drawing is
# always delivered next to the generated outputs.
ORIGINALS_DIRNAME = ".originals"

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
    # The "Full Overview View" dropdown option — the canonical way the overview
    # image is tagged. Saved as 00_full.jpg (OVERVIEW_FILENAME) so view_ingest
    # classifies it as the "full" overview view (whole-part context, and the
    # image the post-build Overview Cross-Verification re-examines).
    "overview": (0, "full"),
    "full": (0, "full"),
}

app = FastAPI(title="MTI 2D->3D Pipeline UI")

# Static assets: the verbatim photo app (Tab 1) and the vendored 3D-viewer libs.
app.mount("/photoapp", StaticFiles(directory=str(WEBAPP_DIR / "photoapp"), html=True), name="photoapp")
app.mount("/vendor", StaticFiles(directory=str(WEBAPP_DIR / "vendor")), name="vendor")
app.mount("/static", StaticFiles(directory=str(WEBAPP_DIR / "static")), name="static")  # design tokens (shared by both documents)

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


def _deliver_run(out_dir: Path, part_name: str,
                 extra_files: list[Path] | None = None) -> dict:
    """Deliver a finished run's outputs to BOTH the project ``UI_Output/<part>/``
    and the user's ``~/Downloads/SolidWorksModel_Parts/<part>/`` so they are easy
    to find and open. ``extra_files`` (e.g. the untouched original upload) are
    copied alongside. Returns the paths that were written."""
    part = _sanitize(part_name)
    project = _flatten_copy(out_dir, DELIVER_DIR / part)
    downloads = _flatten_copy(out_dir, DOWNLOADS_DIR / part)
    for extra in extra_files or []:
        try:
            if extra and Path(extra).is_file():
                for dest in (project, downloads):
                    if dest:
                        shutil.copy2(extra, Path(dest) / Path(extra).name)
        except Exception:
            pass
    return {
        "project": str(project) if project else None,
        "downloads": str(downloads) if downloads else None,
    }


def _start_run(cmd: list[str], output_dir: Path, run_id: str | None = None,
               deliver_name: str | None = None,
               extra_files: list[Path] | None = None) -> str:
    """Spawn the pipeline CLI and stream its output into RUNS[id]['lines'].

    When ``deliver_name`` is given, a successful run's outputs are also copied into
    ``UI_Output/<deliver_name>/`` so they are easy to find and open."""
    run_id = run_id or uuid.uuid4().hex[:12]
    output_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "lines": [], "done": False, "exit": None, "output": output_dir,
        "proc": None, "started": time.time(), "finished": None, "cancelled": False,
        "deliver_name": deliver_name, "delivered": None, "delivered_downloads": None,
        "extra_files": extra_files or [],
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
            # Deliver clean, openable copies for a completed, non-cancelled run:
            # one in the project (UI_Output/) and one in the user's Downloads
            # folder. Exit 8 = completed with review flags — the outputs (model,
            # reports, macros) exist and are delivered too; only a hard failure
            # or a cancel skips delivery.
            if state["exit"] in (0, 8) and not state["cancelled"] and deliver_name:
                paths = _deliver_run(output_dir, deliver_name,
                                     extra_files=state.get("extra_files"))
                state["delivered"] = paths.get("project")
                state["delivered_downloads"] = paths.get("downloads")
                if paths.get("project"):
                    state["lines"].append(f"[delivered outputs to] {paths['project']}")
                if paths.get("downloads"):
                    state["lines"].append(f"[delivered outputs to] {paths['downloads']}")
            state["done"] = True
            state["finished"] = time.time()
            # Persist the console transcript with the run so Sheet 4's Console
            # sub-tab works for historical runs, not just the live one.
            try:
                (output_dir / "ui_console.log").write_text(
                    "\n".join(state["lines"]) + "\n", encoding="utf-8")
            except OSError:
                pass

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


@app.get("/api/codex/health")
def codex_health():
    """Codex CLI install/auth status + resolved mode (for the startup warning).
    Never raises — a down Codex is a normal reported state, not an error."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(PROJECT_DIR))
        from pipeline import codex_client
        return codex_client.health().as_dict()
    except Exception as e:
        return {"enabled": False, "installed": False, "authenticated": False,
                "mode": "stub", "model": "gpt-5.6-sol",
                "message": f"Codex health probe failed: {type(e).__name__}: {e}",
                "instructions": ["npm i -g @openai/codex", "codex login"]}


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
    and orders them; anything else keeps its own (sanitized) name and is left
    for the pipeline to warn about / skip."""
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

    used: set[str] = set()
    for i, up in enumerate(crops, start=1):
        stem = Path(up.filename or f"view{i}").stem
        fname = _crop_filename(stem, i)
        # Avoid clobbering when two crops map to the same view.
        if fname in used:
            fname = f"{Path(fname).stem}_{i}{Path(fname).suffix}"
        used.add(fname)
        (in_dir / fname).write_bytes(await up.read())

    # Full original drawing: kept for Tab 2's Overview panel AND fed into extraction
    # as whole-part context (saved in the views folder as the "full" overview view).
    # A crop tagged "Full Overview View" is canonical and wins over the source mirror.
    if source is not None:
        src_bytes = await source.read()
        (run_root / "source.jpg").write_bytes(src_bytes)
        if OVERVIEW_FILENAME not in used:
            (in_dir / OVERVIEW_FILENAME).write_bytes(src_bytes)

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
    """The REAL (SolidWorks) STL — the CadQuery prevalidation STL is excluded."""
    stls = [p for p in sorted(out.rglob("*.stl")) if p.name != "prevalidation.stl"] \
        if out.exists() else []
    return stls[0] if stls else None


def _find_preval_stl(out: Path) -> Path | None:
    hits = sorted(out.rglob("prevalidation.stl")) if out.exists() else []
    return hits[0] if hits else None


def _active_stl(out: Path) -> tuple[Path | None, str | None]:
    """(path, source): the SolidWorks STL when built, else the pre-validated
    CadQuery STL (shown with a badge until the real one replaces it)."""
    p = _find_stl(out)
    if p is not None:
        return p, "solidworks"
    p = _find_preval_stl(out)
    if p is not None:
        return p, "prevalidation"
    return None, None


@app.get("/api/runs/{run_id}/model.stl")
def run_stl(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(404, "Unknown run id")
    p, _src = _active_stl(RUNS[run_id]["output"])
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


def _review_items(build_plan: Path | None, review_txt: Path | None) -> dict:
    """Engineering Flags tab payload: the structured severity-ranked items from
    build_plan.json (single source of truth) plus the plain-text report."""
    items = []
    if build_plan is not None:
        try:
            import json as _json

            plan = _json.loads(build_plan.read_text(encoding="utf-8"))
            items = plan.get("engineering_review", []) or []
        except Exception:
            items = []
    text = None
    if review_txt is not None:
        try:
            text = review_txt.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = None
    return {"present": bool(items) or text is not None, "items": items, "text": text}


def _usage_summary(out: Path) -> dict:
    """Token/Cost tab payload from the run's own ledger files (source of truth:
    the same token_usage_log.* the CLI writes)."""
    import json as _json

    jsonl = out / "token_usage_log.jsonl"
    txt = out / "token_usage_log.txt"
    rows = []
    if jsonl.is_file():
        for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(_json.loads(line))
                except Exception:
                    continue
    total_cost = round(sum(r.get("cost_usd", 0.0) for r in rows), 4)
    ledger_text = None
    if txt.is_file():
        try:
            ledger_text = txt.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return {
        "present": bool(rows) or ledger_text is not None,
        "rows": rows,
        "last_run": rows[-1] if rows else None,
        "total_cost_usd": total_cost,
        "text": ledger_text,
    }


def _file_listing(out: Path) -> list[dict]:
    """Files tab payload: every output file with its relative path and size."""
    files = []
    if out.exists():
        for p in sorted(out.rglob("*")):
            if p.is_file() and ".extraction_cache" not in p.parts:
                files.append({"name": p.relative_to(out).as_posix(),
                              "size": p.stat().st_size})
    return files


def _mm_summary(out: Path) -> dict:
    """Must-meet checklist payload for Tab 2: the post-build
    constraint_verification.json wins; prevalidation_report.json is shown until
    the real SolidWorks build replaces it. macro_result.json failures (exact
    failing feature) are folded into ``failed`` so the banner is precise —
    never a generic exit code."""
    import json as _json

    def _load(p: Path | None):
        if p is None:
            return None
        try:
            return _json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None

    cv = _load(_first(out, ["constraint_verification.json"]))
    pv = _load(_first(out, ["prevalidation_report.json"]))
    mc = _load(_first(out, ["must_meet_constraints.json"])) or {}
    data, stage = (cv, "post_build") if cv else (pv, "prevalidation" if pv else None)

    results = (data or {}).get("constraints", []) or []
    failed = list((data or {}).get("failed_constraints", []) or [])
    if not failed and (data or {}).get("error"):
        failed = [f"verification error: {data['error']}"]

    # macro_result.json: the COM builder writes {"results": [...]}; the VBA
    # macros append JSON Lines. Read both shapes.
    macro_results: list = []
    mr_path = _first(out, ["macro_result.json"])
    if mr_path is not None:
        raw = _load(mr_path)
        if isinstance(raw, dict):
            macro_results = raw.get("results", []) or []
        else:
            try:
                for line in mr_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line:
                        macro_results.append(_json.loads(line))
            except Exception:
                macro_results = []
    for r in macro_results:
        if str(r.get("status", "")).upper() == "FAIL":
            failed.append(f"feature {r.get('feature', '?')} FAILED: {r.get('detail', '')}")

    return {
        "present": bool(results or mc.get("constraints")),
        "stage": stage,
        "ok": (data or {}).get("ok"),
        "results": results,
        "constraints": mc.get("constraints", []),
        "failed": failed,
        "macro_results": macro_results,
    }


def _overview_analysis_summary(out: Path) -> dict:
    """Stage 1.5 payload for the collapsible panel under the Full Overview View:
    the parsed overview_analysis.json (holistic cross-view read of the sheet)."""
    import json as _json

    p = _first(out, ["overview_analysis.json"])
    if p is None:
        return {"present": False}
    try:
        data = _json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"present": False}
    return {"present": True, "name": p.name, "data": data}


def _codex_summary(out: Path) -> dict:
    """Codex macro manifest + overall-shape-check for the UI (macro writing only;
    there is no independent Codex validation stage)."""
    shape = None
    p = out / "codex_shape_check.json"
    try:
        shape = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except Exception:
        shape = None
    manifest = None
    for m in out.rglob("codex_manifest.json"):
        try:
            manifest = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            manifest = None
        break
    return {"present": bool(shape or manifest), "shape_check": shape, "manifest": manifest}


def _categorize_output(out: Path) -> dict:
    """Categorise a pipeline output dir into the shape the output tabs consume.
    Shared by run-id-scoped and per-part outputs so both render identically —
    the UI reads the SAME files written to disk (one source of truth)."""
    extraction = _first(out, ["*_extraction.json", "*extraction*.json"], exclude=["resolved"])
    resolved = _first(out, ["*_resolved_extraction.json", "*resolved*.json"])
    build_plan = _first(out, ["*build_plan*.json", "**/build_plan*.json"])
    verification = _first(out, ["*verification_report*.txt", "*verification*.txt"])
    model_check = _first(out, ["*model_check*.txt", "*model_validation*.txt", "*validation_report*.txt"])
    review_txt = _first(out, ["*_engineering_review.txt"])

    macros_files = []
    if out.exists():
        for m in sorted(out.rglob("*.vba")):
            try:
                txt = m.read_text(encoding="utf-8", errors="replace") if m.stat().st_size < _INLINE_CAP else "(too large to inline)"
            except Exception:
                txt = "(unreadable)"
            macros_files.append({"name": m.relative_to(out).as_posix(), "text": txt})

    stl, stl_source = _active_stl(out)
    has_any = any(x is not None for x in (extraction, resolved, build_plan, verification, model_check)) or bool(macros_files)
    return {
        "has_any": has_any,
        "stl_mtime": int(stl.stat().st_mtime) if stl else 0,
        "stl_source": stl_source,
        "categories": {
            "extraction": _cat(extraction),
            "resolved": _cat(resolved),
            "build_plan": _cat(build_plan),
            "verification": _cat(verification),
            "model_check": _cat(model_check),
            "review": _review_items(build_plan, review_txt),
            "usage": _usage_summary(out),
            "macros": {"present": bool(macros_files), "files": macros_files},
            "sldprt": {"present": bool(_first(out, ["*.sldprt"]))},
            "stl": {"present": stl is not None, "name": stl.name if stl else None,
                    "source": stl_source},
            "must_meet": _mm_summary(out),
            "overview_analysis": _overview_analysis_summary(out),
            "codex": _codex_summary(out),
            "console": _cat(_first(out, ["ui_console.log"])),
            "files": {"present": out.exists(), "list": _file_listing(out)},
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


# ── DWG intake: convert to a raster PNG server-side ────────────────────────────

# Common install locations of the free ODA File Converter (DWG -> DXF).
_ODA_GLOBS = (
    r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
    r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
)


def _find_oda_converter() -> str | None:
    import glob as _glob

    exe = shutil.which("ODAFileConverter")
    if exe:
        return exe
    for pattern in _ODA_GLOBS:
        hits = sorted(_glob.glob(pattern))
        if hits:
            return hits[-1]  # newest version
    return None


def _dxf_layouts(dxf_path: Path) -> list[str]:
    """Non-empty layouts of a DXF: 'Model' plus any paper-space sheets that
    actually contain entities (empty default sheets are noise, not choices)."""
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    names = []
    if len(doc.modelspace()) > 0:
        names.append("Model")
    for name in doc.layout_names_in_taborder():
        if name == "Model":
            continue
        try:
            if len(doc.layout(name)) > 0:
                names.append(name)
        except Exception:
            continue
    return names or ["Model"]


def _render_dxf_to_png(dxf_path: Path, png_path: Path, layout: str = "Model") -> None:
    """Render one DXF layout/sheet to a high-resolution PNG via ezdxf+matplotlib."""
    import matplotlib

    matplotlib.use("Agg")
    import ezdxf
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    import matplotlib.pyplot as plt

    doc = ezdxf.readfile(str(dxf_path))
    space = doc.modelspace() if layout in ("", "Model") else doc.layout(layout)
    fig = plt.figure(figsize=(20, 14))
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    # Force BLACK line work on a WHITE page. Without this, entities drawn in
    # the DXF default color (7 = white on AutoCAD's dark canvas) render
    # white-on-white and the "converted" image is blank paper.
    try:
        from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration

        config = Configuration(background_policy=BackgroundPolicy.WHITE,
                               color_policy=ColorPolicy.BLACK)
        Frontend(ctx, MatplotlibBackend(ax), config=config).draw_layout(space, finalize=True)
    except ImportError:  # older ezdxf without Configuration policies
        ctx.set_current_layout(space)
        Frontend(ctx, MatplotlibBackend(ax)).draw_layout(space, finalize=True)
    ax.set_facecolor("#ffffff")
    fig.patch.set_facecolor("#ffffff")
    fig.savefig(str(png_path), dpi=150, facecolor="#ffffff")
    plt.close(fig)


# Converted CAD artifacts are cached next to the uploads and every conversion is
# logged (source format, tool, output) so a bad artifact is traceable later.
CONVERT_CACHE = WEBAPP_DIR / ".convert_cache"
CONVERSION_LOG = WEBAPP_DIR / "conversion_log.jsonl"


def _log_conversion(source_name: str, source_format: str, tool: str,
                    output: str, n_bytes: int) -> None:
    import datetime

    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source_name, "source_format": source_format,
        "tool": tool, "output": output, "bytes": n_bytes,
    }
    try:
        with CONVERSION_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


_EDRAWINGS_SUFFIXES = (".edrw", ".eprt", ".easm")


@app.post("/api/convert-dwg")
async def convert_dwg(file: UploadFile = File(...), layout: str = Form("")):
    """Convert an uploaded CAD file (DWG, DXF, or eDrawings) to a viewable PNG.

    - DXF renders directly (ezdxf+matplotlib). DWG converts DWG→DXF first via the
      free ODA File Converter; when it is missing a clear 422 explains exactly
      what to install — never a silent failure.
    - Multi-sheet DWG/DXF: when the file has more than one non-empty layout and
      no ``layout`` was chosen, a JSON body {"layouts": [...]} is returned so the
      client can offer a sheet picker (mirrors multi-page PDF handling).
    - eDrawings (.edrw/.eprt/.easm): a true interactive viewer is not feasible
      server-side, so the largest embedded raster preview is extracted and
      returned with ``X-Static-Preview: 1`` — the client must label it a static
      preview, not the interactive model.

    Responses carry ``X-Source-Format`` / ``X-Conversion-Tool``; every
    conversion is cached (.convert_cache) and logged (conversion_log.jsonl).
    """
    import hashlib
    import tempfile

    name = Path(file.filename or "drawing.dwg").name
    suffix = Path(name).suffix.lower()
    if suffix not in (".dwg", ".dxf") + _EDRAWINGS_SUFFIXES:
        raise HTTPException(
            400, f"Expected a .dwg, .dxf, or eDrawings (.edrw/.eprt/.easm) file, got '{suffix}'.")

    blob = await file.read()
    digest = hashlib.sha256(blob + layout.encode("utf-8")).hexdigest()[:32]
    CONVERT_CACHE.mkdir(exist_ok=True)
    cached = CONVERT_CACHE / f"{digest}.png"
    src_format = "edrawings" if suffix in _EDRAWINGS_SUFFIXES else suffix.lstrip(".")
    headers = {"X-Source-Format": src_format}
    if src_format == "edrawings":
        headers["X-Static-Preview"] = "1"

    if cached.is_file():
        _log_conversion(name, src_format, "cache", cached.name, cached.stat().st_size)
        headers["X-Conversion-Tool"] = "cache"
        return Response(cached.read_bytes(), media_type="image/png", headers=headers)

    # ── eDrawings: extract the embedded static preview ──────────────────────
    if src_format == "edrawings":
        from preview_extract import extract_preview_png

        png_bytes = extract_preview_png(blob)
        if png_bytes is None:
            raise HTTPException(
                422,
                f"'{name}' contains no extractable raster preview. A server-side "
                "interactive eDrawings view is not available in this environment — "
                "open the file in eDrawings and export a PDF or PNG (File → Save As), "
                "then upload that.",
            )
        cached.write_bytes(png_bytes)
        _log_conversion(name, "edrawings", "preview_extract(embedded raster)",
                        cached.name, len(png_bytes))
        headers["X-Conversion-Tool"] = "preview_extract"
        return Response(png_bytes, media_type="image/png", headers=headers)

    # ── DWG/DXF ──────────────────────────────────────────────────────────────
    try:
        import ezdxf  # noqa: F401
    except ImportError:
        raise HTTPException(
            422,
            "DWG/DXF support needs the 'ezdxf' and 'matplotlib' Python packages. "
            "Run: pip install -r webapp/requirements-ui.txt and restart the UI.",
        )

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        src = tdir / name
        src.write_bytes(blob)
        tool = "ezdxf+matplotlib"

        if suffix == ".dwg":
            # No-ODA engine chain: ezdwg (pip) → SolidWorks translator (COM,
            # handles even R13 files) → ODA if it happens to be installed.
            sys.path.insert(0, str(PROJECT_DIR))
            from pipeline.vector_extract.dwg_convert import detect_dwg_version, dwg_to_dxf

            conv_notes: list[str] = []
            dxf_out = tdir / (src.stem + "_converted.dxf")
            engine = dwg_to_dxf(src, dxf_out, conv_notes)
            if engine is None:
                version = detect_dwg_version(src)
                raise HTTPException(
                    422,
                    f"Could not convert '{name}' (DWG {version}). Tried: "
                    + " | ".join(conv_notes)
                    + " — export the drawing as DXF or PDF and upload that instead.",
                )
            src = dxf_out
            tool = f"{engine} + ezdxf+matplotlib"

        # Multi-sheet: offer the choice before rendering anything (like PDF pages).
        try:
            layouts = _dxf_layouts(src)
        except Exception as e:
            raise HTTPException(422, f"Could not read '{name}': {type(e).__name__}: {e}")
        if len(layouts) > 1 and not layout:
            return JSONResponse({"layouts": layouts, "source_format": src_format},
                                headers=headers)
        chosen = layout or layouts[0]
        if chosen not in layouts:
            raise HTTPException(400, f"Unknown layout '{chosen}'; available: {layouts}")

        png = tdir / "render.png"
        try:
            _render_dxf_to_png(src, png, layout=chosen)
        except Exception as e:
            raise HTTPException(422, f"Could not render '{name}' ({chosen}): "
                                     f"{type(e).__name__}: {e}")
        data = png.read_bytes()
        cached.write_bytes(data)
        _log_conversion(name, src_format, tool, f"{cached.name} (layout={chosen})", len(data))
        headers["X-Conversion-Tool"] = tool
        headers["X-Layout"] = chosen
        return Response(data, media_type="image/png", headers=headers)


def _convert_cad_to_png(blob: bytes, name: str) -> tuple[bytes, str]:
    """Convert DWG / DXF / eDrawings bytes to a viewable PNG (first layout, or the
    embedded eDrawings preview) so Claude can read it for extraction. Returns
    ``(png_bytes, tool)``; raises ``ValueError`` with a clear message on failure.

    Shared by ``/api/convert-dwg`` (single upload) and the folder-of-parts upload
    so a batch of DWG/eDrawings overview drawings extracts EXACTLY the same way a
    single interactive upload does. Cached in ``.convert_cache`` like the endpoint."""
    import hashlib
    import tempfile

    suffix = Path(name).suffix.lower()
    src_format = "edrawings" if suffix in _EDRAWINGS_SUFFIXES else suffix.lstrip(".")
    CONVERT_CACHE.mkdir(exist_ok=True)
    cached = CONVERT_CACHE / f"{hashlib.sha256(blob).hexdigest()[:32]}.png"
    if cached.is_file():
        _log_conversion(name, src_format, "cache", cached.name, cached.stat().st_size)
        return cached.read_bytes(), "cache"

    if src_format == "edrawings":
        from preview_extract import extract_preview_png

        png = extract_preview_png(blob)
        if png is None:
            raise ValueError(
                f"'{name}': no extractable eDrawings preview — open it in eDrawings and "
                f"export a PDF or PNG (File → Save As), then upload that.")
        cached.write_bytes(png)
        _log_conversion(name, "edrawings", "preview_extract(embedded raster)", cached.name, len(png))
        return png, "preview_extract"

    try:
        import ezdxf  # noqa: F401
    except ImportError:
        raise ValueError("DWG/DXF support needs 'ezdxf' + 'matplotlib' "
                         "(pip install -r webapp/requirements-ui.txt, then restart the UI).")

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        src = tdir / Path(name).name
        src.write_bytes(blob)
        tool = "ezdxf+matplotlib"
        if suffix == ".dwg":
            sys.path.insert(0, str(PROJECT_DIR))
            from pipeline.vector_extract.dwg_convert import detect_dwg_version, dwg_to_dxf

            notes: list[str] = []
            dxf_out = tdir / (src.stem + "_converted.dxf")
            engine = dwg_to_dxf(src, dxf_out, notes)
            if engine is None:
                raise ValueError(
                    f"Could not convert '{name}' (DWG {detect_dwg_version(src)}). Tried: "
                    + " | ".join(notes) + " — export the drawing as DXF or PDF and upload that.")
            src = dxf_out
            tool = f"{engine} + ezdxf+matplotlib"
        try:
            layouts = _dxf_layouts(src)
        except Exception as e:
            raise ValueError(f"Could not read '{name}': {type(e).__name__}: {e}")
        chosen = layouts[0] if layouts else "Model"
        png = tdir / "render.png"
        try:
            _render_dxf_to_png(src, png, layout=chosen)
        except Exception as e:
            raise ValueError(f"Could not render '{name}' ({chosen}): {type(e).__name__}: {e}")
        data = png.read_bytes()
        cached.write_bytes(data)
        _log_conversion(name, src_format, tool, f"{cached.name} (layout={chosen})", len(data))
        return data, tool


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

# Image types accepted in a part's views folder (crop flow writes .jpg; the
# parts-folder upload keeps whatever the preprocessed folders contain).
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
# CAD types that must be CONVERTED (DWG/DXF/eDrawings) before Claude can read
# them, and the document type (PDF) the pipeline reads directly.
CAD_EXTS = {".dwg", ".dxf"} | set(_EDRAWINGS_SUFFIXES)
PDF_EXTS = {".pdf"}
# Everything the folder-of-parts upload accepts as a drawing (each becomes a
# part's full/overview drawing; CAD is converted, PDF/image kept as-is).
DRAWING_EXTS = IMG_EXTS | PDF_EXTS | CAD_EXTS
# Rasterizable-for-preview types (a PDF overview has no image on disk but can be
# rendered for the thumbnail + overview badge).
PREVIEWABLE_EXTS = IMG_EXTS | PDF_EXTS
# Filename stems view_ingest treats as the overview/full drawing.
_OVERVIEW_STEMS = ("full", "overview", "isometric", "iso")


def _part_images(pdir: Path) -> list[Path]:
    return sorted(p for p in pdir.iterdir()
                  if p.is_file() and p.suffix.lower() in IMG_EXTS)


def _part_previewables(pdir: Path) -> list[Path]:
    """Files that can stand in as the overview preview (images + PDFs)."""
    return sorted(p for p in pdir.iterdir()
                  if p.is_file() and p.suffix.lower() in PREVIEWABLE_EXTS)


def _is_overview_image(p: Path, part_name: str) -> bool:
    stem = p.stem.lower()
    norm = "".join(c for c in stem if c.isalnum())
    part_norm = "".join(c for c in part_name.lower() if c.isalnum())
    return (p.name == OVERVIEW_FILENAME
            or any(k in stem for k in _OVERVIEW_STEMS)
            or (bool(part_norm) and norm == part_norm))


def _find_overview_image(pdir: Path) -> Path | None:
    """The part's overview drawing: 00_full.jpg from the crop flow, else the
    file the pipeline itself would classify as the full/overview view (an image
    OR a PDF full-sheet drawing from the folder-of-parts upload)."""
    exact = pdir / OVERVIEW_FILENAME
    if exact.is_file():
        return exact
    previewables = _part_previewables(pdir)
    for p in previewables:
        if _is_overview_image(p, pdir.name):
            return p
    # A single full-sheet drawing (image or PDF) IS the overview even if its name
    # matched no keyword (folder-of-parts upload names the file after the part).
    return previewables[0] if len(previewables) == 1 else None


def _list_parts(sdir: Path) -> list[dict]:
    if not sdir.is_dir():
        return []
    parts = []
    for pdir in sorted(p for p in sdir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        views = sorted(v.name for v in _part_images(pdir)
                       if not _is_overview_image(v, pdir.name))
        out = pdir / "output"
        fmt_file = pdir / ".source_format"
        # The must-meet spec (authoritative) wins; legacy notes.txt still shows.
        notes_text = ""
        for nf in (pdir / "must_meet_spec.txt", pdir / "notes.txt"):
            if nf.is_file():
                notes_text = nf.read_text(encoding="utf-8", errors="replace")[:20000]
                break
        parts.append({
            "name": pdir.name,
            "n_views": len(views),
            "views": views,
            "has_output": out.is_dir() and _categorize_output(out)["has_any"],
            "has_overview": _find_overview_image(pdir) is not None,
            "source_format": fmt_file.read_text(encoding="utf-8").strip()
                             if fmt_file.is_file() else "",
            "notes": notes_text,
        })
    return parts


def _write_thumbnail(sdir: Path, part_name: str, image_path: Path) -> None:
    """Small JPEG thumbnail for the parts list (never fatal). Handles PDFs by
    rasterizing the first page the same way the pipeline does."""
    try:
        import base64
        import io

        from PIL import Image

        thumbs = sdir / ".thumbs"
        thumbs.mkdir(exist_ok=True)
        if image_path.suffix.lower() == ".pdf":
            from utils.image_prep import prepare_image

            prepared = prepare_image(str(image_path), page=1, return_details=True)
            im = Image.open(io.BytesIO(base64.b64decode(prepared.base64)))
        else:
            im = Image.open(image_path)
        im = im.convert("RGB")
        im.thumbnail((360, 360))
        im.save(thumbs / f"{part_name}.jpg", "JPEG", quality=85)
    except Exception:
        pass


@app.post("/api/session")
def new_session():
    sid = uuid.uuid4().hex[:12]
    (PARTS_DIR / sid).mkdir(parents=True, exist_ok=True)
    return {"session": sid}


@app.post("/api/parts")
async def add_part(
    session: str = Form(...),
    part: str = Form("drawing"),
    source_format: str = Form(""),
    notes: str = Form(""),
    source: UploadFile | None = File(None),
    original: UploadFile | None = File(None),
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
    # Remember what kind of file this part came from (PDF / DWG / eDrawings /
    # image) so the verification view can label the source correctly.
    if source_format.strip():
        (pdir / ".source_format").write_text(source_format.strip()[:64], encoding="utf-8")

    # Human-authored MUST-MEET SPECIFICATIONS (authoritative, tier 0). Saved as
    # must_meet_spec.txt (Stage 2.6 parses it into MM constraints that override
    # vision extraction) AND as notes.txt (legacy per-line grading) — same text,
    # discovered by the pipeline in the views folder.
    _write_spec_files(pdir, notes)

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
        # A crop explicitly tagged "Full Overview View" is the canonical overview
        # and takes precedence — never clobber it with the mirrored source.
        if OVERVIEW_FILENAME not in used:
            (pdir / OVERVIEW_FILENAME).write_bytes(src_bytes)

    # Keep the untouched original upload (PDF/JPG/DWG) OUTSIDE the views folder
    # so it is never mis-classified as a view; it is delivered with the outputs.
    if original is not None:
        odir = sdir / ORIGINALS_DIRNAME
        odir.mkdir(exist_ok=True)
        ext = Path(original.filename or "").suffix.lower() or ".bin"
        for old in odir.glob(f"{part_name}.*"):
            old.unlink()
        (odir / f"{part_name}{ext}").write_bytes(await original.read())

    return {"session": _safe_session(session), "part": part_name, "parts": _list_parts(sdir)}


@app.get("/api/parts")
def list_parts(session: str):
    return {"session": _safe_session(session), "parts": _list_parts(_session_dir(session))}


def _store_part_drawing(pdir: Path, sdir: Path, part_name: str, fname: str,
                        data: bytes, as_overview: bool) -> str:
    """Write an uploaded drawing into a part's views folder so the pipeline can
    read it. Returns the recorded source format. Raises ``ValueError`` when a CAD
    file cannot be converted.

    * image  -> written as-is (Claude reads it directly);
    * PDF     -> written as-is (the pipeline rasterizes it AND uses it as the
                 exact vector source for hole positions);
    * DWG/DXF -> converted to a PNG (vision extraction) and the original kept
                 in the folder (exact vector hole extraction / delivery);
    * eDrawings -> the embedded preview PNG is extracted for vision; the original
                 is kept in .originals for delivery (no vector geometry available).

    When ``as_overview`` (folder-of-parts: one full-sheet drawing per part), the
    stored image is named after the part so ``view_ingest`` classifies it as the
    'full' overview view."""
    ext = Path(fname).suffix.lower()
    stem = part_name if as_overview else Path(fname).stem

    if ext in CAD_EXTS:
        png, _tool = _convert_cad_to_png(data, fname)   # raises ValueError on failure
        (pdir / f"{stem}.png").write_bytes(png)
        if ext in (".dwg", ".dxf"):
            # Keep the original vector file IN the part folder: the pipeline globs
            # it as the exact hole-position source (vector owns position).
            (pdir / fname).write_bytes(data)
            return ext.lstrip(".")
        # eDrawings: original is not vector-usable — keep it only for delivery.
        odir = sdir / ORIGINALS_DIRNAME
        odir.mkdir(exist_ok=True)
        (odir / f"{part_name}{ext}").write_bytes(data)
        return "edrawings"

    if ext in PDF_EXTS:
        (pdir / f"{stem}.pdf").write_bytes(data)
        return "pdf"

    # Plain raster image.
    (pdir / f"{stem}{ext}").write_bytes(data)
    return "image"


@app.post("/api/parts/upload-batch")
async def upload_parts_batch(
    session: str = Form(...),
    paths: list[str] = Form(...),
    files: list[UploadFile] = File(...),
):
    """Upload a FOLDER OF PARTS. Two layouts are auto-detected:

    * **Nested** — each subfolder is one preprocessed part (view images named per
      the --views-folder conventions + optional notes/must_meet_spec txt). Files
      are written verbatim, the exact layout the CLI runs unchanged.
    * **Flat** — the selected folder holds one full/overview DRAWING PER PART
      (``A001211E.pdf``, ``bracket.dwg``, ``plate.png`` …). Each file becomes its
      own part; DWG/DXF/eDrawings are converted to a viewable PNG (with the
      original kept for exact vector hole extraction), PDFs/images are used as-is.

    Accepts .pdf, .png/.jpg/.jpeg/.webp/.bmp/.tif, .dwg, .dxf, and eDrawings
    (.edrw/.eprt/.easm). ``paths`` carries each file's webkitRelativePath,
    parallel to ``files``."""
    if len(paths) != len(files):
        raise HTTPException(400, "paths and files must be parallel lists")
    sdir = _session_dir(session)
    sdir.mkdir(parents=True, exist_ok=True)

    TXT_EXTS = {".txt"}
    norm = [[c for c in p.replace("\\", "/").split("/") if c and c not in (".", "..")]
            for p in paths]
    # Nested (root/part/file) vs flat (root/file = one part per file).
    nested = any(len(c) >= 3 for c in norm)

    added: dict[str, int] = {}
    skipped = 0
    errors: list[str] = []
    for comps, up in zip(norm, files):
        if len(comps) < 2:
            skipped += 1
            continue
        fname = comps[-1]
        ext = Path(fname).suffix.lower()
        if fname.startswith("."):
            skipped += 1
            continue

        if nested:
            part_raw = comps[-2] if len(comps) >= 3 else comps[0]
            as_overview = False
        else:
            # Flat: each drawing file is its own part; a stray top-level txt has
            # no part to attach to, so it is skipped.
            if ext in TXT_EXTS:
                skipped += 1
                continue
            part_raw = Path(fname).stem
            as_overview = True
        if part_raw.startswith("."):
            skipped += 1
            continue
        part_name = _sanitize(part_raw)
        pdir = sdir / part_name

        data = await up.read()
        if not data:
            skipped += 1
            continue

        if ext in TXT_EXTS:
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / fname).write_bytes(data)
            added.setdefault(part_name, added.get(part_name, 0))
            continue
        if ext not in DRAWING_EXTS:
            skipped += 1
            continue

        pdir.mkdir(parents=True, exist_ok=True)
        try:
            fmt = _store_part_drawing(pdir, sdir, part_name, fname, data, as_overview)
        except ValueError as e:
            errors.append(str(e))
            skipped += 1
            continue
        if as_overview:
            (pdir / ".source_format").write_text(fmt, encoding="utf-8")
        added[part_name] = added.get(part_name, 0) + 1

    # notes.txt mirror (legacy grading) + thumbnails for every touched part.
    for part_name in added:
        pdir = sdir / part_name
        spec = pdir / "must_meet_spec.txt"
        legacy = pdir / "notes.txt"
        if spec.is_file() and not legacy.is_file():
            legacy.write_text(spec.read_text(encoding="utf-8", errors="replace"),
                              encoding="utf-8")
        thumb_src = _find_overview_image(pdir)
        if thumb_src is None:
            imgs = _part_images(pdir)
            thumb_src = imgs[0] if imgs else None
        if thumb_src is not None:
            _write_thumbnail(sdir, part_name, thumb_src)

    if not added:
        detail = "No part folders/drawings were found in the selected folder."
        if errors:
            detail += " Conversion errors: " + " | ".join(errors[:5])
        raise HTTPException(400, detail)
    return {"session": _safe_session(session), "added": sorted(added),
            "n_files": sum(added.values()), "n_skipped": skipped,
            "errors": errors[:20], "parts": _list_parts(sdir)}


@app.get("/api/parts/{session}/{part}/thumb.jpg")
def part_thumb(session: str, part: str):
    p = _session_dir(session) / ".thumbs" / f"{_sanitize(part)}.jpg"
    if not p.is_file():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(str(p), media_type="image/jpeg")


def _write_spec_files(pdir: Path, notes: str) -> None:
    """Persist the operator's must-meet text as BOTH must_meet_spec.txt (the
    Stage 2.6 authoritative input, kept with the run) and notes.txt (legacy
    per-line grading). Clearing the text removes both."""
    text = (notes or "").strip()[:20000]
    for fname in ("must_meet_spec.txt", "notes.txt"):
        p = pdir / fname
        if text:
            p.write_text(text + "\n", encoding="utf-8")
        elif p.is_file():
            p.unlink()


@app.post("/api/parts/{session}/{part}/notes")
def update_part_notes(session: str, part: str, notes: str = Form("")):
    """Update a saved part's must-meet specifications in place (applied tier-0
    on the next run)."""
    pdir = _session_dir(session) / _sanitize(part)
    if not pdir.is_dir():
        raise HTTPException(404, "Unknown part")
    _write_spec_files(pdir, notes)
    return {"part": _sanitize(part), "parts": _list_parts(_session_dir(session))}


_IMG_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".bmp": "image/bmp",
              ".tif": "image/tiff", ".tiff": "image/tiff"}


@app.post("/api/parts/{session}/{part}/learning-log")
def part_learning_log(session: str, part: str):
    """Push every flag & failure from this part's latest run into the repo's
    Learning Loop/ folder as one organized report (the "Save all flags"
    button). Reads the run artifacts, reconstructs the gate reasons, and reuses
    the same generator the pipeline runs automatically."""
    import json as _json

    out = _session_dir(session) / _sanitize(part) / "output"
    if not out.is_dir():
        raise HTTPException(400, "This part has no run output yet — run it first.")

    # Locate the folder that actually holds the artifacts (the built part number
    # subfolder), and read the structured engineering review from the build plan.
    anchor = _first(out, ["*_build_plan.json", "*_engineering_review.txt", "*_extraction.json"])
    part_dir = anchor.parent if anchor is not None else out
    review: list = []
    bp = _first(out, ["*_build_plan.json"])
    if bp is not None:
        try:
            review = _json.loads(bp.read_text(encoding="utf-8")).get("engineering_review", []) or []
        except Exception:
            review = []

    # Reconstruct the gate reasons from the artifacts (mirrors the pipeline).
    mm = _mm_summary(out)
    gate: list[str] = list(mm.get("failed") or [])
    if any(i.get("severity") == "CRITICAL" and i.get("source") == "overview_analysis" for i in review):
        gate.append("overview verification found CRITICAL gap(s)")
    for i in review:
        if i.get("source") == "requirement" and str(i.get("status", "")).lower() == "unmet":
            gate.append(f"unmet requirement {i.get('id', '')}: {i.get('what', '')}"[:200])
    status = "NOT READY" if gate else "READY"

    import os as _os

    from pipeline.extractor import DEFAULT_MODEL
    from pipeline.learning_loop import write_learning_log

    path = write_learning_log(part_dir, part_dir.name, status, gate, out,
                              model=_os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL)
    if path is None:
        raise HTTPException(500, "Could not write the Learning Loop report.")
    n_flags = len(review) + len(gate) + len(mm.get("failed") or [])
    return {"saved": True, "file": Path(path).name, "path": str(path),
            "flags": len(review), "gate_reasons": len(gate), "status": status}


@app.get("/api/parts/{session}/{part}/overview.jpg")
def part_overview(session: str, part: str):
    """The part's overview/full drawing for Tab 2's Overview panel: the crop
    flow's 00_full.jpg, or the file the pipeline classifies as the overview in
    an uploaded parts folder. Falls back to the first view image so selecting
    a part always shows its drawing. 404 only when the part has no images."""
    pdir = _session_dir(session) / _sanitize(part)
    p = _find_overview_image(pdir) if pdir.is_dir() else None
    if p is None and pdir.is_dir():
        imgs = _part_images(pdir)
        p = imgs[0] if imgs else None
    if p is None:
        raise HTTPException(404, "No overview image for this part")
    # A PDF overview can't be shown in an <img>; rasterize page 1 to PNG so the
    # panel displays it. Cache at SESSION level (never inside the part folder —
    # a stray .png there would be picked up as an extra view by --views-folder).
    if p.suffix.lower() == ".pdf":
        cache_dir = _session_dir(session) / ".overview_cache"
        cache_dir.mkdir(exist_ok=True)
        cache_png = cache_dir / f"{_sanitize(part)}.png"
        try:
            if not cache_png.is_file() or cache_png.stat().st_mtime < p.stat().st_mtime:
                import base64

                from utils.image_prep import prepare_image
                prepared = prepare_image(str(p), page=1, return_details=True)
                cache_png.write_bytes(base64.b64decode(prepared.base64))
            return FileResponse(str(cache_png), media_type="image/png")
        except Exception as e:
            log.warning("Could not rasterize PDF overview for %s: %s", part, e)
            raise HTTPException(404, "Overview PDF could not be rendered")
    return FileResponse(str(p), media_type=_IMG_MEDIA.get(p.suffix.lower(), "image/jpeg"))


@app.get("/api/parts/{session}/{part}/outputs")
def part_outputs(session: str, part: str):
    pdir = _session_dir(session) / _sanitize(part)
    if not pdir.is_dir():
        raise HTTPException(404, "Unknown part")
    payload = _categorize_output(pdir / "output")
    # Cumulative session cost: sum every part's ledger in this session.
    session_total = 0.0
    for other in _session_dir(session).iterdir():
        if other.is_dir() and not other.name.startswith("."):
            session_total += _usage_summary(other / "output").get("total_cost_usd", 0.0)
    payload["categories"]["usage"]["session_total_cost_usd"] = round(session_total, 4)
    delivered = DELIVER_DIR / _sanitize(part)
    downloads = DOWNLOADS_DIR / _sanitize(part)
    payload.update({
        "part": _sanitize(part),
        "ran": (pdir / "output").is_dir(),
        "delivered": str(delivered) if delivered.is_dir() else None,
        "delivered_downloads": str(downloads) if downloads.is_dir() else None,
    })
    return payload


@app.get("/api/parts/{session}/{part}/file/{name:path}")
def part_file(session: str, part: str, name: str):
    """Download any single output file of a part (Files tab links)."""
    out = (_session_dir(session) / _sanitize(part) / "output").resolve()
    target = (out / name).resolve()
    if not str(target).startswith(str(out)) or target == out:
        raise HTTPException(403, "Path outside part output")
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(str(target), filename=target.name)


@app.get("/api/parts/{session}/{part}/model.stl")
def part_stl(session: str, part: str):
    p, _src = _active_stl(_session_dir(session) / _sanitize(part) / "output")
    if p is None:
        raise HTTPException(404, "No STL for this part yet")
    return FileResponse(str(p), media_type="model/stl", filename=p.name)


# ── Pipeline Explainer — LOCAL-ONLY (Ollama) chat over a run's artifacts ──────
# Zero cost by design: the explainer module never imports the Anthropic client,
# never reads ANTHROPIC_API_KEY, and contacts nothing but localhost:11434.
def _part_output_dir(session: str, part: str) -> Path:
    return _session_dir(session) / _sanitize(part) / "output"


@app.get("/api/explainer/health")
def explainer_health():
    """Ollama status + the model this explainer will use (for the status dot,
    model name, and the auto-pull decision). Never raises — a down Ollama is a
    normal, reported state, not an error."""
    import explainer
    return explainer.health()


@app.post("/api/explainer/pull")
async def explainer_pull(request: Request):
    """Stream a one-time model download as NDJSON progress chunks."""
    import explainer
    try:
        body = await request.json()
    except Exception:
        body = {}
    model = (body.get("model") or "").strip() or explainer.choose_model()

    def gen():
        try:
            for chunk in explainer.pull_model(model):
                yield json.dumps(chunk) + "\n"
            yield json.dumps({"status": "success", "done": True, "model": model}) + "\n"
        except explainer.ExternalHostError as e:
            yield json.dumps({"error": str(e), "done": True}) + "\n"
        except explainer.OllamaUnavailable as e:
            yield json.dumps({"error": f"Ollama unavailable: {e}", "done": True}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/explainer/history")
def explainer_history(session: str, part: str):
    """Per-part persisted chat history + the zero-cost session footer."""
    import explainer
    out = _part_output_dir(session, part)
    return {"part": _sanitize(part), "history": explainer.load_history(out),
            "usage": explainer.usage_total(out)}


@app.post("/api/explainer/chat")
async def explainer_chat(request: Request):
    """Stream a grounded answer as NDJSON. Body: {session, part, question,
    history?}. Writes an export manifest on first ask so 'where did my files
    go?' is answerable, persists the exchange to per-part history."""
    import explainer
    try:
        body = await request.json()
    except Exception:
        body = {}
    session = body.get("session") or ""
    part = body.get("part") or ""
    question = (body.get("question") or "").strip()
    provider = (body.get("provider") or "local").strip().lower()
    if provider not in ("local", "claude"):
        provider = "local"
    if not session or not part:
        raise HTTPException(400, "session and part are required")
    if not question:
        raise HTTPException(400, "question is required")
    out = _part_output_dir(session, part)
    if not out.is_dir():
        raise HTTPException(404, "No completed run for this part yet")

    # Ensure an export manifest exists so delivery questions are citable.
    try:
        explainer.write_export_manifest(
            out, delivered_dirs=[DELIVER_DIR / _sanitize(part),
                                 DOWNLOADS_DIR / _sanitize(part)])
    except Exception:
        pass

    history = explainer.load_history(out)
    explainer.append_history(out, "user", question)

    def gen():
        answer_meta = {}
        for ev in explainer.chat(question, out, history=history, provider=provider):
            yield json.dumps(ev) + "\n"
            if ev.get("type") == "done":
                answer_meta = ev.get("meta", {})
        # Persist the assistant turn (best-effort; never breaks the stream).
        if answer_meta:
            explainer.append_history(out, "assistant", answer_meta.get("answer", ""),
                                     meta={k: v for k, v in answer_meta.items() if k != "answer"})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ── Shared run history: ONE persistent inventory of completed runs ─────────────
# Backs BOTH Sheet 2's "Select Model" dropdown and Sheet 4's "Select Run"
# dropdown, so the two can never disagree. Sourced from disk (webapp/parts/
# <session>/<part>/output), not in-memory state — runs survive server restarts
# and browser sessions.

def _run_presence(out: Path) -> dict:
    """File-existence flags for each output category (drives the ✓ indicators
    without reading file contents — cheap enough to scan every part)."""
    stl, _src = _active_stl(out)
    build_plan = _first(out, ["*build_plan*.json", "**/build_plan*.json"])
    return {
        "extraction": _first(out, ["*_extraction.json", "*extraction*.json"],
                             exclude=["resolved"]) is not None,
        "resolved": _first(out, ["*_resolved_extraction.json", "*resolved*.json"]) is not None,
        "build_plan": build_plan is not None,
        "verification": _first(out, ["*verification_report*.txt", "*verification*.txt"]) is not None,
        "flags": build_plan is not None
                 or _first(out, ["*_engineering_review.txt"]) is not None,
        "model_check": _first(out, ["*model_check*.txt", "*model_validation*.txt",
                                    "*validation_report*.txt"]) is not None,
        "macros": next(out.rglob("*.vba"), None) is not None if out.exists() else False,
        "cost": (out / "token_usage_log.jsonl").is_file() or (out / "token_usage_log.txt").is_file(),
        "console": _first(out, ["ui_console.log"]) is not None,
        "files": out.exists(),
        "stl": stl is not None,
        "overview_analysis": _first(out, ["overview_analysis.json"]) is not None,
    }


def _run_timestamp(out: Path) -> float:
    """When the run completed: the newest mtime among its output files."""
    newest = 0.0
    try:
        for p in out.rglob("*"):
            if ".extraction_cache" in p.parts or not p.is_file():
                continue
            newest = max(newest, p.stat().st_mtime)
    except OSError:
        pass
    return newest or (out.stat().st_mtime if out.exists() else 0.0)


@app.get("/api/run-history")
def run_history():
    """Every completed run across every session/part, newest first."""
    from datetime import datetime as _dt

    runs = []
    if PARTS_DIR.is_dir():
        for sdir in sorted(PARTS_DIR.iterdir()):
            if not sdir.is_dir() or sdir.name.startswith("."):
                continue
            for pdir in sorted(sdir.iterdir()):
                if not pdir.is_dir() or pdir.name.startswith("."):
                    continue
                out = pdir / "output"
                if not out.is_dir():
                    continue
                present = _run_presence(out)
                # A "completed run" left at least one core artifact behind.
                if not (present["extraction"] or present["build_plan"] or present["stl"]):
                    continue
                epoch = _run_timestamp(out)
                stamp = _dt.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "?"
                runs.append({
                    "session": sdir.name,
                    "part": pdir.name,
                    "key": f"{sdir.name}|{pdir.name}",
                    "label": f"{pdir.name} — run @ {stamp}",
                    "epoch": int(epoch),
                    "timestamp": stamp,
                    "output": str(out),
                    "present": present,
                })
    runs.sort(key=lambda r: -r["epoch"])
    return {"runs": runs}


@app.post("/api/run-history/clear")
def clear_run_history():
    """Clear ALL stored run outputs (the source of the shared run history).

    Deletes every ``webapp/parts/<session>/<part>/output`` folder, emptying
    both Sheet 2's model dropdown and Sheet 4's run dropdown. The saved part
    inputs (views, specs) are untouched — every part can simply be re-run —
    and the delivered copies in ``UI_Output/`` and ``~/Downloads`` are NOT
    touched (they are the user's deliverables, not the browsing store)."""
    import shutil

    removed = 0
    errors: list[str] = []
    if PARTS_DIR.is_dir():
        for sdir in PARTS_DIR.iterdir():
            if not sdir.is_dir() or sdir.name.startswith("."):
                continue
            for pdir in sdir.iterdir():
                if not pdir.is_dir() or pdir.name.startswith("."):
                    continue
                out = pdir / "output"
                if out.is_dir():
                    try:
                        shutil.rmtree(out)
                        removed += 1
                    except OSError as e:  # e.g. an STL locked by a viewer
                        errors.append(f"{sdir.name}/{pdir.name}: {e}")
    return {"removed": removed, "errors": errors}


@app.post("/api/run-part")
def run_part(session: str = Form(...), part: str = Form(...),
             feedback: str = Form(""), no_cache: bool = Form(False)):
    if not _has_api_key():
        raise HTTPException(
            400,
            "No ANTHROPIC_API_KEY set — extraction is unavailable. Set it in the "
            "project .env and restart the UI.",
        )
    pdir = _session_dir(session) / _sanitize(part)
    # A part is runnable when it has ANY drawing the pipeline can read — a raster
    # view OR a full-sheet overview (PDF/image). A folder-of-overviews part often
    # has only a PDF, which view_ingest reads directly; requiring a raster image
    # here wrongly blocked those with "no saved views".
    if not pdir.is_dir() or not (_part_images(pdir) or _find_overview_image(pdir)):
        raise HTTPException(400, "Selected part has no drawing to run (upload a "
                                 "full overview drawing or view images first).")
    out_dir = pdir / "output"
    # Correction feedback from the reviewer (Tab 3) becomes an authoritative
    # must-meet correction line, appended to the spec so the specs-first
    # extraction + Stage 2.5 resolution + final grading all apply it. Forcing a
    # fresh extraction guarantees the correction takes effect on the re-run.
    fb = (feedback or "").strip()
    if fb:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"CORRECTION ({stamp}): {fb}"
        for fname in ("must_meet_spec.txt", "notes.txt"):
            f = pdir / fname
            prev = f.read_text(encoding="utf-8", errors="replace").rstrip("\n") + "\n" if f.is_file() else ""
            f.write_text(prev + line + "\n", encoding="utf-8")
        no_cache = True
    # Scope the run to EXACTLY this one part's subfolder (not the parent).
    cmd = [
        sys.executable, "main.py",
        "--views-folder", str(pdir),
        "--output", str(out_dir),
        "--no-export",
    ]
    if no_cache:
        cmd.append("--no-extract-cache")
    # Deliver the untouched original upload (if any) alongside the outputs.
    extras = sorted((_session_dir(session) / ORIGINALS_DIRNAME).glob(f"{_sanitize(part)}.*")) \
        if (_session_dir(session) / ORIGINALS_DIRNAME).is_dir() else []
    # A vector original (PDF/DXF/DWG) also feeds the exact hole-position stage.
    vector_original = next((p for p in extras if p.suffix.lower() in (".pdf", ".dxf", ".dwg")), None)
    if vector_original is not None:
        cmd += ["--source-file", str(vector_original)]
    run_id = _start_run(cmd, out_dir, deliver_name=_sanitize(part), extra_files=extras)
    return {"id": run_id, "part": _sanitize(part)}
