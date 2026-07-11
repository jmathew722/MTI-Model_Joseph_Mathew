"""Task 5 — pattern learning from human-assist answers.

A human answer isn't consumed once and forgotten. When the SAME kind of
ambiguity, with a matching drawing-pattern signature, recurs and is answered the
same way >=2 times, that's a reusable convention (e.g. a drafting style that
trips the callout parser) — not a one-off. Generalizing it means the next part
with the same signature does not re-escalate.

Guardrails against overfitting (this is judgment work, kept conservative and
auditable):
  * Generalize ONLY when a signature recurs >=2 times AND the answers agree on
    the SAME relative choice (e.g. "always the larger candidate", "always the
    chain-closing value") — not on a specific numeric value, which is
    part-specific and must never leak across drawings.
  * Record every generalization to ``pipeline/LEARNED_PATTERNS.md`` with its
    evidence (the source lessons), so the reasoning is auditable and reversible.
  * The pipeline never AUTO-APPLIES a learned numeric value to a different part.
    A learned pattern lowers a matching question's priority / raises a
    candidate's confidence (a bias), so a genuine answer is still requested when
    anything differs — a wrong generalization can only mis-rank, never fabricate.

Public: :func:`signature_for`, :func:`scan_and_generalize`,
:func:`known_patterns`, :class:`LearnedPattern`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

LEARNED_JSON = "learned_patterns.json"
LEARNED_MD = "LEARNED_PATTERNS.md"
MIN_RECURRENCE = 2


@dataclass
class LearnedPattern:
    signature: str
    kind: str
    choice_rule: str            # e.g. "prefer_larger_candidate", "prefer_index_1"
    count: int
    evidence: list[dict] = field(default_factory=list)  # source Q+A summaries

    def as_dict(self) -> dict[str, Any]:
        return {"signature": self.signature, "kind": self.kind,
                "choice_rule": self.choice_rule, "count": self.count,
                "evidence": self.evidence}


def signature_for(kind: str, applies_to: str, n_candidates: int) -> str:
    """A drawing-pattern fingerprint: the ambiguity's KIND + what it dimensions +
    how many readings competed. Deliberately value-free so it generalizes across
    parts without carrying a part-specific number."""
    tok = (applies_to or "value").strip().lower().split("_")[0] or "value"
    return f"{kind}:{tok}:{max(0, int(n_candidates))}"


def _choice_rule(answer, candidates: list) -> Optional[str]:
    """How the human's answer relates to the offered candidates — the
    generalizable part. Returns None when the answer isn't one of the
    candidates (a free-text override, not a rule we can safely learn)."""
    vals = []
    for c in candidates:
        v = c.get("value") if isinstance(c, dict) else c
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            return None
    try:
        a = float(answer)
    except (TypeError, ValueError):
        return None
    if not vals:
        return None
    # Match to a candidate.
    idx = min(range(len(vals)), key=lambda i: abs(vals[i] - a))
    if abs(vals[idx] - a) > 1e-6 * max(1.0, abs(a)):
        return None  # answer not among candidates -> not a learnable choice rule
    if a >= max(vals) - 1e-9:
        return "prefer_larger_candidate"
    if a <= min(vals) + 1e-9:
        return "prefer_smaller_candidate"
    return f"prefer_index_{idx}"


def scan_and_generalize(lessons_path: Path, out_dir: Optional[Path] = None) -> list[LearnedPattern]:
    """Read human-assist Q+A from ``lessons_learned.jsonl``, and when a signature
    recurs >=MIN_RECURRENCE times with the SAME choice rule, record a
    LearnedPattern to LEARNED_PATTERNS.md + learned_patterns.json. Returns the
    patterns generalized this pass. Never raises."""
    lessons_path = Path(lessons_path)
    if not lessons_path.is_file():
        return []
    out_dir = Path(out_dir) if out_dir else Path(__file__).parent

    # Pair questions with their answers by question_id.
    questions: dict[str, dict] = {}
    answers: dict[str, Any] = {}
    try:
        for line in lessons_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("kind") == "human_assist_question":
                questions[rec.get("question_id")] = rec
            elif rec.get("kind") == "human_assist_answer":
                answers[rec.get("question_id")] = rec.get("answer")
    except Exception as e:
        log.warning("learned_patterns: could not read lessons: %s", e)
        return []

    # Group answered questions by signature + choice rule.
    buckets: dict[tuple, list[dict]] = {}
    for qid, ans in answers.items():
        q = questions.get(qid)
        if not q:
            continue
        cands = q.get("candidates") or []
        rule = _choice_rule(ans, cands)
        if rule is None:
            continue
        applies = _applies_from_text(q.get("question_text", ""))
        sig = signature_for(q.get("question_kind", "other"), applies, len(cands))
        buckets.setdefault((sig, rule), []).append(
            {"question_id": qid, "part": q.get("part"), "answer": ans,
             "kind": q.get("question_kind")})

    learned: list[LearnedPattern] = []
    for (sig, rule), evid in buckets.items():
        # Distinct PARTS >= threshold (avoid one weird part re-answered).
        parts = {e.get("part") for e in evid}
        if len(parts) >= MIN_RECURRENCE:
            learned.append(LearnedPattern(signature=sig, kind=sig.split(":")[0],
                                          choice_rule=rule, count=len(parts), evidence=evid))
    if learned:
        _persist(learned, out_dir)
    return learned


def _applies_from_text(text: str) -> str:
    """Best-effort applies_to token from the question text (parenthetical hint)."""
    import re

    m = re.search(r"\(([a-zA-Z_]+)\)", text or "")
    return m.group(1) if m else "value"


def known_patterns(out_dir: Optional[Path] = None) -> dict[str, dict]:
    """Effective learned-pattern map (signature -> {choice_rule, count})."""
    path = (Path(out_dir) if out_dir else Path(__file__).parent) / LEARNED_JSON
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("patterns", {})
        except Exception:
            return {}
    return {}


def _persist(learned: list[LearnedPattern], out_dir: Path) -> None:
    # JSON (machine).
    path = out_dir / LEARNED_JSON
    data: dict[str, Any] = {"patterns": {}}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("patterns", {})
        except Exception:
            data = {"patterns": {}}
    for lp in learned:
        data["patterns"][lp.signature] = {"choice_rule": lp.choice_rule, "count": lp.count}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Markdown (human, auditable, reversible).
    md = out_dir / LEARNED_MD
    header = ("# Learned Ambiguity Patterns\n\n"
              "Auto-recorded by `pipeline/learned_patterns.py` when the same kind of "
              "human-assist ambiguity (same value-free signature) is answered the same "
              "relative way across >=2 parts. These bias question priority / candidate "
              "confidence — they never auto-apply a numeric value across drawings. "
              "Delete an entry (and its `learned_patterns.json` key) to reverse it.\n\n"
              "| Signature | Choice rule | Parts | Evidence (question ids) |\n"
              "|---|---|---|---|\n")
    rows = []
    for lp in learned:
        qids = ", ".join(e.get("question_id", "?") for e in lp.evidence[:5])
        rows.append(f"| `{lp.signature}` | {lp.choice_rule} | {lp.count} | {qids} |")
    try:
        existing = md.read_text(encoding="utf-8") if md.is_file() else ""
        if not existing.startswith("# Learned Ambiguity Patterns"):
            existing = header
        # Append only new signatures not already present.
        for row in rows:
            sig_cell = row.split("|")[1].strip()
            if sig_cell not in existing:
                existing = existing.rstrip() + "\n" + row + "\n"
        md.write_text(existing if existing.startswith("#") else header + "\n".join(rows) + "\n",
                      encoding="utf-8")
    except Exception as e:
        log.warning("learned_patterns: could not write %s: %s", LEARNED_MD, e)
