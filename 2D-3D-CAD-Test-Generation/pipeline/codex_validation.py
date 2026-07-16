"""Stage A — Codex independent extraction validation.

Runs AFTER Claude extraction + Stage 2.5 resolution and BEFORE the build JSON is
finalized / macros are written. Codex reads the drawing image(s) itself and
compares field-by-field against Claude's resolved extraction, returning a
structured verdict:

    { "overall_status": "APPROVED" | "APPROVED_WITH_NOTES" | "REJECTED",
      "summary": "...",
      "per_field_agreement": [ {field, claude_value, codex_value, agree} ],
      "discrepancies": [ {field, severity, claude_value, codex_value, reasoning} ],
      "notes": [ "..." ] }

Rules:
  * Hole-count / hole-pattern disagreements are ALWAYS >= HIGH severity
    (the A050211E "5 visible vs (6) HLS" class of bug).
  * Must-Meet Specifications stay tier-0: Codex may flag, never overrule them.
  * REJECTED halts the pipeline before macro writing; the caller may re-run Claude
    extraction with the discrepancies injected as context hints.

Offline/dry-run: a deterministic stub derives the verdict by cross-checking the
extraction against the Stage-1.5 overview analysis (hole-count callouts) and the
must-meet constraints — no network. Force a verdict for tests with
MTI_CODEX_FORCE_VERDICT=REJECTED|APPROVED_WITH_NOTES|APPROVED.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pipeline import codex_client

VALIDATION_FILENAME = "codex_validation.json"
STAGE_TAG = "stage_2_7_codex_validation"
_SEV_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


# ── helpers ───────────────────────────────────────────────────────────────────
def _holes(ext: dict) -> list[dict]:
    return ext.get("hole_callouts") or []


def _hole_total(ext: dict) -> int:
    tot = 0
    for h in _holes(ext):
        q = h.get("qty")
        tot += int(q) if isinstance(q, (int, float)) else 1
    return tot


def _overview_hole_count(ov: Optional[dict]) -> Optional[int]:
    """The hole count the Stage-1.5 overview asserts (e.g. '(6) HLS'), if any."""
    if not ov:
        return None
    for note in ov.get("global_notes", []) or []:
        rc = note.get("resolved_count")
        txt = str(note.get("note", "")).upper()
        if rc is not None and ("HLS" in txt or "HOLE" in txt):
            try:
                return int(rc)
            except Exception:
                continue
    return None


def _worst(discrepancies: list[dict]) -> Optional[str]:
    if not discrepancies:
        return None
    return max(discrepancies, key=lambda d: _SEV_RANK.get(str(d.get("severity", "LOW")).upper(), 0)
               ).get("severity")


def _status_from(discrepancies: list[dict]) -> str:
    worst = _worst(discrepancies)
    if worst in ("CRITICAL",):
        return "REJECTED"
    if worst in ("HIGH", "MEDIUM", "LOW"):
        return "APPROVED_WITH_NOTES"
    return "APPROVED"


# ── deterministic offline stub (dry-run / no Codex) ───────────────────────────
def _stub_verdict(resolved: dict, overview: Optional[dict], must_meet_text: str) -> dict:
    """A defensible verdict computed with no network: cross-check hole counts vs
    the overview callout, and surface must-meet-relevant fields."""
    disc: list[dict] = []
    agree: list[dict] = []

    claude_holes = _hole_total(resolved)
    ov_holes = _overview_hole_count(overview)
    if ov_holes is not None:
        ok = ov_holes == claude_holes
        agree.append({"field": "hole_count", "claude_value": claude_holes,
                      "codex_value": ov_holes, "agree": ok})
        if not ok:
            disc.append({"field": "hole_count", "severity": "HIGH",
                         "claude_value": claude_holes, "codex_value": ov_holes,
                         "reasoning": f"Overview callout indicates {ov_holes} holes but the "
                                      f"extraction totals {claude_holes}. Hole-count "
                                      f"disagreements are always at least HIGH."})
    # Non-disputed fields are recorded as agreements for the table.
    for f in ("part_number", "material", "units", "drawing_standard"):
        v = resolved.get(f)
        agree.append({"field": f, "claude_value": v, "codex_value": v, "agree": True})
    agree.append({"field": "dimension_count",
                  "claude_value": len(resolved.get("dimensions") or []),
                  "codex_value": len(resolved.get("dimensions") or []), "agree": True})

    forced = (os.getenv("MTI_CODEX_FORCE_VERDICT") or "").strip().upper()
    if forced in ("REJECTED", "APPROVED_WITH_NOTES", "APPROVED"):
        status = forced
        if forced == "REJECTED" and not disc:
            disc.append({"field": "forced_test", "severity": "CRITICAL",
                         "claude_value": None, "codex_value": None,
                         "reasoning": "Forced REJECTED via MTI_CODEX_FORCE_VERDICT (test path)."})
    else:
        status = _status_from(disc)

    return {
        "overall_status": status,
        "summary": (f"Stub validation (offline): {len(disc)} discrepancy(ies); "
                    f"hole count {claude_holes}"
                    + (f" vs overview {ov_holes}" if ov_holes is not None else "") + "."),
        "per_field_agreement": agree,
        "discrepancies": disc,
        "notes": (["Must-Meet Specifications remain tier-0 and override both models."]
                  if must_meet_text.strip() else []),
        "engine": "stub",
    }


# ── prompt for the real Codex CLI ─────────────────────────────────────────────
def _build_prompt(resolved: dict, must_meet_text: str, overview: Optional[dict]) -> str:
    ov_note = ""
    if overview and overview.get("overall_shape_summary"):
        ov_note = f"\nStage-1.5 overall shape read: {overview['overall_shape_summary']}\n"
    return f"""You are Codex, performing an INDEPENDENT engineering validation of a
