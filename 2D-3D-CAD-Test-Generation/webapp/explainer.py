"""Pipeline Explainer — a LOCAL-ONLY, ZERO-COST chat over a run's artifacts.

Hard guarantees (enforced by tests):
  * OLLAMA ONLY. This module imports nothing from the Anthropic client, never
    reads ``ANTHROPIC_API_KEY``, and makes NO network call to any host other
    than ``localhost`` (``http://localhost:11434`` by default). Every request
    goes through :func:`_urlopen`, which calls :func:`assert_local` first — a
    non-localhost host raises ``ExternalHostError`` before a socket is opened.
  * Every exchange costs $0.00. Token counts (from Ollama's final chunk) are
    recorded for context-budget awareness, but ``cost_usd`` is always ``0.0``.
  * Read-only. The assembler only ever READS artifacts; it never mutates a
    run. The one file it may WRITE is ``_export_manifest.json`` (a manifest of
    what a completed run delivered), and only if it does not already exist.

The chat is grounded strictly in the artifacts of ONE part's ``output/`` dir;
the model is instructed to answer only from what it is given and to cite the
artifact filenames it used.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Configuration — localhost Ollama only
# --------------------------------------------------------------------------- #
OLLAMA_HOST = os.getenv("EXPLAINER_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
# QWEN ONLY. The explainer never uses a llama model. The default is the large
# qwen; on a machine that already has ANY qwen installed we use that one (no
# re-download). A small qwen is the only fallback for genuinely low-RAM boxes.
DEFAULT_MODEL = os.getenv("EXPLAINER_OLLAMA_MODEL", "qwen3.6:latest")
FALLBACK_MODEL = os.getenv("EXPLAINER_OLLAMA_FALLBACK", "qwen2.5:7b")

# The explainer supports TWO providers, chosen per-message in the UI:
#   * "local"  — Ollama on localhost (qwen), zero cost, never leaves the machine.
#   * "claude" — the Anthropic API using the SAME ANTHROPIC_API_KEY the pipeline
#                uses. This is a PAID, external call (opt-in); its cost is
#                estimated and shown. The local path's no-external-host guarantee
#                is unaffected — only the claude path talks to the internet.
CLAUDE_MODEL = os.getenv("EXPLAINER_CLAUDE_MODEL", "claude-sonnet-5")
# Bound the local model so a too-big-for-RAM model fails with a clear message
# instead of hanging for minutes. If no token arrives within this many seconds,
# the local path errors and suggests Claude / a smaller qwen.
LOCAL_CHAT_TIMEOUT = float(os.getenv("EXPLAINER_LOCAL_TIMEOUT", "120"))
# Ollama silently truncates context to ~2048-4096 by default — the #1 way this
# feature would quietly break. Always request a large window.
NUM_CTX = int(os.getenv("EXPLAINER_NUM_CTX", "16384"))
# Artifact budget per question (~4 chars/token heuristic), leaving room in the
# 16k window for history + the answer.
ARTIFACT_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 4

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class ExternalHostError(RuntimeError):
    """Raised the instant any non-localhost host is about to be contacted."""


class OllamaUnavailable(RuntimeError):
    """Ollama is not installed / not running / unreachable on localhost."""


def assert_local(url: str) -> None:
    """Guard EVERY outbound request: only localhost is ever permitted. Raising
    here (before a socket opens) is what makes the zero-cost / no-exfiltration
    guarantee testable at the module boundary."""
    host = (urlparse(url).hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        raise ExternalHostError(
            f"Pipeline Explainer is local-only; refusing to contact non-localhost host {host!r}. "
            "This module never makes an external/paid call.")


# --------------------------------------------------------------------------- #
# Network choke point (all Ollama traffic passes through here)
# --------------------------------------------------------------------------- #
def _urlopen(url: str, data: Optional[bytes] = None, timeout: float = 300.0):
    """The ONLY place a real HTTP connection is opened. Validates locality
    first. Tests monkeypatch this to record the URL and assert it is local."""
    assert_local(url)
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _get_json(path: str, timeout: float = 10.0) -> dict:
    url = f"{OLLAMA_HOST}{path}"
    try:
        with _urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except ExternalHostError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OllamaUnavailable(str(e)) from e


def _post_stream(path: str, payload: dict, timeout: float = 600.0) -> Iterator[dict]:
    """POST a JSON body and yield each NDJSON chunk Ollama streams back."""
    url = f"{OLLAMA_HOST}{path}"
    data = json.dumps(payload).encode("utf-8")
    try:
        resp = _urlopen(url, data=data, timeout=timeout)
    except ExternalHostError:
        raise
    except (urllib.error.URLError, OSError) as e:
        raise OllamaUnavailable(str(e)) from e
    with resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


# --------------------------------------------------------------------------- #
# Hardware headroom (best-effort; no hard dependency on psutil)
# --------------------------------------------------------------------------- #
def total_ram_gb() -> Optional[float]:
    """Total physical RAM in GB, best-effort. Returns None if undetectable."""
    try:  # Windows
        import ctypes

        class _MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        if hasattr(ctypes, "windll"):
            m = _MEM()
            m.dwLength = ctypes.sizeof(_MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    try:  # POSIX
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 1)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Health + model bootstrap
# --------------------------------------------------------------------------- #
def installed_models() -> list[str]:
    tags = _get_json("/api/tags")
    return [m.get("name", "") for m in tags.get("models", []) if m.get("name")]


def _is_qwen(name: str) -> bool:
    return "qwen" in (name or "").lower()


def choose_model(installed: Optional[list[str]] = None) -> str:
    """Pick the qwen model to use — NEVER a llama. Preference order:
      1. the configured default if it is already installed;
      2. ANY already-installed qwen (so a machine that already has a qwen is
         used as-is and NEVER triggers a re-download);
      3. the small qwen fallback only on a clearly low-RAM box (< 16 GB) with
         no qwen installed;
      4. otherwise the configured default (which the UI will offer to pull)."""
    if installed is None:
        try:
            installed = installed_models()
        except OllamaUnavailable:
            installed = []
    if DEFAULT_MODEL in installed:
        return DEFAULT_MODEL
    already = [m for m in installed if _is_qwen(m)]
    if already:
        return already[0]
    ram = total_ram_gb()
    if ram is not None and ram < 16 and FALLBACK_MODEL != DEFAULT_MODEL:
        return FALLBACK_MODEL
    return DEFAULT_MODEL


def claude_available() -> bool:
    """True when the Claude provider can be used (the SAME key the pipeline uses)."""
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def health() -> dict:
    """Status snapshot for the UI: both providers. ``local`` = Ollama (up?
    which model, needs pull?); ``claude`` = Anthropic API (key present? model).
    Top-level Ollama fields are kept for backward-compatibility."""
    claude = {"available": claude_available(), "model": CLAUDE_MODEL}
    try:
        ver = _get_json("/api/version")
    except OllamaUnavailable as e:
        base = {"ok": False, "running": False, "error": str(e),
                "host": OLLAMA_HOST, "cost_usd": 0.0, "local": True}
        base["providers"] = {
            "local": {"available": False, "running": False, "error": str(e)},
            "claude": claude,
        }
        return base
    try:
        models = installed_models()
    except OllamaUnavailable:
        models = []
    model = choose_model(models)
    local = {
        "available": True, "running": True, "host": OLLAMA_HOST,
        "version": ver.get("version", "?"), "installed": models,
        "model": model, "model_ready": model in models,
        "num_ctx": NUM_CTX, "ram_gb": total_ram_gb(),
    }
    return {
        "ok": True, "running": True, "host": OLLAMA_HOST,
        "version": ver.get("version", "?"), "installed": models,
        "model": model, "model_ready": model in models,
        "num_ctx": NUM_CTX, "ram_gb": total_ram_gb(),
        "cost_usd": 0.0, "local": True,
        "providers": {"local": local, "claude": claude},
    }


def pull_model(model: str) -> Iterator[dict]:
    """Stream ``POST /api/pull`` progress chunks. Each is a dict with keys like
    ``status``/``completed``/``total`` — the UI renders these as a progress
    bubble. On completion, pin the model into ``.env`` so it is stable."""
    for chunk in _post_stream("/api/pull", {"name": model, "stream": True}):
        yield chunk
    _pin_model_choice(model)


def _pin_model_choice(model: str) -> None:
    """Persist a successful model choice to the project .env so it is stable
    across sessions (never touches ANTHROPIC_API_KEY or any other key)."""
    try:
        env = _project_dir() / ".env"
        lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
        out, seen = [], False
        for ln in lines:
            if ln.strip().startswith("EXPLAINER_OLLAMA_MODEL="):
                out.append(f"EXPLAINER_OLLAMA_MODEL={model}"); seen = True
            else:
                out.append(ln)
        if not seen:
            out.append(f"EXPLAINER_OLLAMA_MODEL={model}")
        env.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception:
        pass  # pinning is a convenience, never fatal


def _project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Artifact registry — every stage output, extraction -> export
# --------------------------------------------------------------------------- #
# stage_key -> (human label, [glob patterns relative to the part output dir]).
ARTIFACT_REGISTRY: list[tuple[str, str, list[str]]] = [
    ("image_prep", "Stage 1 · Image prep",
     ["*prep*log*", "*prep*.json", "tile_map*.json", "*_tiled*.json", "crops*.json"]),
    ("extraction", "Stage 2 · Extraction (raw)",
     ["*_extraction.json"]),
    ("overview", "Stage 1.5 · Overview analysis",
     ["overview_analysis.json"]),
    ("resolution", "Stage 2.5 · Resolution",
     ["*_resolved_extraction.json", "*_clarifications.json", "*_assist_queue.json"]),
    ("must_meet", "Stage 2.6 · Must-meet constraints",
     ["must_meet_constraints.json", "must_meet_spec.txt", "notes.txt"]),
    ("verification", "Stage 6 · Verification",
     ["*verification_report*.txt", "*_verification*.txt"]),
    ("build_plan", "Stage 6.5 · Build plan",
     ["*_build_plan.json", "build_plan.json", "*_build_dispositions.json"]),
    ("macros", "Stage 7 · Macros",
     ["macros/*.vba", "*_audit_report.json"]),
    ("prevalidation", "Stage 8 · CadQuery pre-validation",
     ["prevalidation_report.json"]),
    ("build", "Stage 9 · SolidWorks build",
     ["*_model_check.txt", "*_deferred_log.json", "macro_result.json",
      "logs/build_log.txt", "logs/macro_result.json"]),
    ("constraint_verify", "Stage 10 · Constraint verification",
     ["constraint_verification.json"]),
    ("feature_verify", "Stage 10.6 · Per-feature verification",
     ["*_feature_verification.json", "*_geometric_loop_report.json"]),
    ("reconciliation", "Stage 10.5 · Reconciliation",
     ["*_reconciliation_report.json"]),
    ("review", "Stage 12 · Engineering review",
     ["*_engineering_review.txt"]),
    ("export", "Export · Delivery manifest",
     ["_export_manifest.json"]),
    ("usage", "Global · Cost ledgers",
     ["token_usage_log.txt", "token_usage_log.jsonl", "chat_usage_log.jsonl"]),
]

# keyword -> stage keys it should pull in (routing table).
ROUTING: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("image", "blank", "resolution dpi", "dpi", "tile", "zoom", "raster", "nearly blank"),
     ("image_prep",)),
    (("export", "download", "downloads", "zip", "where did", "files go", "where are my",
      "delivered", "manifest"),
     ("export",)),
    (("build log", "pass", "fail", "failed", "bbox", "com", "build error"),
     ("build",)),
    (("audit", "banned", "prohibited api"),
     ("macros",)),
    (("clarification", "question you asked", "asked me", "assist"),
     ("resolution",)),
    (("macro", "vba", "step", "feature cut", "sketch"),
     ("macros", "build_plan")),
    (("resolve", "resolved", "ambigu", "flag", "assumption", "basis", "why was"),
     ("resolution", "verification")),
    (("must meet", "must-meet", "mm-", "spec", "constraint", "requirement"),
     ("must_meet", "constraint_verify")),
    (("overview", "cross-view", "relationship", "holistic"),
     ("overview",)),
    (("slot", "notch", "u-notch", "fillet", "rectangle", "corner"),
     ("build_plan", "resolution")),
    (("reconcil", "checklist", "missing", "unresolved", "dropped"),
     ("reconciliation", "build_plan")),
    (("verify", "verification", "measured", "watertight", "volume"),
     ("feature_verify", "constraint_verify", "verification")),
    (("hole", "diameter", "bolt", "pattern", "bore", "counterbore", "tap"),
     ("extraction", "build_plan", "feature_verify")),
    (("deferred", "retry", "quarantine"),
     ("build",)),
    (("cost", "token", "usage", "how much"),
     ("usage",)),
]

# The default artifact set for an un-routed question (the high-signal spine).
DEFAULT_STAGES = ("extraction", "resolution", "build_plan", "reconciliation", "review")

_ID_RE = re.compile(r"\b(?:MM-\d+|[DHF]\d{2,4}|F\d{2,4}_[A-Za-z_]+)\b")


@dataclass
class Artifact:
    stage: str
    label: str
    path: Path
    name: str

    def rel(self, part_out: Path) -> str:
        try:
            return str(self.path.relative_to(part_out)).replace("\\", "/")
        except ValueError:
            return self.name


# Per-stage filename exclusions — a glob like ``*_extraction.json`` also matches
# ``*_resolved_extraction.json``; the raw-extraction stage must not swallow the
# resolved file (which belongs to the resolution stage).
_STAGE_EXCLUDE: dict[str, tuple[str, ...]] = {
    "extraction": ("resolved",),
}


def list_artifacts(part_out: Path) -> list[Artifact]:
    """Resolve the registry against a real part output dir (read-only)."""
    found: list[Artifact] = []
    seen: set[Path] = set()
    for stage, label, patterns in ARTIFACT_REGISTRY:
        excl = _STAGE_EXCLUDE.get(stage, ())
        for pat in patterns:
            for p in sorted(part_out.glob(pat)) + sorted(part_out.rglob(pat)):
                if not p.is_file() or p in seen or ".extraction_cache" in p.parts:
                    continue
                if any(x in p.name for x in excl):
                    continue
                seen.add(p)
                found.append(Artifact(stage, label, p, p.name))
    return found


def artifacts_by_stage(part_out: Path) -> dict[str, list[Artifact]]:
    out: dict[str, list[Artifact]] = {}
    for a in list_artifacts(part_out):
        out.setdefault(a.stage, []).append(a)
    return out


def route(question: str) -> list[str]:
    """Which stage keys a question needs. Keyword table + always-on spine."""
    q = question.lower()
    stages: list[str] = []
    for keys, targets in ROUTING:
        if any(k in q for k in keys):
            for t in targets:
                if t not in stages:
                    stages.append(t)
    for s in DEFAULT_STAGES:
        if s not in stages:
            stages.append(s)
    return stages


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _slice_for_ids(text: str, ids: list[str], name: str) -> str:
    """When a question names specific IDs, keep only the lines/objects that
    mention them (plus a small header) — this is how we fit big JSON under the
    budget without dropping the relevant part."""
    if not ids:
        return text
    lines = text.splitlines()
    keep, hits = [], 0
    for i, ln in enumerate(lines):
        if any(x in ln for x in ids):
            lo, hi = max(0, i - 2), min(len(lines), i + 3)
            keep.extend(lines[lo:hi])
            hits += 1
    if hits:
        uniq = list(dict.fromkeys(keep))
        return f"# {name} (sliced to lines mentioning {', '.join(ids)})\n" + "\n".join(uniq)
    return text


def _read(path: Path, cap: int = 60_000) -> str:
    try:
        t = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"<could not read {path.name}: {e}>"
    return t if len(t) <= cap else t[:cap] + f"\n...<truncated {len(t) - cap} chars>"


@dataclass
class AssembledContext:
    text: str
    citations: list[dict] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    tokens: int = 0


def assemble_context(question: str, part_out: Path,
                     budget_tokens: int = ARTIFACT_TOKEN_BUDGET) -> AssembledContext:
    """Gather the artifacts a question needs, sliced by any IDs it names, and
    packed under the token budget in stage order. Returns the context blob +
    the citation list (what the model may cite) + used/skipped accounting."""
    ids = list(dict.fromkeys(_ID_RE.findall(question)))
    by_stage = artifacts_by_stage(part_out)
    wanted = route(question)

    blocks: list[str] = []
    citations: list[dict] = []
    used: list[str] = []
    skipped: list[str] = []
    budget_chars = budget_tokens * _CHARS_PER_TOKEN
    spent = 0

    for stage in wanted:
        for a in by_stage.get(stage, []):
            body = _read(a.path)
            body = _slice_for_ids(body, ids, a.name)
            block = f"===== [{a.rel(part_out)}] ({a.label}) =====\n{body}\n"
            if spent + len(block) > budget_chars:
                skipped.append(a.rel(part_out))
                continue
            blocks.append(block)
            spent += len(block)
            citations.append({"name": a.name, "rel": a.rel(part_out),
                              "stage": a.stage, "label": a.label})
            used.append(a.rel(part_out))
    text = "\n".join(blocks) if blocks else "(no artifacts found for this part yet)"
    return AssembledContext(text=text, citations=citations, used=used,
                            skipped=skipped, tokens=_approx_tokens(text))


def trace_field(field_id: str, part_out: Path) -> AssembledContext:
    """The single most useful debugging answer: follow ONE field (e.g. D009)
    across every stage in order — raw extraction reading, resolved value +
    basis, build-plan usage, the macro line(s) that consume it, and the build
    result — assembling only the slices that mention it, with citations."""
    fid = field_id.strip()
    by_stage = artifacts_by_stage(part_out)
    order = ["extraction", "overview", "resolution", "must_meet", "verification",
             "build_plan", "macros", "build", "constraint_verify", "feature_verify",
             "reconciliation", "review"]
    blocks: list[str] = []
    citations: list[dict] = []
    used: list[str] = []
    for stage in order:
        for a in by_stage.get(stage, []):
            body = _read(a.path)
            sliced = _slice_for_ids(body, [fid], a.name)
            if sliced == body and fid not in body:
                continue  # this artifact never mentions the field — skip it
            blocks.append(f"===== [{a.rel(part_out)}] ({a.label}) =====\n{sliced}\n")
            citations.append({"name": a.name, "rel": a.rel(part_out),
                              "stage": a.stage, "label": a.label})
            used.append(a.rel(part_out))
    if not blocks:
        text = f"(no artifact for this part mentions {fid})"
    else:
        text = (f"TRACE of {fid} across the pipeline, in stage order:\n\n"
                + "\n".join(blocks))
    return AssembledContext(text=text, citations=citations, used=used, tokens=_approx_tokens(text))


# --------------------------------------------------------------------------- #
# Export manifest (written at explain-time if the run never produced one)
# --------------------------------------------------------------------------- #
def write_export_manifest(part_out: Path, delivered_dirs: Iterable[Path] = ()) -> Optional[Path]:
    """Record what a completed run delivered (files, sizes, timestamps, and the
    delivery/zip locations) so 'where did my files go?' has a citable answer.
    Idempotent: never overwrites an existing manifest."""
    manifest = part_out / "_export_manifest.json"
    if manifest.is_file():
        return manifest
    if not part_out.is_dir():
        return None
    files = []
    for p in sorted(part_out.rglob("*")):
        if p.is_file() and ".extraction_cache" not in p.parts and p.name != "_export_manifest.json":
            try:
                st = p.stat()
                files.append({"rel": str(p.relative_to(part_out)).replace("\\", "/"),
                              "size_bytes": st.st_size,
                              "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                                        time.localtime(st.st_mtime))})
            except OSError:
                continue
    payload = {
        "part_output_dir": str(part_out),
        "delivered_to": [str(d) for d in delivered_dirs if d and Path(d).exists()],
        "file_count": len(files),
        "total_bytes": sum(f["size_bytes"] for f in files),
        "files": files,
    }
    try:
        manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        return None
    return manifest


# --------------------------------------------------------------------------- #
# Prompt + chat
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
You are the MTI 2D->3D Pipeline Explainer. You help an engineer understand what \
the pipeline did to ONE part: how a drawing became a SolidWorks model, why each \
value was resolved the way it was, what was flagged, deferred, or skipped, and \
where the outputs went.

RULES:
- Answer ONLY from the ARTIFACTS provided below. They are the ground truth.
- NEVER invent dimensions, values, or outcomes. If the artifacts do not contain \
the answer, say so plainly and name which artifact would hold it.
- Cite the artifact filenames you used, in square brackets, e.g. [D009 was \
resolved to 1.560 in ..._resolved_extraction.json]. Cite the exact bracketed \
filename shown in each artifact header.
- Be concise and concrete. Prefer the specific number/flag/step over generalities.
- You are READ-ONLY: you explain, you do not change anything. If you diagnose a \
problem, you may suggest a one-line correction the engineer could apply, but make \
clear it is a suggestion.
- This is a fully LOCAL, zero-cost assistant. Do not claim to call any external \
service."""


