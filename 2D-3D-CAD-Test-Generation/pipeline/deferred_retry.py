"""Workstream 1 — deferred feature retry queue.

One bad feature must not stall the whole part. When a feature fails HARD during
the COM build, quarantine it and CONTINUE — the rest of the solid always
finishes. Then, after the part is otherwise complete, retry the deferred
features with the completed solid as context (its real faces/edges now exist),
using escalating strategies chosen from a failure taxonomy. Retry-after-
completion converges where retry-in-place loops: a hole whose target face didn't
exist yet (build-order defect) now has it; a fillet can enumerate real topology
instead of predicting it; bounding-box/mass sanity on the near-complete solid
localizes bad parameters.

This module is the orchestration + taxonomy (pure, injectable for tests). The
COM-specific retry actions (re-select by enumerated topology, re-derive from a
datum ref, etc.) are supplied by ``solidworks_builder`` via the injected
``retry_one`` callback, so the loop logic is unit-tested without SolidWorks.

States: a deferred feature ends BUILT (recovered), or — after the cap —
``deferred_open`` with its best diagnosis + a ready-to-answer clarification
question (never a silent forever-skip). Every attempt appends to
``_deferred_log.json``.

Public: :func:`classify_failure`, :func:`retry_strategies`, :class:`DeferredItem`,
:class:`DeferredQueue`, :func:`run_retry_passes`, :func:`clarification_question`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

DEFERRED_LOG = "_deferred_log.json"
DEFAULT_RETRY_CAP = 3

# ── Failure taxonomy ──────────────────────────────────────────────────────────
SELECTION = "selection_failure"
SKETCH_DEF = "sketch_over_under_defined"
GEOMETRY = "zero_thickness_or_geometry"
MISSING_PARENT = "missing_parent"
COM_TIMEOUT = "com_timeout"
PARAM_RANGE = "parameter_out_of_range"
UNKNOWN = "unknown"

# Ordered so a more-specific signature wins over a generic one.
_TAXONOMY: tuple[tuple[str, str], ...] = (
    (MISSING_PARENT, r"requires an existing solid|no seed feature|parent .*not|before any base"),
    (SELECTION, r"selectbyid2|could not select|failed to enter sketch|no active sketch|pattern axis"),
    (SKETCH_DEF, r"over-?defined|under-?defined"),
    (GEOMETRY, r"zero[- ]thickness|removes no material|outside the solid|returned nothing|returned none|no solid body"),
    (PARAM_RANGE, r"type mismatch|out of range|invalid parameter|-?2147\d{6}"),
    (COM_TIMEOUT, r"timeout|unresponsive|rpc|server is busy|call was rejected"),
)

# The escalating retry playbook per class (each attempt MUST change something —
# never retry the identical call twice; the last resort is the clarification gate).
_PLAYBOOK: dict[str, list[str]] = {
    SELECTION: ["reselect_by_enumerated_topology", "widen_selection_tolerance",
                "rederive_coords_from_datum", "clarify"],
    SKETCH_DEF: ["drop_redundant_constraint_keep_dims", "reemit_fully_dimensioned_from_datum", "clarify"],
    GEOMETRY: ["nudge_material_safe_direction", "rerun_conservative_resolution", "clarify"],
    MISSING_PARENT: ["reorder_after_parent", "chain_defer_with_dependency", "clarify"],
    COM_TIMEOUT: ["rebuild_doc_retry", "restart_session_reload_retry", "clarify"],
    PARAM_RANGE: ["clamp_within_general_tolerance", "clarify"],
    UNKNOWN: ["retry_after_completion", "clarify"],
}


def classify_failure(error_text: str) -> str:
    """Map a failure message/exception text to a taxonomy class."""
    t = (error_text or "").lower()
    for cls, pat in _TAXONOMY:
        if re.search(pat, t):
            return cls
    return UNKNOWN


def retry_strategies(error_class: str) -> list[str]:
    """The escalating strategy list for a class (ends in 'clarify')."""
    return list(_PLAYBOOK.get(error_class, _PLAYBOOK[UNKNOWN]))


@dataclass
class Attempt:
    pass_num: int
    strategy: str
    result: str            # recovered | failed | deferred
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"pass": self.pass_num, "strategy": self.strategy,
                "result": self.result, "detail": self.detail}


@dataclass
class DeferredItem:
    feature_id: str
    feature_type: str
    error_text: str
    error_class: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    recovered: bool = False

    def __post_init__(self):
        if not self.error_class:
            self.error_class = classify_failure(self.error_text)

    def next_strategy(self) -> Optional[str]:
        """The next un-tried strategy for this item, or None if exhausted."""
        used = {a.strategy for a in self.attempts}
        for s in retry_strategies(self.error_class):
            if s not in used:
                return s
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"feature_id": self.feature_id, "feature_type": self.feature_type,
                "error_class": self.error_class, "error_text": self.error_text[:400],
                "recovered": self.recovered,
                "attempts": [a.as_dict() for a in self.attempts]}


@dataclass
class DeferredQueue:
    items: list[DeferredItem] = field(default_factory=list)

    def add(self, feature_id: str, feature_type: str, error_text: str) -> DeferredItem:
        item = DeferredItem(feature_id, feature_type, error_text)
        self.items.append(item)
        log.warning("deferred %s (%s): %s [%s]", feature_id, feature_type,
                    error_text[:120], item.error_class)
        return item

    def open_items(self) -> list[DeferredItem]:
        return [i for i in self.items if not i.recovered]

    def write(self, part_dir: Path) -> Path:
        payload = {"total": len(self.items),
                   "recovered": sum(1 for i in self.items if i.recovered),
                   "open": len(self.open_items()),
                   "items": [i.as_dict() for i in self.items]}
        path = Path(part_dir) / DEFERRED_LOG
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path


def run_retry_passes(queue: DeferredQueue,
                     retry_one: Callable[[DeferredItem, str, dict], tuple[bool, str]],
                     topology_ctx: Optional[Callable[[], dict]] = None,
                     cap: int = DEFAULT_RETRY_CAP) -> DeferredQueue:
    """Retry deferred items after the rest of the part is built.

    ``retry_one(item, strategy, ctx) -> (recovered, detail)`` performs ONE
    strategy attempt (COM in production, mock in tests). ``topology_ctx()``
    returns the completed-solid inventory (faces/edges/bbox/tree) passed as
    ``ctx`` — the new information that makes iteration converge. Each pass tries
    the next un-tried strategy for each still-open item; an item with no strategy
    left (only 'clarify' remains) stops trying and goes to the clarification gate.
    Never retries the identical strategy twice."""
    for pass_num in range(2, 2 + max(0, cap)):
        open_items = queue.open_items()
        if not open_items:
            break
        ctx = {}
        if topology_ctx is not None:
            try:
                ctx = topology_ctx() or {}
            except Exception as e:
                log.warning("topology context capture failed: %s", e)
        progressed = False
        for item in open_items:
            strategy = item.next_strategy()
            if strategy is None or strategy == "clarify":
                continue  # exhausted -> stays open for the clarification gate
            try:
                recovered, detail = retry_one(item, strategy, ctx)
            except Exception as e:
                recovered, detail = False, f"retry raised: {type(e).__name__}: {e}"
            item.attempts.append(Attempt(pass_num, strategy,
                                         "recovered" if recovered else "failed", detail))
            if recovered:
                item.recovered = True
                progressed = True
                log.info("  recovered deferred %s on pass %d via %s", item.feature_id,
                         pass_num, strategy)
        if not progressed and all(i.next_strategy() in (None, "clarify")
                                  for i in queue.open_items()):
            break  # nothing left to try -> stop (deterministic, no thrash)
    return queue


def clarification_question(item: DeferredItem, part: str = "") -> dict[str, Any]:
    """The ready-to-answer gate question a still-open deferred item generates —
    shaped like a pipeline.human_assist Question dict so the assist queue can
    absorb it directly."""
    prompts = {
        SELECTION: f"Could not select the target face/edge for {item.feature_id}. Which "
                   "face or edge should it attach to (name a datum, e.g. REF_DATUM_A)?",
        SKETCH_DEF: f"{item.feature_id}'s sketch is over/under-defined. Which dimension "
                    "pins it (value + the datum it is measured from)?",
        GEOMETRY: f"{item.feature_id} produced invalid geometry (zero-thickness / no material). "
                  "Is its size/depth correct, or should it be suppressed?",
        MISSING_PARENT: f"{item.feature_id} depends on a parent feature that did not build. "
                        "Confirm the parent, or should this feature be suppressed?",
        COM_TIMEOUT: f"SolidWorks was unresponsive building {item.feature_id}. Re-run once "
                     "SolidWorks is stable — no drawing change needed.",
        PARAM_RANGE: f"A parameter for {item.feature_id} was rejected as out of range. "
                     "Confirm the intended value.",
        UNKNOWN: f"{item.feature_id} could not be built automatically after retries. "
                 "Please confirm its intent from the drawing.",
    }
    return {
        "question_id": f"Q-{part}-{item.feature_id}-deferred",
        "part": part, "feature_id": item.feature_id, "kind": "unclassifiable_callout",
        "question_text": prompts.get(item.error_class, prompts[UNKNOWN]),
        "candidates": [], "region_crop": "", "target_dimension_id": "",
        "automated_attempts": [f"{a.strategy}: {a.result}" for a in item.attempts],
        "default_if_unanswered": "ships without this feature (deferred_open, flagged)",
        "priority": 50.0, "status": "pending", "created_at": "", "answered_at": None,
        "answer": None,
    }