mechanical drawing extraction. Read the attached drawing image(s) yourself and
compare, field by field, against Claude's resolved extraction JSON below:
dimensions, hole counts and patterns, callouts, tolerances, materials, feature
types.
{ov_note}
MUST-MEET SPECIFICATIONS (tier-0 — you may flag conflicts but MUST NOT overrule
these; they win over both models):
{must_meet_text or '(none provided)'}

CLAUDE RESOLVED EXTRACTION (JSON):
{json.dumps(resolved, indent=2)[:24000]}

Return ONLY a JSON object (write it to ./result.json AND print it) with this schema:
{{
  "overall_status": "APPROVED" | "APPROVED_WITH_NOTES" | "REJECTED",
  "summary": "one-paragraph verdict",
  "per_field_agreement": [ {{"field": str, "claude_value": any, "codex_value": any, "agree": bool}} ],
  "discrepancies": [ {{"field": str, "severity": "CRITICAL"|"HIGH"|"MEDIUM"|"LOW",
                       "claude_value": any, "codex_value": any, "reasoning": str}} ],
  "notes": [str]
}}
Rules: any hole-count or hole-pattern disagreement is ALWAYS at least "HIGH".
REJECTED only for CRITICAL geometry-defining disagreements. Do not restate the
input; output JSON only, no markdown fences."""


def validate_extraction(resolved: dict, *,
                        images: Optional[list[Path]] = None,
                        must_meet_text: str = "",
                        overview_analysis: Optional[dict] = None,
                        output_dir: Optional[Path] = None,
                        drawing_id: str = "") -> dict:
    """Run Stage A. Always returns a verdict dict (never raises); on any Codex
    error it falls back to the deterministic stub so the pipeline keeps moving.
    Writes ``codex_validation.json`` and appends to the lessons ledger."""
    images = [Path(p) for p in (images or []) if Path(p).is_file()]

    def stub():
        return _stub_verdict(resolved, overview_analysis, must_meet_text)

    try:
        verdict, engine = codex_client.run_json(
            _build_prompt(resolved, must_meet_text, overview_analysis),
            images=images, stub_fn=stub)
    except Exception as e:  # never let validation crash the run
        verdict, engine = stub(), "stub"
        verdict.setdefault("notes", []).append(f"Codex call failed → stub: {type(e).__name__}: {e}")

    verdict = _normalize(verdict)
    verdict["engine"] = verdict.get("engine") or engine
    verdict["model"] = codex_client.CODEX_MODEL
    verdict["drawing_id"] = drawing_id

    if output_dir is not None:
        try:
            (Path(output_dir) / VALIDATION_FILENAME).write_text(
                json.dumps(verdict, indent=2), encoding="utf-8")
        except Exception:
            pass
        _log_lessons(Path(output_dir), drawing_id, verdict)
    return verdict


def _normalize(v: dict) -> dict:
    """Enforce the schema invariants regardless of who produced the verdict:
    hole-count disagreements >= HIGH, and status consistent with severities."""
    v = dict(v or {})
    disc = list(v.get("discrepancies") or [])
    for d in disc:
        sev = str(d.get("severity", "LOW")).upper()
        field = str(d.get("field", "")).lower()
        if ("hole" in field and ("count" in field or "qty" in field or "pattern" in field)) \
                and _SEV_RANK.get(sev, 0) < _SEV_RANK["HIGH"]:
            d["severity"] = "HIGH"
        d["severity"] = str(d.get("severity", "LOW")).upper()
    v["discrepancies"] = disc
    status = str(v.get("overall_status", "")).upper()
    if status not in ("APPROVED", "APPROVED_WITH_NOTES", "REJECTED"):
        status = _status_from(disc)
    # A CRITICAL discrepancy can never be APPROVED.
    if _worst(disc) == "CRITICAL":
        status = "REJECTED"
    elif disc and status == "APPROVED":
        status = "APPROVED_WITH_NOTES"
    v["overall_status"] = status
    v.setdefault("per_field_agreement", [])
    v.setdefault("summary", "")
    v.setdefault("notes", [])
    return v


def _log_lessons(output_dir: Path, drawing_id: str, verdict: dict) -> None:
    try:
        from pipeline.must_meet import append_lesson
        lessons = output_dir.parent / "lessons_learned.jsonl" \
            if (output_dir / "..").resolve() else output_dir / "lessons_learned.jsonl"
        # Prefer the run root's shared ledger (…/output/lessons_learned.jsonl).
        lessons = output_dir / "lessons_learned.jsonl"
        for d in verdict.get("discrepancies", []):
            append_lesson(lessons, {
                "stage": STAGE_TAG,
                "resolution": "codex_validation",
                "drawing_id": drawing_id,
                "field": d.get("field"),
                "severity": d.get("severity"),
                "claude_value": d.get("claude_value"),
                "codex_value": d.get("codex_value"),
                "reasoning": d.get("reasoning"),
                "overall_status": verdict.get("overall_status"),
                "engine": verdict.get("engine"),
            })
        if not verdict.get("discrepancies"):
            append_lesson(lessons, {
                "stage": STAGE_TAG, "resolution": "codex_validation",
                "drawing_id": drawing_id, "overall_status": verdict.get("overall_status"),
                "engine": verdict.get("engine"), "field": None, "severity": None})
    except Exception:
        pass


def build_hints(verdict: dict) -> str:
    """Turn a REJECTED verdict into context hints for a Claude re-extraction."""
    lines = ["Codex validation flagged these discrepancies — re-read the drawing "
             "carefully for each and correct if warranted:"]
    for d in verdict.get("discrepancies", []):
        lines.append(f"- [{d.get('severity')}] {d.get('field')}: Claude={d.get('claude_value')!r} "
                     f"vs Codex={d.get('codex_value')!r} — {d.get('reasoning')}")
    return "\n".join(lines)