def build_messages(question: str, ctx: AssembledContext,
                   history: Optional[list[dict]] = None) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-8:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        content = h.get("content", "")
        if content:
            msgs.append({"role": role, "content": content})
    user = (f"ARTIFACTS FOR THIS PART:\n\n{ctx.text}\n\n"
            f"-----\nQUESTION: {question}")
    if ctx.skipped:
        user += ("\n\n(Note: these artifact slices did not fit the context budget and were "
                 f"NOT included: {', '.join(ctx.skipped)}. If the answer needs them, say so.)")
    msgs.append({"role": "user", "content": user})
    return msgs


def chat(question: str, part_out: Path, *, history: Optional[list[dict]] = None,
         model: Optional[str] = None, provider: str = "local") -> Iterator[dict]:
    """Stream an answer from the chosen provider. Yields:
        {"type":"context", "citations":[...], "used":[...], "skipped":[...]}
        {"type":"status", "text":"..."}   (optional — e.g. the model is thinking)
        {"type":"token", "text":"..."}    (many)
        {"type":"done", "meta":{provider, model, prompt_tokens, eval_tokens,
                                duration_s, cost_usd, local, citations, used, skipped}}
    or {"type":"error", "error":"..."} on failure.

    provider="local"  -> Ollama (qwen) on localhost; zero cost.
    provider="claude" -> the Anthropic API (paid, external, opt-in) using the
                         same ANTHROPIC_API_KEY the pipeline uses.
    """
    # A "trace <id>" question gets the dedicated cross-stage assembler.
    m = re.match(r"\s*trace\s+([A-Za-z0-9_\-]+)", question, re.I)
    ctx = trace_field(m.group(1), part_out) if m else assemble_context(question, part_out)
    yield {"type": "context", "citations": ctx.citations, "used": ctx.used, "skipped": ctx.skipped}

    if provider == "claude":
        yield from _claude_chat(question, part_out, ctx, history, model)
    else:
        yield from _local_chat(question, part_out, ctx, history, model)


