"""Overview-analysis macro validation (2026-07-16).

Cross-checks the GENERATED macro package against the Stage 1.5 holistic
overview analysis — the "extraction words" of ``overview_analysis.json``. Where
:mod:`pipeline.macro_echo` proves every emitted literal round-trips to the
build plan, this stage proves the PACKAGE AS A WHOLE agrees with what the
overview pass said the drawing shows:

* **note counts** — a global note stating a feature count ("(6) HLS" →
  ``resolved_count: 6``) must equal the number of hole instances the macros
  actually drill (baked circles + circular-pattern instances);
* **correspondence coverage** — every cross-view feature the overview saw
  ("center_bore seen in front+side") must map to at least one generated build
  step, matched on canonicalized words (bore/HLS/drilled → hole, …);
* **through-vs-blind agreement** — when a correspondence's relation text
  confirms a THROUGH (or blind) feature, the matched step's ``depth_type``
  must not contradict it;
* **conflict carryover** — unreconciled CRITICAL/HIGH overview conflicts are
  re-surfaced at macro time (upstream flags own the resolution; this keeps
  them visible next to the macros they affect);
* **symmetry advisory** — declared rotational symmetry with no pattern step is
  reported (informational; X/Y dimensioning is valid evidence against polar).

Pipeline principle unchanged: *resolve and flag, never block*. The validator is
ADVISORY by default — it writes ``<Part>_macro_overview_validation.json``, logs
findings, and folds FAIL entries into the engineering review; it never raises
unless the caller opts into ``assert_overview_macro_validation``.

Public entry points: :func:`validate_macros_against_overview` (pure),
:func:`run_overview_macro_validation` (load + validate + persist),
:func:`assert_overview_macro_validation` (strict).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

REPORT_SUFFIX = "_macro_overview_validation.json"

# Canonical-word map: overview prose and step descriptions meet on these stems.
# Values are the canonical token; keys are the drawing/extraction spellings.
_SYNONYMS: dict[str, str] = {
    "bore": "hole", "bores": "hole", "holes": "hole", "hls": "hole",
    "drill": "hole", "drilled": "hole", "drilling": "hole",
    "tap": "thread", "taps": "thread", "tapped": "thread", "thd": "thread",
    "threads": "thread", "threaded": "thread",
    "cbore": "counterbore", "counterbored": "counterbore",
    "csk": "countersink", "countersunk": "countersink",
    "notch": "slot", "notches": "slot", "slots": "slot", "cutout": "slot",
    "cutouts": "slot", "keyway": "slot",
    "bosses": "boss",
    "chamfers": "chamfer", "fillets": "fillet", "radii": "fillet",
    "thru": "through",
    "patterns": "pattern", "bolts": "bolt",
    "tabs": "tab",
}

# Words that carry no feature identity (view names, articles, drafting boilerplate).
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "for", "with", "and", "or", "to",
    "is", "are", "as", "by", "its", "it", "this", "that", "vs", "not", "same",
    "view", "views", "front", "side", "top", "bottom", "left", "right",
    "section", "detail", "profile", "drawing", "sheet", "line", "lines",
    "hidden", "visible", "shown", "seen", "corresponds", "corresponding",
    "confirms", "confirmed", "matching", "matches", "feature", "features",
    "all", "one", "two", "three", "four", "five", "six", "dia", "diameter",
})

# Overview correspondences that describe sheet furniture, not part geometry —
# there is legitimately no build step for these.
_NON_GEOMETRY_TOKENS = frozenset({
    "title", "block", "border", "note", "notes", "finish", "inspection",
    "balloon", "balloons", "revision", "tolerance", "tolerances",
})

# Step types that create (or pattern) part geometry — the match pool.
_GEOMETRY_STEP_TYPES = frozenset({
    "extrude_boss", "extrude_cut", "hole", "thread", "revolve", "mirror",
    "pattern", "slot_rect_cut", "slot_corner_fillet", "circular_pattern",
    "fillet", "chamfer", "fillet/chamfer",
})

# Note words that mean the count refers to drilled/tapped features.
_HOLE_COUNT_TOKENS = frozenset({"hole", "thread", "counterbore", "countersink"})


class OverviewMacroValidationError(Exception):
    """Strict mode: the macro package contradicts the overview analysis."""


@dataclass
class ValidationEntry:
    check: str          # note_count | correspondence_coverage | through_blind |
                        # conflict_carryover | symmetry_advisory
    status: str         # PASS | WARN | FAIL
    severity: str       # CRITICAL | HIGH | MEDIUM | LOW
    subject: str        # the overview words being checked (note text / feature name)
    detail: str
    matched_feature_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check, "status": self.status, "severity": self.severity,
            "subject": self.subject, "detail": self.detail,
            "matched_feature_ids": self.matched_feature_ids,
        }


@dataclass
class OverviewMacroReport:
    entries: list[ValidationEntry] = field(default_factory=list)
    planned_hole_instances: int = 0

    @property
    def ok(self) -> bool:
        return not any(e.status == "FAIL" for e in self.entries)

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "WARN": 0, "FAIL": 0}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": self.counts(),
            "planned_hole_instances": self.planned_hole_instances,
            "entries": [e.to_dict() for e in self.entries],
        }

    def review_items(self) -> list[dict[str, Any]]:
        """FAIL findings as engineering-review items (same dict shape as
        :func:`pipeline.engineering_review.build_review_items` emits)."""
        items: list[dict[str, Any]] = []
        for e in self.entries:
            if e.status != "FAIL":
                continue
            items.append({
                "severity": e.severity,
                "source": "overview_macro_validation",
                "id": ",".join(e.matched_feature_ids) or "-",
                "what": f"Macro package disagrees with the overview analysis "
                        f"({e.check}): {e.subject}",
                "decision": "macros were generated as planned; the disagreement is "
                            "flagged, not silently reconciled",
                "why": e.detail,
                "affects": ", ".join(e.matched_feature_ids) or "whole part",
            })
        return items


# --------------------------------------------------------------------------- #
# Word canonicalization
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> set[str]:
    """Lowercased canonical word set of a phrase (synonyms applied, stopwords
    and bare numbers dropped)."""
    out: set[str] = set()
    for raw in re.split(r"[^a-z0-9']+", (text or "").lower()):
        if not raw or raw.isdigit():
            continue
        word = _SYNONYMS.get(raw, raw)
        if word in _STOPWORDS or len(word) < 2:
            continue
        out.add(word)
    return out


def _step_tokens(step) -> set[str]:
    toks = _tokens(f"{step.description} {step.feature_type} {step.notes}")
    toks |= _tokens(step.feature_id.replace("_", " "))
    toks.add(_SYNONYMS.get(step.feature_type, step.feature_type))
    return toks


def _geometry_steps(pkg) -> list[Any]:
    return [s for s in pkg.steps
            if s.feature_type in _GEOMETRY_STEP_TYPES
            and s.status in ("generated", "needs_review")]


def _planned_hole_instances(pkg) -> int:
    """Hole instances the package actually drills: baked circles on hole/thread
    steps plus circular-pattern copies (total_instances INCLUDES the seed, and
    the seed is its own hole step — count total-1 for the pattern)."""
    total = 0
    for s in pkg.steps:
        if s.status not in ("generated", "needs_review"):
            continue
        if s.feature_type in ("hole", "thread"):
            total += max(1, len(s.positions_xy))
        elif s.feature_type == "circular_pattern":
            n = int((s.circular_pattern or {}).get("total_instances", 0) or 0)
            total += max(0, n - 1)
    return total


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _check_note_counts(overview: dict, pkg, report: OverviewMacroReport) -> None:
    planned = report.planned_hole_instances
    for note in overview.get("global_notes") or []:
        count = note.get("resolved_count")
        if not count:
            continue
        if "inspection" in (note.get("applies_to") or "").lower():
            continue
        note_toks = _tokens(f"{note.get('note', '')} {note.get('applies_to', '')}")
        if not (note_toks & _HOLE_COUNT_TOKENS):
            continue  # a count of something the macros don't enumerate (e.g. views)
        subject = note.get("note", "")
        if planned == count:
            report.entries.append(ValidationEntry(
                "note_count", "PASS", "LOW", subject,
                f"drawing note states {count} hole feature(s); the macros drill "
                f"exactly {planned} instance(s)."))
        elif planned < count:
            report.entries.append(ValidationEntry(
                "note_count", "FAIL", "CRITICAL", subject,
                f"drawing note states {count} hole feature(s) but the macros only "
                f"drill {planned} instance(s) — {count - planned} instance(s) "
                f"missing from the build."))
        else:
            report.entries.append(ValidationEntry(
                "note_count", "WARN", "MEDIUM", subject,
                f"the macros drill {planned} hole instance(s), more than the "
                f"note's {count} — the note may govern only a subset "
                f"(one diameter/thread) of the part's holes."))


def _check_correspondences(overview: dict, pkg,
                           report: OverviewMacroReport) -> list[tuple[dict, list]]:
    """Coverage check. Returns (correspondence, matched_steps) pairs for the
    through/blind check to reuse."""
    steps = _geometry_steps(pkg)
    step_tok = [(s, _step_tokens(s)) for s in steps]
    matched_pairs: list[tuple[dict, list]] = []

    for corr in overview.get("cross_view_correspondences") or []:
        name = corr.get("feature", "")
        toks = _tokens(name.replace("_", " "))
        if not toks or toks <= _NON_GEOMETRY_TOKENS:
            continue
        hits = [s for s, st in step_tok if toks & st]
        confidence = (corr.get("confidence") or "medium").lower()
        if hits:
            matched_pairs.append((corr, hits))
            report.entries.append(ValidationEntry(
                "correspondence_coverage", "PASS", "LOW", name,
                f"overview feature matches {len(hits)} build step(s).",
                matched_feature_ids=sorted({s.feature_id for s in hits})))
        elif confidence == "high":
            report.entries.append(ValidationEntry(
                "correspondence_coverage", "FAIL", "HIGH", name,
                f"the overview analysis saw this feature across views "
                f"({', '.join(corr.get('seen_in') or []) or 'unspecified'}; "
                f"relation: {corr.get('relation', '')[:160]}) with HIGH confidence, "
                f"but no generated build step matches its words."))
        else:
            report.entries.append(ValidationEntry(
                "correspondence_coverage", "WARN",
                "MEDIUM" if confidence == "medium" else "LOW", name,
                f"no build step matches this {confidence}-confidence overview "
                f"feature — verify it is either built under another name or "
                f"genuinely not part geometry."))
    return matched_pairs


def _check_through_blind(matched_pairs: list[tuple[dict, list]],
                         report: OverviewMacroReport) -> None:
    for corr, hits in matched_pairs:
        # Overview prose confirms one reading by negating the other ("a THROUGH
        # bore, not a blind hole") — drop the negated mention before deciding.
        rel_text = re.sub(r"\bnot\s+(?:a\s+|an\s+)?(?:blind|through|thru)\b", " ",
                          (corr.get("relation") or "").lower())
        relation = _tokens(rel_text)
        says_through = "through" in relation
        says_blind = "blind" in relation
        if says_through == says_blind:  # neither, or contradictory prose — skip
            continue
        expected = "through_all" if says_through else "blind"
        typed = [s for s in hits if s.depth_type in ("blind", "through_all")]
        if not typed:
            continue
        wrong = [s for s in typed if s.depth_type != expected]
        name = corr.get("feature", "")
        if not wrong:
            report.entries.append(ValidationEntry(
                "through_blind", "PASS", "LOW", name,
                f"overview confirms {expected.replace('_all', '')}; every matched "
                f"step agrees.",
                matched_feature_ids=sorted({s.feature_id for s in typed})))
        elif len(wrong) == len(typed):
            report.entries.append(ValidationEntry(
                "through_blind", "FAIL", "CRITICAL", name,
                f"the overview's cross-view read confirms a "
                f"{expected.replace('_all', '')} feature "
                f"({corr.get('relation', '')[:160]}), but the matched step(s) are "
                f"built {wrong[0].depth_type} — building from one view alone "
                f"would produce a wrong part.",
                matched_feature_ids=sorted({s.feature_id for s in wrong})))
        else:
            report.entries.append(ValidationEntry(
                "through_blind", "WARN", "MEDIUM", name,
                f"overview confirms {expected.replace('_all', '')}; the matched "
                f"steps disagree among themselves — the words may span several "
                f"distinct features.",
                matched_feature_ids=sorted({s.feature_id for s in wrong})))


def _check_conflicts(overview: dict, report: OverviewMacroReport) -> None:
    for c in overview.get("cross_view_conflicts") or []:
        sev = (c.get("severity") or "MEDIUM").upper()
        if sev not in ("CRITICAL", "HIGH"):
            continue
        report.entries.append(ValidationEntry(
            "conflict_carryover", "WARN", sev, c.get("description", "")[:160],
            "unreconciled overview conflict still open at macro time — "
            + (c.get("recommendation") or "verify against the drawing.")))


def _check_symmetry(overview: dict, pkg, report: OverviewMacroReport) -> None:
    sym = (overview.get("symmetry") or {})
    if sym.get("type") not in ("rotational", "both"):
        return
    has_pattern = any(
        s.feature_type in ("circular_pattern", "pattern") or s.placement == "pattern"
        for s in pkg.steps)
    multi_hole = any(s.feature_type in ("hole", "thread") and len(s.positions_xy) >= 3
                     for s in pkg.steps)
    if not has_pattern and multi_hole:
        report.entries.append(ValidationEntry(
            "symmetry_advisory", "WARN", "LOW", f"symmetry: {sym.get('type')}",
            "the overview reports rotational symmetry but no pattern step exists — "
            "individually-placed holes are the safe default (X/Y dimensioning is "
            "evidence against a polar pattern); flagged for awareness only. "
            + (sym.get("notes") or "")))


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def validate_macros_against_overview(model, pkg, overview: dict) -> OverviewMacroReport:
    """Pure validation of a generated :class:`MacroPackage` against the Stage 1.5
    ``overview_analysis`` dict. Deterministic; no I/O."""
    report = OverviewMacroReport(planned_hole_instances=_planned_hole_instances(pkg))
    _check_note_counts(overview, pkg, report)
    matched = _check_correspondences(overview, pkg, report)
    _check_through_blind(matched, report)
    _check_conflicts(overview, report)
    _check_symmetry(overview, pkg, report)
    return report


def run_overview_macro_validation(
    model, pkg, overview: Optional[dict] = None, write: bool = True,
) -> Optional[OverviewMacroReport]:
    """Load ``overview_analysis.json`` from the package root (unless the dict is
    passed in), validate, persist ``<Part>_macro_overview_validation.json``, and
    log findings. Returns ``None`` when no overview analysis exists — the stage
    is additive and never breaks a run."""
    if overview is None:
        path = Path(pkg.root) / "overview_analysis.json"
        if not path.is_file():
            return None
        try:
            overview = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("overview macro validation: unreadable %s (%s) — skipped.",
                        path, e)
            return None
    report = validate_macros_against_overview(model, pkg, overview)
    if write:
        name = Path(pkg.build_plan_json).name.replace("_build_plan.json", "")
        out = Path(pkg.root) / f"{name}{REPORT_SUFFIX}"
        out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    counts = report.counts()
    for e in report.entries:
        if e.status == "FAIL":
            log.warning("overview macro validation FAIL [%s] %s: %s",
                        e.check, e.subject, e.detail)
        elif e.status == "WARN":
            log.info("overview macro validation WARN [%s] %s", e.check, e.subject)
    log.info("overview macro validation: %d PASS / %d WARN / %d FAIL "
             "(%d planned hole instance(s))",
             counts["PASS"], counts["WARN"], counts["FAIL"],
             report.planned_hole_instances)
    return report


def assert_overview_macro_validation(model, pkg,
                                     overview: Optional[dict] = None) -> OverviewMacroReport:
    """Strict variant: raise :class:`OverviewMacroValidationError` on any FAIL."""
    report = run_overview_macro_validation(model, pkg, overview)
    if report is not None and not report.ok:
        fails = [e for e in report.entries if e.status == "FAIL"]
        detail = "; ".join(f"[{e.check}] {e.subject}: {e.detail}" for e in fails[:10])
        raise OverviewMacroValidationError(
            f"Macro package contradicts the overview analysis "
            f"({len(fails)} failure(s)): {detail}")
    return report
