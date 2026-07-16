"""OpenAI Codex CLI client — the single integration + settings surface.

Codex takes over two pipeline roles (see docs/codex-integration.md):
  A) independent validation of Claude's extraction (pipeline/codex_validation.py)
  B) writing ALL VBA macros from the validated build JSON (pipeline/codex_macros.py)

INTEGRATION METHOD — **no API key required by default**. We drive the Codex CLI
(`codex exec`) as a subprocess, authenticated via ChatGPT sign-in:

    npm i -g @openai/codex     # one-time install
    codex login                # choose "Sign in with ChatGPT"

An OpenAI API key is only a *fallback* for headless environments where the
ChatGPT sign-in is unavailable (set OPENAI_API_KEY + CODEX_AUTH=api).

Every knob lives here (the single settings location):
    CODEX_ENABLED   truthy to turn the integration on (default: auto — on iff the
                    codex CLI is found on PATH).  Force with 1/0/true/false.
    CODEX_MODEL     model pinned for `codex exec -m` (default gpt-5.6-sol).
    CODEX_TIMEOUT_S per-call subprocess timeout in seconds (default 240).
    CODEX_RETRIES   retries on failure / malformed JSON (default 2).
    CODEX_AUTH      "chatgpt" (default) or "api" (OPENAI_API_KEY fallback).
    OPENAI_API_KEY  only used when CODEX_AUTH=api.
    MTI_CODEX_STUB  1 to force the deterministic offline STUB (dry-run/CI on Mac).

When the CLI is absent/unauthenticated and no API fallback is configured, calls
run in **stub mode**: each caller supplies a deterministic ``stub_fn`` so the
whole pipeline (including the REJECTED and CadQuery-failure halt paths) is
testable end-to-end with no network — this is what --dry-run exercises.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# ── Settings (single location) ───────────────────────────────────────────────
def _env_flag(name: str, default: Optional[bool] = None) -> Optional[bool]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.6-sol").strip()
CODEX_TIMEOUT_S = int(os.getenv("CODEX_TIMEOUT_S", "240") or "240")
CODEX_RETRIES = int(os.getenv("CODEX_RETRIES", "2") or "2")
CODEX_AUTH = (os.getenv("CODEX_AUTH", "chatgpt") or "chatgpt").strip().lower()
_FORCE_STUB = _env_flag("MTI_CODEX_STUB", False)


class CodexUnavailable(RuntimeError):
    """Codex could not run (not installed / not authenticated / no fallback)."""


class CodexError(RuntimeError):
    """Codex ran but failed or returned unparseable output after all retries."""


def codex_path() -> Optional[str]:
    """Absolute path to the codex CLI, or None if not on PATH."""
    return shutil.which("codex")


def is_installed() -> bool:
    return codex_path() is not None


def _run(args: list[str], timeout: int, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=str(cwd) if cwd else None)


def version() -> Optional[str]:
    if not is_installed():
        return None
    try:
        r = _run([codex_path(), "--version"], timeout=20)
        return (r.stdout or r.stderr or "").strip() or None
    except Exception:
        return None


def is_authenticated() -> bool:
    """Best-effort ChatGPT/API auth check. Never raises."""
    if CODEX_AUTH == "api":
        return bool(os.getenv("OPENAI_API_KEY"))
    if not is_installed():
        return False
    for probe in (["login", "status"], ["auth", "status"], ["whoami"]):
        try:
            r = _run([codex_path(), *probe], timeout=20)
            out = (r.stdout + r.stderr).lower()
            if r.returncode == 0 and ("logged in" in out or "signed in" in out
                                      or "authenticated" in out or "chatgpt" in out):
                return True
            if r.returncode == 0 and "not" not in out and out.strip():
                return True
        except Exception:
            continue
    return False


def enabled() -> bool:
    """Master on/off. Default: on iff the CLI is installed (or API fallback set)."""
    flag = _env_flag("CODEX_ENABLED", None)
    if flag is not None:
        return flag
    return is_installed() or (CODEX_AUTH == "api" and bool(os.getenv("OPENAI_API_KEY")))


def active() -> bool:
    """Whether the Codex pipeline STAGES should run at all. On when the
    integration is enabled, or when the offline stub is forced (dry-run/tests).
    Off by default when Codex is absent, so the base pipeline is unchanged."""
    return enabled() or _FORCE_STUB


def mode() -> str:
    """Resolved execution mode: 'cli' | 'api' | 'stub'."""
    if _FORCE_STUB or not enabled():
        return "stub"
    if CODEX_AUTH == "api" and os.getenv("OPENAI_API_KEY"):
        return "api"
    if is_installed() and is_authenticated():
        return "cli"
    return "stub"


@dataclass
class CodexHealth:
    enabled: bool
    installed: bool
    authenticated: bool
    mode: str
    model: str
    version: Optional[str] = None
    message: str = ""
    instructions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "installed": self.installed,
                "authenticated": self.authenticated, "mode": self.mode,
                "model": self.model, "version": self.version,
                "message": self.message, "instructions": self.instructions}


def health() -> CodexHealth:
    """Startup/UI health probe. Never raises — a down Codex is a reported state."""
    inst = is_installed()
    auth = is_authenticated()
    m = mode()
    msg, steps = "", []
    if m == "cli":
        msg = f"Codex CLI ready ({version() or 'codex'}), model {CODEX_MODEL}, ChatGPT sign-in."
    elif m == "api":
        msg = f"Codex via OpenAI API fallback, model {CODEX_MODEL}."
    elif not inst:
        msg = "Codex CLI not installed — validation + macro writing run in the offline STUB (dry-run safe)."
        steps = ["npm i -g @openai/codex",
                 "codex login   (choose 'Sign in with ChatGPT')",
                 "set CODEX_ENABLED=1 and restart the app"]
    elif not enabled():
        msg = "Codex CLI installed but the integration is off (CODEX_ENABLED=0)."
        steps = ["set CODEX_ENABLED=1 and restart the app"]
    elif not auth:
        msg = "Codex CLI installed but not authenticated — offline STUB mode."
        steps = ["codex login   (choose 'Sign in with ChatGPT')",
                 "or set CODEX_AUTH=api and OPENAI_API_KEY for headless use"]
    else:
        msg = "Codex forced to STUB mode (MTI_CODEX_STUB=1)."
    return CodexHealth(enabled=enabled(), installed=inst, authenticated=auth,
                       mode=m, model=CODEX_MODEL, version=version(),
                       message=msg, instructions=steps)


# ── JSON extraction (robust to markdown fences / chatter) ─────────────────────
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull the final JSON object/array out of arbitrary CLI stdout."""
    if not text:
        raise CodexError("empty output")
    # 1) fenced ```json blocks — take the last one
    blocks = _FENCE.findall(text)
    candidates = list(blocks) + [text]
    for cand in reversed(candidates):
        cand = cand.strip()
        # trim to the outermost {..} or [..]
        for open_c, close_c in (("{", "}"), ("[", "]")):
            i, j = cand.find(open_c), cand.rfind(close_c)
            if i != -1 and j != -1 and j > i:
                try:
                    return json.loads(cand[i:j + 1])
                except Exception:
                    continue
    raise CodexError("no parseable JSON in output")