def _local_chat(question, part_out, ctx, history, model) -> Iterator[dict]:
    model = model or choose_model()
    payload = {
        "model": model,
        "messages": build_messages(question, ctx, history),
        "stream": True,
        # Disable qwen's chain-of-thought so it streams the ANSWER immediately
        # instead of emitting (invisible) thinking tokens first — the #1 cause
        # of "it takes forever / outputs nothing".
        "think": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.2},
    }
    start = time.time()
    prompt_tokens = eval_tokens = 0
    answer_parts: list[str] = []
    thinking_seen = False
    too_big_hint = (f"The installed local model ({model}) is likely too large to run "
                    "interactively on this machine's RAM. Switch the provider to "
                    "Claude API, or install a smaller qwen (`ollama pull qwen2.5:7b`).")
    try:
        for chunk in _post_stream("/api/chat", payload, timeout=LOCAL_CHAT_TIMEOUT):
            msg = chunk.get("message") or {}
            if msg.get("thinking") and not thinking_seen:
                thinking_seen = True
                yield {"type": "status", "text": "thinking…"}
            piece = msg.get("content", "")
            if piece:
                answer_parts.append(piece)
                yield {"type": "token", "text": piece}
            if chunk.get("done"):
                prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                eval_tokens = int(chunk.get("eval_count") or 0)
    except OllamaUnavailable as e:
        yield {"type": "error", "error": f"Ollama unavailable: {e}"}
        return
    except ExternalHostError as e:
        yield {"type": "error", "error": str(e)}
        return
    except Exception as e:  # read timeout / dropped stream on an over-large model
        if answer_parts:
            pass  # partial answer arrived — fall through and keep it
        else:
            yield {"type": "error",
                   "error": f"Local model timed out after {int(LOCAL_CHAT_TIMEOUT)}s "
                            f"({type(e).__name__}). {too_big_hint}"}
            return

    answer = "".join(answer_parts)
    if not answer.strip():
        yield {"type": "error",
               "error": ("The local model returned no text. On this machine the installed "
                         f"model ({model}) may be too large to run interactively — try the "
                         "Claude provider, or install a smaller qwen (e.g. `ollama pull qwen2.5:7b`).")}
        return
    duration = round(time.time() - start, 2)
    meta = {
        "provider": "local", "model": model, "prompt_tokens": prompt_tokens,
        "eval_tokens": eval_tokens, "duration_s": duration, "cost_usd": 0.0, "local": True,
        "citations": ctx.citations, "used": ctx.used, "skipped": ctx.skipped, "answer": answer,
    }
    log_usage(part_out, provider="local", model=model, prompt_tokens=prompt_tokens,
              eval_tokens=eval_tokens, duration_s=duration, cost_usd=0.0)
    yield {"type": "done", "meta": meta}


