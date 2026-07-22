"""Human-verification questions (2026-07-21, MTI_Codex).

Turns a run's ENGINEERING FLAGS into concise, drawing-specific confirmation
questions a human can answer in one line — the input to the UI's "Human
Verification" tab. The intent: instead of shipping a part with a stack of
CRITICAL/HIGH assumptions, ask the operator a short question per flag ("The
.531 R end radius couldn't be tied to the slot — is the slot 1.60 wide?"),
collect a fill-in answer, and feed those answers back as must-meet CORRECTION
lines so the specs-first extraction + Stage 2.5 resolution clear the flag on
the next run. Fewer flags, higher accuracy, less rework.

The questions are phrased by the SAME model the pipeline uses (GPT-5.6 on this
branch, via :mod:`pipeline.ai_provider`) in ONE batched call, cached to
``<prefix>_verification_questions.json`` keyed by the flag set so a tab reload
never re-calls the model. When no provider/key is available it degrades to a
deterministic template built from the flag text — the tab still works, just
with plainer wording.

Public: :func:`build_verification_items`, :func:`compile_corrections`.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

CACHE_SUFFIX = "_verification_questions.json"
_MAX_FLAGS = 40  # never send an unbounded prompt; the review is severity-ranked


# --------------------------------------------------------------------------- #
# Reading the run's flags
# --------------------------------------------------------------------------- #
def _prefix_and_dir(part_dir: Path) -> tuple[Path, str]:
    """(part_dir, artifact-prefix). The prefix is discovered from the build plan
    / engineering-review filename on disk (folder name != prefix, e.g. folder
    A001581E holds 158-C_* files)."""
    part_dir = Path(part_dir)
    for pat in ("*_build_plan.json", "*_engineering_review.txt", "*_extraction.json"):
        hits = sorted(part_dir.glob(pat))
        if hits:
            name = hits[0].name
            for suf in ("_build_plan.json", "_engineering_review.txt", "_extraction.json"):
                if name.endswith(suf):
                    return part_dir, name[: -len(suf)]
    return part_dir, part_dir.name


def _load_flags(part_dir: Path) -> list[dict[str, Any]]:
    """Every engineering-review item for the run, most-severe first (as the plan
    already orders them). Read from ``build_plan.json``'s ``engineering_review``
    (the canonical list); falls back to an empty list."""
    part_dir, prefix = _prefix_and_dir(part_dir)
    bp = part_dir / f"{prefix}_build_plan.json"
    if not bp.is_file():
        return []
    try:
        plan = json.loads(bp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = plan.get("engineering_review") or []
    return [it for it in items if isinstance(it, dict)][:_MAX_FLAGS]


def _flag_id(flag: dict, idx: int) -> str:
    base = str(flag.get("id") or flag.get("source") or "flag")
    return f"{base}_{idx}"


def _flags_hash(flags: list[dict]) -> str:
    h = hashlib.sha256()
    for f in flags:
        h.update((str(f.get("id")) + "|" + str(f.get("severity")) + "|"
                  + str(f.get("what"))[:200] + "\0").encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# LLM question phrasing (batched, cached) — with a deterministic fallback
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are a manufacturing engineer reviewing an automated read of a 2D "
    "engineering drawing. For each flagged assumption or ambiguity, write ONE "
    "short question (max 22 words) that asks the operator to confirm or correct "
    "the specific value/feature, so their one-line answer can resolve it. Be "
    "concrete and reference the feature id and value when present. Do NOT invent "
    "new facts. Return ONLY a JSON array of strings, one per flag, in order."
)


def _fallback_question(flag: dict) -> str:
    what = re.sub(r"\s+", " ", str(flag.get("what") or "").strip())
    fid = flag.get("id") or ""
    if not what:
        return f"Please confirm or correct feature {fid}." if fid else "Please confirm this item."
    # Trim to a single clause; phrase as a confirmation.
    clause = what.split(". ")[0][:180]
    lead = f"{fid}: " if fid and not clause.startswith(str(fid)) else ""
    return f"Confirm or correct — {lead}{clause}?"


def _llm_questions(flags: list[dict]) -> Optional[list[str]]:
    """One batched model call → a question per flag, or None if unavailable."""
    try:
        from pipeline.ai_provider import build_client, default_model
    except Exception:
        return None
    import os

    if not (os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")):
        return None
    payload = [{"id": f.get("id"), "severity": f.get("severity"),
                "what": str(f.get("what") or "")[:300],
                "why": str(f.get("why") or "")[:200]} for f in flags]
    user = ("Flags (JSON):\n" + json.dumps(payload, indent=0)
            + "\n\nReturn ONLY a JSON array of question strings, one per flag, in order.")
    try:
        client = build_client(2)
        resp = client.messages.create(
            model=default_model(), max_tokens=2000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        # Extract the JSON array even if the model wrapped it in prose/fences.
        m = re.search(r"\[.*\]", text, re.DOTALL)
        arr = json.loads(m.group(0) if m else text)
        if isinstance(arr, list) and arr:
            return [str(q) for q in arr]
    except Exception as e:  # any failure → deterministic fallback, never blocks
        log.info("verification questions: LLM phrasing unavailable (%s) — using fallback.", e)
    return None


def build_verification_items(part_dir: Path, *, use_llm: bool = True) -> dict[str, Any]:
    """The Human-Verification view-model for one run: the flags, each with a
    concise confirmation question and a fill-in hint. Cached per flag-set.

    Returns ``{"part": prefix, "count": n, "counts": {sev: n}, "items": [...]}``
    where each item is ``{id, severity, source, what, why, affects, question}``.
    """
    part_dir, prefix = _prefix_and_dir(part_dir)
    flags = _load_flags(part_dir)
    result: dict[str, Any] = {"part": prefix, "count": len(flags),
                              "counts": {}, "items": []}
    for f in flags:
        sev = str(f.get("severity") or "MEDIUM")
        result["counts"][sev] = result["counts"].get(sev, 0) + 1
    if not flags:
        return result

    cache_path = part_dir / f"{prefix}{CACHE_SUFFIX}"
    fset = _flags_hash(flags)
    questions: Optional[list[str]] = None
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("flags_hash") == fset and len(cached.get("questions", [])) == len(flags):
                questions = cached["questions"]
        except (OSError, json.JSONDecodeError):
            questions = None
    if questions is None:
        questions = (_llm_questions(flags) if use_llm else None)
        if not questions or len(questions) != len(flags):
            questions = [_fallback_question(f) for f in flags]
        try:
            cache_path.write_text(json.dumps(
                {"flags_hash": fset, "questions": questions}, indent=2), encoding="utf-8")
        except OSError:
            pass

    for idx, (f, q) in enumerate(zip(flags, questions)):
        result["items"].append({
            "id": _flag_id(f, idx),
            "flag_id": f.get("id") or "",
            "severity": f.get("severity") or "MEDIUM",
            "source": f.get("source") or "",
            "what": f.get("what") or "",
            "why": f.get("why") or "",
            "affects": f.get("affects") or "",
            "question": q or _fallback_question(f),
        })
    return result


# --------------------------------------------------------------------------- #
# Compiling answers into a correction block (fed to the existing re-run path)
# --------------------------------------------------------------------------- #
def compile_corrections(items: list[dict], answers: dict[str, str]) -> str:
    """A single must-meet CORRECTION block from the human's per-flag answers.

    ``items`` are the verification items (for the question text + feature id);
    ``answers`` maps item id → the operator's fill-in text. Empty answers are
    skipped. The block is phrased so specs-first extraction + Stage 2.5 apply
    each answer against the specific feature it resolves."""
    by_id = {it["id"]: it for it in items}
    lines: list[str] = []
    for iid, ans in answers.items():
        ans = (ans or "").strip()
        if not ans:
            continue
        it = by_id.get(iid, {})
        fid = it.get("flag_id") or ""
        tag = f"[{fid}] " if fid else ""
        lines.append(f"- {tag}{ans}")
    if not lines:
        return ""
    return ("Operator human-verification answers (resolve these flagged items; "
            "these are authoritative over the automated read):\n" + "\n".join(lines))