def try_install() -> tuple[bool, str]:
    """Attempt a one-time global install of the Codex CLI via npm. On-demand only
    (never at import). Returns (ok, message)."""
    npm = shutil.which("npm")
    if not npm:
        return False, "npm not found — install Node.js first."
    try:
        r = _run([npm, "i", "-g", "@openai/codex"], timeout=600)
        if r.returncode == 0 and is_installed():
            return True, "Codex CLI installed. Next: run `codex login` (Sign in with ChatGPT)."
        return False, (r.stderr or r.stdout or "npm install failed").strip()[:500]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── The one call surface used by validation + macro modules ───────────────────
def run_json(prompt: str, *,
             images: Optional[list[Path]] = None,
             stub_fn: Optional[Callable[[], Any]] = None,
             workdir: Optional[Path] = None,
             timeout: Optional[int] = None,
             retries: Optional[int] = None,
             model: Optional[str] = None) -> tuple[Any, str]:
    """Run a JSON-only Codex task. Returns ``(parsed_json, mode)``.

    * ``images`` are passed with ``codex exec -i`` (vision inputs).
    * Runs in a sandboxed temp working dir per job (isolated file writes).
    * Retries up to ``retries`` on failure or malformed JSON.
    * In stub mode calls ``stub_fn`` (required for offline/dry-run).
    """
    m = mode()
    if m == "stub":
        if stub_fn is None:
            raise CodexUnavailable("Codex unavailable and no stub provided.")
        return stub_fn(), "stub"

    model = model or CODEX_MODEL
    timeout = timeout or CODEX_TIMEOUT_S
    retries = CODEX_RETRIES if retries is None else retries
    images = images or []

    tmp_created = None
    if workdir is None:
        tmp_created = Path(tempfile.mkdtemp(prefix="mti_codex_"))
        workdir = tmp_created
    try:
        last_err = ""
        for attempt in range(retries + 1):
            prompt_file = workdir / "prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            args = [codex_path(), "exec", "-m", model,
                    "--cd", str(workdir), "--skip-git-repo-check"]
            for img in images:
                args += ["-i", str(img)]
            # The prompt is delivered on stdin so long specs never hit arg limits.
            args += ["-"]
            try:
                if m == "api":
                    env_note = ""  # codex reads OPENAI_API_KEY from the env itself
                r = subprocess.run(args, input=prompt, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout,
                                   cwd=str(workdir))
                if r.returncode != 0:
                    last_err = (r.stderr or r.stdout or "nonzero exit").strip()[:800]
                    continue
                # Codex may write the answer to stdout and/or a file; prefer a
                # result.json it was told to emit, else parse stdout.
                rj = workdir / "result.json"
                raw = rj.read_text(encoding="utf-8") if rj.is_file() else (r.stdout or "")
                return extract_json(raw), m
            except subprocess.TimeoutExpired:
                last_err = f"timeout after {timeout}s"
                continue
            except CodexError as e:
                last_err = str(e)
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
        raise CodexError(f"Codex failed after {retries + 1} attempt(s): {last_err}")
    finally:
        if tmp_created is not None:
            shutil.rmtree(tmp_created, ignore_errors=True)