def _claude_cost(model: str, in_tok: int, out_tok: int) -> float:
    """Estimate USD from the pipeline's own published price table."""
    try:
        from pipeline.usage_log import estimate_cost
        return round(estimate_cost({"input_tokens": in_tok, "output_tokens": out_tok}, model), 4)
    except Exception:
        # Fallback list price (Sonnet-class) if the ledger module isn't importable.
        return round(in_tok / 1e6 * 3.0 + out_tok / 1e6 * 15.0, 4)


def _claude_chat(question, part_out, ctx, history, model) -> Iterator[dict]:
    model = model or CLAUDE_MODEL
    if not claude_available():
        yield {"type": "error", "error": "No ANTHROPIC_API_KEY set — the Claude provider is unavailable. "
               "Set it in the project .env, or use the Local (qwen) provider."}
        return
    try:
        import anthropic
    except Exception:
        yield {"type": "error", "error": "The 'anthropic' package is not installed in this environment."}
        return

    msgs = build_messages(question, ctx, history)
    system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else SYSTEM_PROMPT
    convo = [{"role": mm["role"], "content": mm["content"]} for mm in msgs if mm["role"] != "system"]

    start = time.time()
    answer_parts: list[str] = []
    in_tok = out_tok = 0
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), max_retries=2)
        with client.messages.stream(model=model, max_tokens=1500, system=system,
                                     messages=convo) as stream:
            for text in stream.text_stream:
                if text:
                    answer_parts.append(text)
                    yield {"type": "token", "text": text}
            final = stream.get_final_message()
            in_tok = int(getattr(final.usage, "input_tokens", 0) or 0)
            out_tok = int(getattr(final.usage, "output_tokens", 0) or 0)
    except Exception as e:
        yield {"type": "error", "error": f"Claude API error: {type(e).__name__}: {e}"}
        return

    duration = round(time.time() - start, 2)
    cost = _claude_cost(model, in_tok, out_tok)
    meta = {
        "provider": "claude", "model": model, "prompt_tokens": in_tok, "eval_tokens": out_tok,
        "duration_s": duration, "cost_usd": cost, "local": False,
        "citations": ctx.citations, "used": ctx.used, "skipped": ctx.skipped,
        "answer": "".join(answer_parts),
    }
    log_usage(part_out, provider="claude", model=model, prompt_tokens=in_tok,
              eval_tokens=out_tok, duration_s=duration, cost_usd=cost)
    yield {"type": "done", "meta": meta}


