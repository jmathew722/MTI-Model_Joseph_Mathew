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
import uuid
import shlex
import zipfile
import threading
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response, JSONResponse
from pydantic import BaseModel

# webapp/ lives inside the project dir; the CLI + samples live one level up.
WEBAPP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEBAPP_DIR.parent
RUNS_DIR = WEBAPP_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_DIR / ".env")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".tif", ".tiff", ".bmp"}

app = FastAPI(title="MTI 2D->3D Pipeline UI")

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


def _start_run(cmd: list[str], output_dir: Path, run_id: str | None = None) -> str:
    """Spawn the pipeline CLI and stream its output into RUNS[id]['lines']."""
    run_id = run_id or uuid.uuid4().hex[:12]
    output_dir.mkdir(parents=True, exist_ok=True)
    state = {"lines": [], "done": False, "exit": None, "output": output_dir}
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
                bufsize=1,
            )
        except Exception as e:  # spawn failure
            state["lines"].append(f"[launch error] {type(e).__name__}: {e}")
            state["exit"] = 127
            state["done"] = True
            return
        for line in proc.stdout:  # type: ignore[union-attr]
            state["lines"].append(line.rstrip("\n"))
        proc.wait()
        state["exit"] = proc.returncode
        state["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    return run_id


@app.get("/")
def index():
    return FileResponse(WEBAPP_DIR / "index.html")


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
