"""Human-assist escalation layer (2026-07-10).

The closed-loop system (resolver ladder -> reconciliation -> Phase B correction
loop -> Phase D method experiments) exhausts every *automated* path to a value.
Some blockers, though, are not reasoning problems — they are MISSING FACTS
(an occluded dimension, a cropped title block, genuine designer intent). No
amount of re-looping manufactures a fact that isn't on the sheet, and each retry
costs a slow SolidWorks COM round-trip. This layer is the exit ramp: once the
automated ladder is genuinely exhausted for one item, it pauses THAT item (never
the run) and emits a narrow, specific question with the drawing region and
pre-populated candidate answers.

Four design principles (mirrored in the code):
  * Escalate LATE, escalate NARROW — only after all four automated stages fail,
    and always one specific fact, never "look at this drawing".
  * Never block the run — a pending question is a new *flagged* disposition
    (``NEEDS_HUMAN_INPUT``) that still SHIPS its best-available value; the part
    still produces a complete approximate model and its usual READY status.
  * Batch, don't interrupt — questions queue to ``<Part>_assist_queue.json`` and
    are answered together, not per-feature mid-build.
  * Answers become reusable knowledge — every Q+A goes to
    ``lessons_learned.jsonl``; a recurring pattern generalizes (see
    :mod:`pipeline.learned_patterns`) so the next similar part doesn't re-ask.

Public: :class:`Question`, :class:`AssistQueue`, :func:`generate_assist_queue`,
:func:`apply_answers`, :func:`escalation_eligible`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

ASSIST_QUEUE_SUFFIX = "_assist_queue.json"
DEFAULT_QUESTION_CAP = 3

# The automated ladder, in order. An item is escalation-eligible ONLY after every
# applicable stage has been attempted and failed.
LADDER_STAGES = (
    "resolver_plausibility",   # spec / chain / geometry / vector / format / std-size
    "typ_derivation",          # TYP + constraint-graph propagation
    "correction_loop",         # Phase B build->measure->correct, up to its cap
    "method_experiments",      # Phase D scratch-part method matrix (chronic classes only)
)
# Only construction-class features (holes/slots/cuts) can be helped by a method
# experiment; for a pure dimension/position ambiguity that stage is not
# applicable and counts as satisfied (there is no method to try).
CHRONIC_KINDS = {"unresolved_position", "unclassifiable_callout"}

# Question kinds.
KIND_AMBIGUOUS_DIM = "ambiguous_dimension"
KIND_UNRESOLVED_POS = "unresolved_position"
KIND_UNCLASSIFIABLE = "unclassifiable_callout"
KIND_CONFLICTING_VIEWS = "conflicting_views"
KIND_OTHER = "other"

# Statuses.
PENDING = "pending"
ANSWERED = "answered"
EXPIRED = "expired"


def escalation_eligible(stages_attempted, kind: str) -> bool:
    """True only once the FULL applicable automated ladder is exhausted.

    ``method_experiments`` is required only for chronic construction kinds; for a
    dimension/view ambiguity there is no construction method to try, so that
    stage is auto-satisfied. Escalating before the ladder is done defeats the
    purpose (a human answer that automation would have found); escalating never
    wastes the exact COM cycles this layer exists to save."""
    attempted = set(stages_attempted or ())
    required = {"resolver_plausibility", "typ_derivation", "correction_loop"}
    if kind in CHRONIC_KINDS:
        required.add("method_experiments")
    return required.issubset(attempted)


@dataclass
class Candidate:
    value: Any
    basis: str
    confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "basis": self.basis,
                "confidence": round(float(self.confidence), 3)}


@dataclass
class Question:
    question_id: str
    part: str
    feature_id: str
    kind: str
    question_text: str
    default_if_unanswered: Any            # ALWAYS populated — the value that ships
    candidates: list[Candidate] = field(default_factory=list)
    region_crop: str = ""
    automated_attempts: list[str] = field(default_factory=list)  # audit trail
    target_dimension_id: str = ""         # which dim the answer feeds (if any)
    priority: float = 0.0
    status: str = PENDING
    created_at: str = ""
    answered_at: Optional[str] = None
    answer: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id, "part": self.part,
            "feature_id": self.feature_id, "kind": self.kind,
            "question_text": self.question_text,
            "region_crop": self.region_crop,
            "candidates": [c.as_dict() for c in self.candidates],
            "automated_attempts": self.automated_attempts,
            "target_dimension_id": self.target_dimension_id,
            "default_if_unanswered": self.default_if_unanswered,
            "priority": round(self.priority, 3),
            "status": self.status, "created_at": self.created_at,
            "answered_at": self.answered_at, "answer": self.answer,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Question":
        return cls(
            question_id=d["question_id"], part=d.get("part", ""),
            feature_id=d.get("feature_id", ""), kind=d.get("kind", KIND_OTHER),
            question_text=d.get("question_text", ""),
            default_if_unanswered=d.get("default_if_unanswered"),
            candidates=[Candidate(**c) for c in d.get("candidates", [])],
            region_crop=d.get("region_crop", ""),
            automated_attempts=d.get("automated_attempts", []),
            target_dimension_id=d.get("target_dimension_id", ""),
            priority=d.get("priority", 0.0), status=d.get("status", PENDING),
            created_at=d.get("created_at", ""), answered_at=d.get("answered_at"),
            answer=d.get("answer"),
        )


@dataclass
class AssistQueue:
    part: str
    questions: list[Question] = field(default_factory=list)
    cap: int = DEFAULT_QUESTION_CAP

    def pending(self) -> list[Question]:
        return [q for q in self.questions if q.status == PENDING]

    def as_dict(self) -> dict[str, Any]:
        return {"part": self.part, "cap": self.cap,
                "questions": [q.as_dict() for q in self.questions]}

    def write(self, part_dir: Path, safe_name: str) -> Path:
        path = Path(part_dir) / f"{safe_name}{ASSIST_QUEUE_SUFFIX}"
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "AssistQueue":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        q = cls(part=d.get("part", ""), cap=d.get("cap", DEFAULT_QUESTION_CAP))
        q.questions = [Question.from_dict(x) for x in d.get("questions", [])]
        return q


# --------------------------------------------------------------------------- #
# Severity-based prioritization
# --------------------------------------------------------------------------- #
_ENVELOPE_TOKENS = ("length", "width", "height", "overall", "envelope", "thickness", "depth")


def _priority_for(kind: str, applies_to: str, downstream_fanout: int,
                  flag_tier: str) -> float:
    """Rank a candidate question. A base/envelope dimension (everything else is
    positioned relative to it) outranks one slot's radius; a dimension many
    features depend on outranks a leaf; CRITICAL outranks LOW. Same severity
    signal the engineering review uses."""
    score = 0.0
    a = (applies_to or "").lower()
    if any(t in a for t in _ENVELOPE_TOKENS):
        score += 100.0                      # base profile — highest leverage
    score += 5.0 * max(0, downstream_fanout)  # fan-out: how many features depend on it
    tier_rank = {"CRITICAL": 40, "HIGH": 30, "MEDIUM": 20, "LOW": 10}
    score += tier_rank.get((flag_tier or "").upper(), 15)
    if kind == KIND_UNRESOLVED_POS:
        score += 8.0
    return score


def prioritize_and_cap(questions: list[Question], cap: int) -> list[Question]:
    """Keep the ``cap`` highest-leverage questions; the rest are dropped from the
    surface (their default still ships and they re-surface on a later run)."""
    ordered = sorted(questions, key=lambda q: (-q.priority, q.feature_id, q.question_id))
    return ordered[:max(0, cap)]


def _qid(part: str, feature_id: str, kind: str) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in f"{part}-{feature_id}-{kind}")
    return f"Q-{safe}".strip("-")


# A resolution basis meaning the value was read/derived confidently — such a
# dimension is NOT an open ambiguity worth a human question.
_CONFIDENT_BASES = {"explicit_callout", "arithmetic_chain", "spec_driven",
                    "human_provided", "stock_dimension"}


def _fanout_map(model) -> dict[str, int]:
    """dim_id -> how many features consume it (downstream leverage)."""
    counts: dict[str, int] = {}
    if model is None:
        return counts
    for f in getattr(model, "features", []) or []:
        ids = list(getattr(f, "related_dimensions", []) or [])
        dep = getattr(f, "depth_dimension_id", "")
        if dep:
            ids.append(dep)
        for did in ids:
            counts[did] = counts.get(did, 0) + 1
    return counts


def _feature_of_dim(model, dim_id: str) -> str:
    if model is None:
        return ""
    for f in getattr(model, "features", []) or []:
        ids = list(getattr(f, "related_dimensions", []) or [])
        if getattr(f, "depth_dimension_id", ""):
            ids.append(f.depth_dimension_id)
        if dim_id in ids:
            return f.id
    return ""


def generate_assist_queue(
    *,
    resolution,
    part: str,
    part_dir: Path,
    safe_name: str,
    model=None,
    reconciliation_result=None,
    cap: int = DEFAULT_QUESTION_CAP,
    crop_fn: Optional[Callable[[str, str], str]] = None,
    now: str = "",
    lessons_path: Optional[Path] = None,
) -> AssistQueue:
    """Build the escalation queue for one part AFTER the automated ladder + the
    reconciliation/correction loop have run. Only genuinely-exhausted items
    become questions; each is capped and prioritized. Never raises."""
    questions: list[Question] = []
    try:
        raw_dims = {d.get("id"): d for d in
                    (resolution.resolved_extraction.get("dimensions", []) if resolution else [])}
        fanout = _fanout_map(model)

        # 1) Ambiguous dimensions — multiple plausible readings the ladder could
        #    not disambiguate (the "0.44 or 0.94" case).
        for dim_id, dr in (resolution.dim_resolutions.items() if resolution else []):
            raw = raw_dims.get(dim_id, {}) or {}
            poss = [p for p in (raw.get("possible_values") or []) if isinstance(p, (int, float))]
            if not (raw.get("value_unclear") and len(set(poss)) >= 2):
                continue
            if (dr.assumption_basis or "") in _CONFIDENT_BASES:
                continue
            stages = {"resolver_plausibility", "typ_derivation", "correction_loop"}
            if not escalation_eligible(stages, KIND_AMBIGUOUS_DIM):
                continue
            applies = raw.get("applies_to", "") or raw.get("type", "value")
            fid = raw.get("feature_ref", "") or _feature_of_dim(model, dim_id)
            cands = [Candidate(v, "plausible chain/geometry reading",
                               0.5 if v == dr.resolved_value else 0.3) for v in dict.fromkeys(poss)]
            text = (f"Is {dim_id} ({applies}) "
                    + " or ".join(f"{v:g}" for v in dict.fromkeys(poss))
                    + "? The automated ladder could not disambiguate these readings.")
            questions.append(Question(
                question_id=_qid(part, dim_id, KIND_AMBIGUOUS_DIM), part=part,
                feature_id=fid, kind=KIND_AMBIGUOUS_DIM, question_text=text,
                default_if_unanswered=dr.resolved_value, candidates=cands,
                target_dimension_id=dim_id,
                region_crop=(crop_fn(fid, dim_id) if crop_fn else ""),
                automated_attempts=[dr.human_note] if dr.human_note else [],
                priority=_priority_for(KIND_AMBIGUOUS_DIM, applies,
                                       fanout.get(dim_id, 0), dr.flag_tier),
                created_at=now, status=PENDING))

        # 2) Excluded features — a driving fact (position or a parameter) is
        #    missing from the sheet; no re-reasoning recovers it.
        for fl in (resolution.flags if resolution else []):
            if not fl.get("excluded_from_build"):
                continue
            fid = fl.get("feature_id") or fl.get("dimension_id") or "?"
            src = fl.get("source", "")
            kind = KIND_UNRESOLVED_POS if "position" in src else KIND_UNCLASSIFIABLE
            # Missing-info exclusion: no construction method can help, so
            # method_experiments is not applicable (auto-satisfied).
            stages = {"resolver_plausibility", "typ_derivation", "correction_loop",
                      "method_experiments"}
            if not escalation_eligible(stages, kind):
                continue
            region = fl.get("expected_region") or {}
            text = (f"{fid}: {fl.get('human_note', 'a driving value could not be resolved from the drawing.')}"
                    )[:400]
            questions.append(Question(
                question_id=_qid(part, fid, kind), part=part, feature_id=fid, kind=kind,
                question_text=text,
                default_if_unanswered=(f"ships EXCLUDED (flagged model-derived assumption); "
                                       f"expected region {region}" if region else
                                       "ships EXCLUDED (flagged model-derived assumption)"),
                candidates=[],
                region_crop=(crop_fn(fid, "") if crop_fn else ""),
                automated_attempts=[fl.get("human_note", "")],
                priority=_priority_for(kind, "", fanout.get(fid, 0),
                                       fl.get("flag_tier", "CRITICAL")),
                created_at=now, status=PENDING))

        # 3) Reconciliation unresolved items (already through resolver + loop).
        for u in (getattr(reconciliation_result, "unresolved", []) or []):
            ud = u.as_dict() if hasattr(u, "as_dict") else dict(u)
            fid = ud.get("feature_id", "?")
            if any(q.feature_id == fid for q in questions):
                continue  # already covered above
            kind = KIND_UNRESOLVED_POS if "instance" in (ud.get("issue", "").lower()) \
                or "location" in (ud.get("issue", "").lower()) else KIND_OTHER
            stages = {"resolver_plausibility", "typ_derivation", "correction_loop", "method_experiments"}
            if not escalation_eligible(stages, kind):
                continue
            questions.append(Question(
                question_id=_qid(part, fid, kind), part=part, feature_id=fid, kind=kind,
                question_text=f"{fid}: {ud.get('issue', 'unresolved after the correction loop.')}"[:400],
                default_if_unanswered="ships with the best-available flagged value from reconciliation",
                candidates=[], region_crop=(crop_fn(fid, "") if crop_fn else ""),
                automated_attempts=[ud.get("resolution_attempted", "")],
                priority=_priority_for(kind, "", fanout.get(fid, 0), "CRITICAL"),
                created_at=now, status=PENDING))
    except Exception as e:  # the assist layer must never sink a run
        log.warning("assist queue generation failed (non-fatal): %s", e)

    kept = prioritize_and_cap(questions, cap)
    queue = AssistQueue(part=part, questions=kept, cap=cap)
    try:
        queue.write(part_dir, safe_name)
        if lessons_path is not None:
            _log_questions(lessons_path, kept)
    except Exception as e:
        log.warning("could not persist assist queue: %s", e)
    return queue


def apply_answers(part_dir: Path, safe_name: str,
                  answers: dict[str, Any],
                  lessons_path: Optional[Path] = None) -> dict[str, float]:
    """Record human answers into the queue and return the ``human_answers`` map
    (dimension_id -> value) to feed the resolver's top-priority tier on re-run.

    Only numeric answers to dimension-targeted questions become resolver inputs;
    every answer is still recorded on its question (and to lessons_learned) for
    the audit trail and pattern learning. No paid extraction anywhere here."""
    path = Path(part_dir) / f"{safe_name}{ASSIST_QUEUE_SUFFIX}"
    if not path.is_file():
        return {}
    queue = AssistQueue.load(path)
    human_answers: dict[str, float] = {}
    by_id = {q.question_id: q for q in queue.questions}
    for qid, ans in (answers or {}).items():
        q = by_id.get(qid)
        if q is None:
            continue
        q.answer = ans
        q.status = ANSWERED
        if q.target_dimension_id:
            try:
                human_answers[q.target_dimension_id] = float(ans)
            except (TypeError, ValueError):
                pass  # non-numeric answer — recorded, but not a resolver input
        if lessons_path is not None:
            _log_answer(lessons_path, q)
    queue.write(part_dir, safe_name)
    return human_answers


def rerun_with_answers(part_dir: Path, safe_name: str, answers: dict[str, Any],
                       *, requirements: Optional[list[str]] = None,
                       overview_analysis: Optional[dict] = None,
                       output_dir: Optional[Path] = None) -> dict[str, Any]:
    """Feed human answers back into resolution and re-splice the affected
    feature(s) — the Task 2 re-resolution path. Re-runs ONLY the deterministic
    resolver (never the extractor: no paid API call), then reuses the
    reconciliation splice-back to update build_plan.json + add a macro, exactly
    like a recovered reconciliation feature. Returns a summary dict.

    Never raises; a failure returns ``{"reresolved": False, "error": ...}``."""
    part_dir = Path(part_dir)
    result: dict[str, Any] = {"reresolved": False, "human_answers": {}, "recovered": []}
    try:
        human = apply_answers(part_dir, safe_name, answers,
                              lessons_path=(output_dir / "lessons_learned.jsonl") if output_dir else None)
        result["human_answers"] = human
        if not human:
            result["note"] = "no numeric dimension answers to re-resolve against"
            return result

        ext = part_dir / f"{safe_name}_extraction.json"
        if not ext.is_file():
            cands = list(part_dir.glob("*_extraction.json"))
            if not cands:
                result["error"] = "no extraction JSON found to re-resolve"
                return result
            ext = cands[0]
        raw = json.loads(ext.read_text(encoding="utf-8"))

        from pipeline.build_sequencer import STATE_EXCLUDED, sequence_build_order
        from pipeline.macro_generator import generate_macro_package  # noqa: F401 (splice uses it)
        from pipeline.reconciliation import _splice_recovered_features
        from pipeline.resolver import resolve_extraction
        from pipeline.validator import format_verification_report, run_verification

        # Old dispositions (to detect what the answer recovers).
        old_excluded: set[str] = set()
        for p in part_dir.glob("*_build_dispositions.json"):
            for d in json.loads(p.read_text(encoding="utf-8")):
                if d.get("state") == STATE_EXCLUDED:
                    old_excluded.add(d.get("feature_id"))
            break

        new_res = resolve_extraction(raw, requirements=requirements,
                                     overview_analysis=overview_analysis, human_answers=human)
        new_model, report = run_verification(new_res.clean_extraction)
        if new_model is None:
            result["error"] = "re-resolved extraction failed schema validation"
            return result
        new_seq = sequence_build_order(new_model, new_res)
        built_now = {d["feature_id"] for d in new_seq.disposition_table
                     if d.get("state") != STATE_EXCLUDED}
        # Features recovered by the human answer: previously excluded, now built.
        recovered = sorted(old_excluded & built_now)
        # Also re-emit any feature that consumes an answered dimension (its value
        # may have changed) even if it was already built.
        answered_dims = set(human.keys())
        for f in new_model.features:
            ids = set(getattr(f, "related_dimensions", []) or [])
            if getattr(f, "depth_dimension_id", ""):
                ids.add(f.depth_dimension_id)
            if ids & answered_dims and f.id not in recovered:
                recovered.append(f.id)

        # Persist the fresh resolved extraction.
        (part_dir / f"{safe_name}_resolved_extraction.json").write_text(
            json.dumps(new_res.resolved_extraction, indent=2), encoding="utf-8")

        if recovered:
            _splice_recovered_features(
                model=new_model, resolution=new_res, raw_extraction=raw,
                verification_text=format_verification_report(new_model, report),
                part_dir=part_dir, feature_ids=sorted(set(recovered)), pass_num=0)
        result["reresolved"] = True
        result["recovered"] = sorted(set(recovered))

        # Regenerate the assist queue: answered questions drop out of pending.
        new_queue = generate_assist_queue(
            resolution=new_res, part=safe_name, part_dir=part_dir, safe_name=safe_name,
            model=new_model,
            lessons_path=(output_dir / "lessons_learned.jsonl") if output_dir else None)
        # Preserve any already-answered questions' status in the rewritten queue.
        result["pending_questions"] = len(new_queue.pending())
        result["note"] = ("a full .sldprt rebuild is needed to reflect the recovered "
                          "geometry in the 3D model" if recovered else
                          "answers recorded; no excluded feature was recovered")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        log.warning("rerun_with_answers failed: %s", result["error"])
    return result


def overlay_dispositions(part_dir: Path, safe_name: str, queue: AssistQueue) -> Optional[Path]:
    """Overlay a NEEDS_HUMAN_INPUT flag onto the build-disposition table for every
    feature with an open question. ADDITIVE: adds ``needs_human_input`` +
    ``question_id`` fields; it does NOT change the geometric ``state`` (BUILT /
    EXCLUDED_INCOMPLETE), so existing build_dispositions.json consumers are
    unaffected. Returns the path written, or None if there is no table."""
    from pipeline.build_sequencer import STATE_NEEDS_HUMAN_INPUT

    candidates = list(Path(part_dir).glob("*_build_dispositions.json"))
    if not candidates:
        return None
    path = candidates[0]
    try:
        disps = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    by_fid: dict[str, str] = {q.feature_id: q.question_id for q in queue.pending() if q.feature_id}
    for d in disps:
        if d.get("feature_id") in by_fid:
            d["needs_human_input"] = True
            d["question_id"] = by_fid[d["feature_id"]]
            d["human_input_state"] = STATE_NEEDS_HUMAN_INPUT
    path.write_text(json.dumps(disps, indent=2), encoding="utf-8")
    return path


def _log_questions(lessons_path: Path, questions: list[Question]) -> None:
    try:
        from pipeline.must_meet import append_lesson

        for q in questions:
            append_lesson(Path(lessons_path), {
                "kind": "human_assist_question", "part": q.part,
                "feature_id": q.feature_id, "question_id": q.question_id,
                "question_kind": q.kind, "question_text": q.question_text,
                "candidates": [c.as_dict() for c in q.candidates],
                "default_if_unanswered": q.default_if_unanswered,
            })
    except Exception:
        pass


def _log_answer(lessons_path: Path, q: Question) -> None:
    try:
        from pipeline.must_meet import append_lesson

        append_lesson(Path(lessons_path), {
            "kind": "human_assist_answer", "part": q.part,
            "feature_id": q.feature_id, "question_id": q.question_id,
            "question_kind": q.kind, "question_text": q.question_text,
            "answer": q.answer,
        })
    except Exception:
        pass