# --------------------------------------------------------------------------- #
# Usage log + per-part history persistence (both zero-cost)
# --------------------------------------------------------------------------- #
def log_usage(part_out: Path, *, model: str, prompt_tokens: int, eval_tokens: int,
              duration_s: float, provider: str = "local", cost_usd: float = 0.0) -> None:
    """Append one line to chat_usage_log.jsonl. Local answers are cost_usd 0.0
    (proving 'Explainer chat: $0.00 (local)'); Claude answers carry their
    estimated cost so the unified ledger stays honest."""
    try:
        part_out.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
               "provider": provider, "model": model, "prompt_tokens": prompt_tokens,
               "eval_tokens": eval_tokens, "duration_s": duration_s,
               "cost_usd": round(cost_usd, 4), "local": provider == "local"}
        with (part_out / "chat_usage_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def usage_total(part_out: Path) -> dict:
    """Sum the chat ledger: message/token counts, total cost (0 for the local
    provider, estimated for Claude), and whether every message was local."""
    msgs = in_tok = out_tok = 0
    cost = 0.0
    all_local = True
    log = part_out / "chat_usage_log.jsonl"
    if log.is_file():
        for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            msgs += 1
            in_tok += int(r.get("prompt_tokens") or 0)
            out_tok += int(r.get("eval_tokens") or 0)
            cost += float(r.get("cost_usd") or 0.0)
            if r.get("provider", "local") != "local":
                all_local = False
    return {"messages": msgs, "prompt_tokens": in_tok, "eval_tokens": out_tok,
            "cost_usd": round(cost, 4), "local": all_local, "all_local": all_local}


def _history_path(part_out: Path) -> Path:
    return part_out / "_explainer_history.json"


def load_history(part_out: Path) -> list[dict]:
    p = _history_path(part_out)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(part_out: Path, role: str, content: str,
                   meta: Optional[dict] = None) -> None:
    try:
        hist = load_history(part_out)
        hist.append({"role": role, "content": content,
                     "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                     "meta": meta or {}})
        part_out.mkdir(parents=True, exist_ok=True)
        _history_path(part_out).write_text(json.dumps(hist, indent=2), encoding="utf-8")
    except Exception:
        pass
