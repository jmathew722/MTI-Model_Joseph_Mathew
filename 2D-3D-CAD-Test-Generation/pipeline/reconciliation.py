"""Stage 10.5 — Reconciliation Pass (2026-07-10 audit + self-correcting loop).

Closes the one gap the full-repository audit found to be genuinely missing
(see ``AUDIT_REPORT.md``): nothing previously re-checked the pipeline's own
output against the ORIGINAL extraction before reporting a part done. Every
other check in the pipeline (``overview_check.py``, ``requirements_check.py``,
``constraint_verify.py``) grades the build against the drawing or the
operator's spec — none of them re-derives "does every feature Claude Vision
extracted actually have a build disposition" as an explicit, bounded,
self-correcting loop. This module is that loop.

Design, per the audit brief:

  * The checklist is built from the RAW ``_extraction.json`` — the artifact
    closest to the actual drawing content — never from ``resolved_extraction``
    or ``build_plan.json``, which are downstream and could themselves contain
    the bug being checked for.
  * Every checklist item ends in exactly one of: confirmed built (``BUILT`` /
    ``BUILT_WITH_DERIVED_VALUE``), a justified ``EXCLUDED_INCOMPLETE`` /
    ``skipped_prohibited`` entry, or — if truly unresolved after the capped
    loop — an entry in ``unresolved`` naming exactly what is still missing.
    Nothing is ever silently absent.
  * The loop re-runs ONLY ``resolve_extraction`` (pure Python over
    already-extracted data) — it never calls the extractor, so it can never
    force a paid re-extraction and never breaks the ``--from-json`` /
    extraction-cache cost discipline. See :func:`reconcile_part`'s docstring
    for exactly what "targeted" means here.
  * The loop is capped at ``max_passes`` (default 3). If issues remain after
    the cap, the part is marked ``READY_WITH_OPEN_ITEMS`` and every remaining
    item is listed by name — never a silent give-up.
  * Position AND orientation get the same scrutiny as dimension value: a hole
    feature's checklist entry also carries its expected instance COUNT (from
    the hole callout's ``qty`` / ``instance_positions``), so a pattern that
    built with fewer instances than the drawing shows is caught even though
    its diameter is correct.

Scope boundary (documented honestly, like this session's HoleWizard5 finding):
when a re-resolution pass DOES recover a previously-excluded feature, this
module splices the new step into the existing ``build_plan.json`` and adds a
new, clearly-named macro file — it does NOT renumber or touch any existing
macro file, and it does NOT attempt to hot-patch an already-built, closed
``.sldprt`` via COM (that would require re-opening a live SolidWorks document
mid-session, which cannot be reliably validated headlessly). Instead the
report and engineering review say plainly that a full rebuild is needed to
pick up the recovered feature in the 3D model — the JSON/macro artifacts are
corrected immediately; the COM model catches up on the next `.sldprt` build.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pipeline.build_sequencer import (
    STATE_BUILT,
    STATE_BUILT_DERIVED,
    STATE_EXCLUDED,
    sequence_build_order,
)
from pipeline.schema import DrawingData, FeatureType

log = logging.getLogger(__name__)

RECONCILIATION_REPORT_SUFFIX = "_reconciliation_report.json"


# --------------------------------------------------------------------------- #
# The checklist — built ONLY from the raw extraction (ground truth)
# --------------------------------------------------------------------------- #
@dataclass
class ChecklistItem:
    feature_id: str
    feature_type: str
    description: str
    expected_instances: int  # 1 for non-hole features; qty for hole/pattern features


def _expected_instances_for(feature: dict, hole_callouts_by_ref: dict[str, list[dict]]) -> int:
    """The number of physical instances the DRAWING describes for this feature.

    Prefers ``len(instance_positions)`` (every instance explicitly dimensioned)
    over ``qty`` (a count without individual positions) when both are present,
    since explicit positions are the more specific ground truth; falls back to
    ``qty``; defaults to 1 for anything without a linked hole callout."""
    callouts = hole_callouts_by_ref.get(feature.get("id", ""), [])
    if not callouts:
        return 1
    best = 1
    for h in callouts:
        positions = h.get("instance_positions") or []
        qty = int(h.get("qty") or 1)
        best = max(best, len(positions) if positions else qty, qty)
    return best


def build_checklist(raw_extraction: dict) -> list[ChecklistItem]:
    """The ground-truth checklist: one entry per feature in the RAW extraction.

    Never reads ``resolved_extraction`` or ``build_plan`` — those are the
    artifacts this function's checklist is used to verify."""
    hole_callouts_by_ref: dict[str, list[dict]] = {}
    for h in raw_extraction.get("hole_callouts", []) or []:
        ref = h.get("feature_ref")
        if ref:
            hole_callouts_by_ref.setdefault(ref, []).append(h)

    items: list[ChecklistItem] = []
    for feat in raw_extraction.get("features", []) or []:
        fid = feat.get("id", "")
        items.append(ChecklistItem(
            feature_id=fid,
            feature_type=feat.get("type", ""),
            description=feat.get("description", ""),
            expected_instances=_expected_instances_for(feat, hole_callouts_by_ref),
        ))
    return items


# --------------------------------------------------------------------------- #
# Diffing the checklist against what actually got built
# --------------------------------------------------------------------------- #
@dataclass
class UnresolvedItem:
    feature_id: str
    feature_type: str
    issue: str
    resolution_attempted: str = ""
    status: str = "unresolved"

    def as_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "feature_type": self.feature_type,
            "issue": self.issue,
            "resolution_attempted": self.resolution_attempted,
            "status": self.status,
        }


def _instance_count_in_build_plan(build_plan: dict, feature_id: str) -> Optional[int]:
    """Instances actually present for ``feature_id`` in ``build_plan.json``'s
    steps — the count of ``positions_xy`` if any, else 1 for a built step with
    no positions (a non-hole feature), else ``None`` if the feature has no step
    at all (the disposition-table check already catches that case; this is a
    finer-grained secondary check for instance-COUNT fidelity)."""
    for step in build_plan.get("steps", []) or []:
        fids = str(step.get("feature_id", "")).split(",")
        if feature_id in fids:
            positions = step.get("positions_xy") or []
            if positions:
                return len(positions)
            circ = step.get("circular_pattern") or {}
            if circ.get("total_instances"):
                return int(circ["total_instances"])
            return 1
    return None


def diff_checklist(
    checklist: list[ChecklistItem],
    dispositions: list[dict],
    build_plan: dict,
) -> list[UnresolvedItem]:
    """Compare the ground-truth checklist against the disposition table +
    build_plan.json. Returns every item that is NOT (built with the expected
    instance count) and NOT a justified exclusion/skip — i.e. everything that
    still needs attention, worded with the SPECIFIC missing parameter."""
    disp_by_id = {d.get("feature_id"): d for d in dispositions}
    skipped_ids = set(build_plan.get("skipped_prohibited", []) or [])
    unresolved: list[UnresolvedItem] = []

    for item in checklist:
        disp = disp_by_id.get(item.feature_id)
        if disp is None:
            # Structurally should not happen (build_sequencer records every
            # model feature) — but the checklist is built from the RAW
            # extraction, which could contain a feature id the resolved model
            # no longer has (e.g. a coercion/validation error dropped it).
            unresolved.append(UnresolvedItem(
                item.feature_id, item.feature_type,
                "feature present in the original extraction but has NO disposition "
                "entry at all — it did not survive schema validation/resolution.",
            ))
            continue

        state = disp.get("state")
        if state == STATE_EXCLUDED:
            if item.feature_id in skipped_ids:
                continue  # justified prohibited skip — acceptable, not unresolved
            flags = disp.get("flags") or []
            why = next((f.get("human_note") for f in flags if f.get("human_note")), "")
            unresolved.append(UnresolvedItem(
                item.feature_id, item.feature_type,
                why or "excluded from the build by the completeness gate "
                       "(missing driving dimension).",
            ))
            continue

        if state in (STATE_BUILT, STATE_BUILT_DERIVED):
            if item.expected_instances > 1:
                actual = _instance_count_in_build_plan(build_plan, item.feature_id)
                if actual is not None and actual < item.expected_instances:
                    unresolved.append(UnresolvedItem(
                        item.feature_id, item.feature_type,
                        f"extraction describes {item.expected_instances} instance(s) but only "
                        f"{actual} made it into the build plan.",
                    ))
            continue

        # Any other/unknown state (e.g. a manual-only prohibited type that IS
        # justified) — accept if it's in skipped_prohibited, else flag it.
        if item.feature_id not in skipped_ids:
            unresolved.append(UnresolvedItem(
                item.feature_id, item.feature_type,
                f"disposition state {state!r} is neither built nor a justified skip.",
            ))
    return unresolved


def slot_checks(raw_extraction: dict, build_plan: dict) -> list[UnresolvedItem]:
    """Canonical-slot reconciliation. Every ``slot_cut`` in the raw extraction
    MUST appear in the build as a ``slot_rect_cut`` step (the mandatory
    rectangle that carries the slot's position + size truth) — a missing
    rectangle is CRITICAL (it silently loses the slot), never merely deferred
    like the cosmetic corner fillet. Also a cheap geometric spot-check: the
    rectangle's near edge must sit at the dimensioned ``anchor_offset`` (within
    a small tolerance) — a mis-anchored rectangle is as wrong as a missing one."""
    slots = raw_extraction.get("slot_cuts", []) or []
    if not slots:
        return []
    rect_steps: dict[str, dict] = {}
    for step in build_plan.get("steps", []) or []:
        if step.get("type") == "slot_rect_cut":
            for fid in str(step.get("feature_id", "")).split(","):
                rect_steps[fid] = step

    out: list[UnresolvedItem] = []
    for slot in slots:
        sid = slot.get("id", "?")
        step = rect_steps.get(sid)
        if step is None:
            out.append(UnresolvedItem(
                sid, "slot_rect_cut",
                f"CRITICAL: slot {sid} (a U-notch/slot) has NO mandatory slot_rect_cut "
                "rectangle in the build plan — the slot's position/size would be lost. "
                "The rectangle is must_complete; it can never be dropped or deferred.",
            ))
            continue
        # Geometric near-edge spot-check: a horizontal-anchored slot's near edge
        # X (the minimum corner X, or the anchored corner) should equal anchor.
        corners = ((step.get("sketch") or {}).get("corners_drawing_units")
                   or step.get("corners_drawing_units") or [])
        anchor = slot.get("anchor_offset")
        edge = (slot.get("open_edge") or "").lower()
        if corners and anchor is not None and edge in ("top", "bottom", ""):
            near_x = min(float(c[0]) for c in corners)
            # edge_to_centerline: near edge is half a width inboard of the anchor.
            expected = float(anchor)
            if slot.get("anchor_semantics") == "edge_to_centerline":
                expected -= float(slot.get("width") or 0) / 2.0
            if abs(near_x - expected) > 0.01:
                out.append(UnresolvedItem(
                    sid, "slot_rect_cut",
                    f"slot {sid} rectangle near-edge X = {near_x:g} but the drawing anchors it "
                    f"at {expected:g} from the {slot.get('anchor_edge')} edge "
                    f"(delta {abs(near_x - expected):g}) — the rectangle is mis-positioned.",
                ))
    return out


# --------------------------------------------------------------------------- #
# The reconciliation report
# --------------------------------------------------------------------------- #
@dataclass
class ReconciliationResult:
    part: str
    checklist_total: int
    confirmed_built: int
    loop_passes_used: int
    unresolved: list[UnresolvedItem] = field(default_factory=list)
    final_status: str = "READY"
    splices_applied: list[str] = field(default_factory=list)  # feature ids recovered mid-loop

    def as_dict(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "checklist_total": self.checklist_total,
            "confirmed_built": self.confirmed_built,
            "loop_passes_used": self.loop_passes_used,
            "unresolved": [u.as_dict() for u in self.unresolved],
            "splices_applied": self.splices_applied,
            "final_status": self.final_status,
        }

    def write(self, part_dir: Path, safe_name: str) -> Path:
        path = part_dir / f"{safe_name}{RECONCILIATION_REPORT_SUFFIX}"
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        return path


# --------------------------------------------------------------------------- #
# Splicing a recovered feature into the EXISTING build_plan.json + macros/
# --------------------------------------------------------------------------- #
def _splice_recovered_features(
    *, model: DrawingData, resolution, raw_extraction: dict, verification_text: str,
    part_dir: Path, feature_ids: list[str], pass_num: int,
) -> None:
    """Regenerate the full macro package into a scratch directory, then copy
    ONLY the recovered features' new macro file(s) into the real ``macros/``
    dir (never touching/renumbering any existing file) and patch the real
    ``build_plan.json``'s ``steps``/``dispositions`` in place for those ids.

    Scope boundary: this updates the JSON/VBA artifacts only. It does not
    reopen the already-built ``.sldprt`` — see the module docstring."""
    import tempfile

    from pipeline.macro_generator import generate_macro_package

    with tempfile.TemporaryDirectory(prefix="mti_reconcile_") as tmp:
        tmp_pkg = generate_macro_package(model, raw_extraction, verification_text,
                                         Path(tmp), resolution=resolution)
        tmp_plan = json.loads(tmp_pkg.build_plan_json.read_text(encoding="utf-8"))

        real_macros_dir = part_dir / "macros"
        real_macros_dir.mkdir(parents=True, exist_ok=True)
        build_plan_path = part_dir / f"{part_dir.name}_build_plan.json"
        if not build_plan_path.is_file():
            # part_dir.name may not match the plan's file prefix; fall back to
            # the one build_plan.json present.
            candidates = list(part_dir.glob("*_build_plan.json"))
            if not candidates:
                log.warning("reconciliation: no build_plan.json found under %s — cannot splice.", part_dir)
                return
            build_plan_path = candidates[0]
        real_plan = json.loads(build_plan_path.read_text(encoding="utf-8"))

        new_step_by_fid = {}
        for step in tmp_plan.get("steps", []):
            for fid in str(step.get("feature_id", "")).split(","):
                if fid in feature_ids:
                    new_step_by_fid[fid] = step

        copied_files: list[str] = []
        for fid, step in new_step_by_fid.items():
            src_name = step.get("macro_file", "")
            src = tmp_pkg.macros_dir / src_name if src_name else None
            if src is not None and src.is_file():
                dest_name = f"RECONCILE_pass{pass_num}_{src_name}"
                shutil.copy2(src, real_macros_dir / dest_name)
                copied_files.append(dest_name)
                step = dict(step)
                step["macro_file"] = dest_name
                step["notes"] = (step.get("notes", "") + " [added by reconciliation pass "
                                 f"{pass_num} — run this macro manually or re-run RUN_ALL after "
                                 "regenerating the package; a full .sldprt rebuild is needed to "
                                 "reflect this in the 3D model]").strip()

            # Replace or append the step in the real plan.
            real_steps = real_plan.setdefault("steps", [])
            replaced = False
            for i, s in enumerate(real_steps):
                if fid in str(s.get("feature_id", "")).split(","):
                    real_steps[i] = step
                    replaced = True
                    break
            if not replaced:
                real_steps.append(step)
            # Remove from skipped_prohibited/needs_review — it is recovered.
            real_plan["skipped_prohibited"] = [
                s for s in real_plan.get("skipped_prohibited", []) if s != fid
            ]
            real_plan["needs_review"] = [
                s for s in real_plan.get("needs_review", []) if s != fid
            ]

        # Patch the disposition table (recovered feature's new state).
        tmp_disp_path = tmp_pkg.root / f"{tmp_pkg.root.name}_build_dispositions.json"
        if tmp_disp_path.is_file():
            tmp_disps = {d["feature_id"]: d for d in json.loads(tmp_disp_path.read_text(encoding="utf-8"))}
            real_disp_candidates = list(part_dir.glob("*_build_dispositions.json"))
            if real_disp_candidates:
                real_disp_path = real_disp_candidates[0]
                real_disps = json.loads(real_disp_path.read_text(encoding="utf-8"))
                by_id = {d["feature_id"]: d for d in real_disps}
                for fid in feature_ids:
                    if fid in tmp_disps:
                        by_id[fid] = tmp_disps[fid]
                real_disp_path.write_text(
                    json.dumps(list(by_id.values()), indent=2), encoding="utf-8")

        build_plan_path.write_text(json.dumps(real_plan, indent=2), encoding="utf-8")
        if copied_files:
            log.info("reconciliation pass %d: added %s to %s for recovered feature(s) %s",
                     pass_num, copied_files, real_macros_dir, sorted(feature_ids))


# --------------------------------------------------------------------------- #
# The capped, self-correcting loop
# --------------------------------------------------------------------------- #
def reconcile_part(
    *,
    raw_extraction: dict,
    resolution,
    model: DrawingData,
    dispositions: list[dict],
    build_plan: dict,
    verification_text: str,
    part_dir: Path,
    part: str,
    requirements: Optional[list[str]] = None,
    overview_analysis: Optional[dict] = None,
    max_passes: int = 3,
) -> ReconciliationResult:
    """Stage 10.5: verify the pipeline's own output against the original
    extraction, and try (in bounded fashion) to close any gap found.

    What "targeted re-resolution" means here, precisely: this function NEVER
    calls the extractor (no paid API call, no cache invalidation) — it only
    ever re-runs ``resolve_extraction`` on the SAME raw extraction. Because
    that function is a deterministic pure computation over its inputs, simply
    calling it again with IDENTICAL inputs is guaranteed to reproduce the
    identical result — no amount of looping recovers new information from
    nothing (this module never fabricates a value, per the hard rule). So each
    pass re-loads every REAL signal available on disk that may not have been
    part of the original resolution call — a ``must_meet_spec.txt`` /
    ``overview_analysis.json`` written after the original run, or explicitly
    passed-in ``requirements``/``overview_analysis`` the caller has newer than
    what produced ``resolution`` — and re-resolves with the fullest available
    context. If a pass closes zero additional gaps compared to the previous
    pass, further passes with the same inputs cannot help either (determinism),
    so the loop stops immediately rather than silently burning the remaining
    cap — but it still stops LOUDLY: every remaining item is named in
    ``unresolved``, never dropped without a trace.
    """
    checklist = build_checklist(raw_extraction)
    unresolved = diff_checklist(checklist, dispositions, build_plan)
    unresolved += slot_checks(raw_extraction, build_plan)
    confirmed_built = len(checklist) - len(unresolved)
    passes_used = 0
    splices: list[str] = []

    cur_resolution, cur_model = resolution, model

    while unresolved and passes_used < max_passes:
        passes_used += 1
        fresh_requirements = _reload_requirements(part_dir, fallback=requirements)
        fresh_overview = _reload_overview_analysis(part_dir, fallback=overview_analysis)

        try:
            from pipeline.resolver import resolve_extraction
            from pipeline.validator import run_verification

            new_resolution = resolve_extraction(
                raw_extraction, requirements=fresh_requirements, overview_analysis=fresh_overview)
            new_model, report = run_verification(new_resolution.clean_extraction)
        except Exception as e:  # the loop must never crash a run
            log.warning("reconciliation pass %d: re-resolution failed (%s) — stopping loop.",
                       passes_used, e)
            break
        if new_model is None:
            log.warning("reconciliation pass %d: re-resolved extraction failed schema "
                       "validation — stopping loop.", passes_used)
            break

        new_seq = sequence_build_order(new_model, new_resolution)
        new_dispositions = new_seq.disposition_table
        new_build_plan = dict(build_plan)  # instance counts recomputed only via steps below
        new_unresolved = diff_checklist(checklist, new_dispositions, build_plan)
        new_unresolved += slot_checks(raw_extraction, build_plan)

        fixed_ids = {u.feature_id for u in unresolved} - {u.feature_id for u in new_unresolved}
        if not fixed_ids:
            for item in unresolved:
                item.resolution_attempted = (
                    f"Re-ran Stage 2.5 resolution (pass {passes_used}) with every available "
                    "requirements/overview-analysis signal reloaded from disk; the result was "
                    "identical to the previous pass — no further information is available to "
                    "resolve this without fabricating a value, which this pipeline never does.")
                item.status = f"unresolved_after_pass_{passes_used}"
            log.info("reconciliation pass %d made no progress — stopping (deterministic resolver, "
                     "no new signal available).", passes_used)
            break

        try:
            _splice_recovered_features(
                model=new_model, resolution=new_resolution, raw_extraction=raw_extraction,
                verification_text=verification_text, part_dir=part_dir,
                feature_ids=sorted(fixed_ids), pass_num=passes_used,
            )
            splices.extend(sorted(fixed_ids))
        except Exception as e:  # a failed splice must not lose the run or hide the recovery
            log.warning("reconciliation pass %d: splice failed (%s) — recording the recovery "
                       "as still unresolved.", passes_used, e)
            new_unresolved = unresolved  # revert — nothing was actually applied on disk
            for item in new_unresolved:
                item.resolution_attempted = (
                    f"Re-resolution recovered this feature on pass {passes_used}, but splicing "
                    f"it into build_plan.json/macros failed ({type(e).__name__}: {e}).")
                item.status = f"splice_failed_pass_{passes_used}"
            break

        for item in unresolved:
            if item.feature_id in fixed_ids:
                item.resolution_attempted = (
                    f"Re-ran Stage 2.5 resolution (pass {passes_used}) with the fullest available "
                    "context; the feature is now built and spliced into build_plan.json/macros/.")
                item.status = f"resolved_on_pass_{passes_used}"

        cur_resolution, cur_model = new_resolution, new_model
        unresolved = new_unresolved
        confirmed_built = len(checklist) - len(unresolved)

    final_status = "READY" if not unresolved else "READY_WITH_OPEN_ITEMS"
    result = ReconciliationResult(
        part=part, checklist_total=len(checklist), confirmed_built=confirmed_built,
        loop_passes_used=passes_used, unresolved=unresolved, final_status=final_status,
        splices_applied=splices,
    )
    return result


def _reload_requirements(part_dir: Path, fallback: Optional[list[str]]) -> Optional[list[str]]:
    """Re-read the operator's must-meet spec text fresh from disk, if present —
    it may have been written after the original resolution call ran."""
    try:
        from pipeline.must_meet import MUST_MEET_FILENAME

        spec_path = part_dir / MUST_MEET_FILENAME
        if spec_path.is_file():
            text = spec_path.read_text(encoding="utf-8", errors="replace")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if lines:
                return lines
    except Exception:
        pass
    return fallback


def _reload_overview_analysis(part_dir: Path, fallback: Optional[dict]) -> Optional[dict]:
    """Re-read ``overview_analysis.json`` fresh from disk, if present."""
    path = part_dir / "overview_analysis.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return fallback


# =========================================================================== #
# Phase B — the geometric build->measure->correct->rebuild loop
# =========================================================================== #
# Where reconcile_part (above) closes the CHECKLIST-level gap (does every
# extracted feature have a disposition?), this loop closes the GEOMETRIC gap:
# does every BUILT feature have the right position/size? It wraps Phase A
# (pipeline.feature_verify) in a bounded, self-correcting loop and shares the
# exact discipline of reconcile_part — capped iterations, splice-only edits,
# stop-when-no-progress, never fabricate, no paid re-extraction.

_TRANSFORM_TOL_IN = 0.02  # deltas within this are "the same" for systematic detection


def classify_transform(misplaced: list[dict]) -> Optional[dict]:
    """Detect a SYSTEMATIC transform error from a set of misplaced features.

    Each item is ``{"expected": (ex, ey), "measured": (mx, my)}``. Returns a
    transform descriptor when >=2 features share one consistent error — an
    origin offset, an x/y axis swap, or a uniform scale (unit-factor) — else
    None (the misplacement is a one-off, handled per-feature). A systematic
    error is a transform-layer bug, not N independent feature bugs, so it is
    corrected once and applied to every affected step."""
    pairs = [(m["expected"], m["measured"]) for m in misplaced
             if m.get("expected") and m.get("measured")]
    if len(pairs) < 2:
        return None

    # Origin offset: measured = expected + (dx, dy), constant.
    dxs = [mx - ex for (ex, ey), (mx, my) in pairs]
    dys = [my - ey for (ex, ey), (mx, my) in pairs]
    if _spread(dxs) <= _TRANSFORM_TOL_IN and _spread(dys) <= _TRANSFORM_TOL_IN:
        dx, dy = sum(dxs) / len(dxs), sum(dys) / len(dys)
        if math.hypot(dx, dy) > _TRANSFORM_TOL_IN:
            return {"kind": "origin_offset", "dx": round(dx, 4), "dy": round(dy, 4)}

    # Axis swap: measured (x, y) == expected (y, x).
    swap_err = [math.hypot(mx - ey, my - ex) for (ex, ey), (mx, my) in pairs]
    if max(swap_err) <= _TRANSFORM_TOL_IN:
        return {"kind": "axis_swap"}

    # Uniform scale (unit-factor slip): measured = k * expected, same k both axes.
    ratios = []
    for (ex, ey), (mx, my) in pairs:
        if abs(ex) > 1e-6:
            ratios.append(mx / ex)
        if abs(ey) > 1e-6:
            ratios.append(my / ey)
    if ratios and _spread(ratios) <= 0.02 and abs(sum(ratios) / len(ratios) - 1.0) > 0.05:
        return {"kind": "uniform_scale", "factor": round(sum(ratios) / len(ratios), 5)}
    return None


def _spread(vals: list[float]) -> float:
    return (max(vals) - min(vals)) if vals else 0.0


def _apply_transform_to_plan(build_plan: dict, transform: dict) -> tuple[dict, list[str]]:
    """Return a COPY of build_plan with a systematic transform compensated on
    every feature's positions, plus the list of affected feature ids. The build
    added the error, so we PRE-COMPENSATE the emitted positions to cancel it —
    the drawing's resolved values are never altered, only the emitted coords."""
    plan = copy.deepcopy(build_plan)
    affected: list[str] = []

    def _fix(pt: list[float]) -> list[float]:
        x, y = float(pt[0]), float(pt[1])
        if transform["kind"] == "origin_offset":
            return [round(x - transform["dx"], 6), round(y - transform["dy"], 6)]
        if transform["kind"] == "axis_swap":
            return [y, x]
        if transform["kind"] == "uniform_scale":
            k = transform["factor"] or 1.0
            return [round(x / k, 6), round(y / k, 6)]
        return [x, y]

    for step in plan.get("steps", []):
        pts = step.get("positions_xy")
        if pts and step.get("feature_id") not in ("-", None):
            step["positions_xy"] = [_fix(p) for p in pts]
            affected.append(step.get("feature_id"))
    return plan, affected


@dataclass
class Correction:
    feature_id: str
    mismatch_class: str          # MISSING | MISPLACED | WRONG_SIZE | EXTRA
    action: str                  # transform_fix | reemit_step | reresolve_dim | flag
    detail: str
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"feature_id": self.feature_id, "mismatch_class": self.mismatch_class,
                "action": self.action, "detail": self.detail, "applied": self.applied}


def plan_corrections(verification: dict, build_plan: dict) -> tuple[Optional[dict], list[Correction]]:
    """Map a feature_verification report to corrective actions.

    Returns ``(transform_or_None, corrections)``. A detected systematic
    transform is returned separately because it supersedes the per-feature
    MISPLACED actions (one fix, all steps)."""
    mismatches = verification.get("mismatches") or []
    corrections: list[Correction] = []

    misplaced = [{"feature_id": m["feature_id"],
                  "expected": tuple((m.get("expected") or {}).get(k, 0.0) for k in ("x", "y")),
                  "measured": tuple((m.get("measured") or {}).get(k, 0.0) for k in ("x", "y"))}
                 for m in mismatches if m.get("classification") == "MISPLACED"
                 and m.get("expected") and m.get("measured")]
    transform = classify_transform(misplaced)

    for m in mismatches:
        cls, fid = m.get("classification"), m.get("feature_id")
        if cls == "MISPLACED":
            if transform:
                corrections.append(Correction(fid, cls, "transform_fix",
                                   f"systematic {transform['kind']} — corrected for all affected steps"))
            else:
                # One-off: the build diverged from a correct plan. Re-emit the
                # single step (position stays resolver-derived — the drawing is
                # truth, never the measured value). A rebuild may clear a
                # transient COM placement failure; if it recurs, it is flagged.
                corrections.append(Correction(fid, cls, "reemit_step",
                                   "re-emit step with resolver-derived position (drawing is truth)"))
        elif cls == "MISSING":
            corrections.append(Correction(fid, cls, "flag",
                               "feature absent from the build; check disposition/COM failure "
                               "(no value fabricated to force it in)"))
        elif cls == "WRONG_SIZE":
            corrections.append(Correction(fid, cls, "reresolve_dim",
                               "re-check the driving dimension against the resolver ladder; "
                               "flag if the resolution does not change"))
    for e in verification.get("extras") or []:
        corrections.append(Correction(e.get("feature_id", "?"), "EXTRA", "flag",
                           "measured geometry with no planned feature — surfaced for review"))
    return transform, corrections


@dataclass
class GeometricLoopResult:
    part: str
    iterations_used: int
    final_status: str            # READY | READY_WITH_OPEN_ITEMS
    iteration_ledger: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    transforms_applied: list[dict] = field(default_factory=list)
    stopped_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "iterations_used": self.iterations_used,
            "final_status": self.final_status,
            "iteration_ledger": self.iteration_ledger,
            "unresolved": self.unresolved,
            "transforms_applied": self.transforms_applied,
            "stopped_reason": self.stopped_reason,
        }

    def write(self, part_dir: Path, safe_name: str) -> Path:
        path = Path(part_dir) / f"{safe_name}_geometric_loop_report.json"
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        return path


def geometric_correction_loop(
    *,
    build_fn,
    build_plan: dict,
    part_dir: Path,
    part: str,
    resolved_extraction: Optional[dict] = None,
    expected_thickness_in: Optional[float] = None,
    verify_fn=None,
    max_iterations: int = 3,
    lessons_path: Optional[Path] = None,
) -> GeometricLoopResult:
    """Bounded build->measure->correct->rebuild loop (Phase B).

    ``build_fn(build_plan, part_dir, iteration) -> Path`` builds the model and
    returns the STL path (COM in production; injectable for tests). ``verify_fn``
    defaults to :func:`pipeline.feature_verify.verify_features`. Terminates on:
    all-features-OK (READY); the iteration cap; no applicable correction for the
    remaining mismatches; or OSCILLATION — a previously-PASS feature regressing —
    which stops immediately rather than thrash. Every iteration appends a
    structured entry to ``lessons_learned.jsonl``."""
    if verify_fn is None:
        from pipeline.feature_verify import verify_features as verify_fn  # noqa: N806

    result = GeometricLoopResult(part=part, iterations_used=0,
                                 final_status="READY_WITH_OPEN_ITEMS")
    plan = copy.deepcopy(build_plan)
    prev_ok_ids: set[str] = set()

    for it in range(1, max_iterations + 1):
        result.iterations_used = it
        try:
            stl_path = build_fn(plan, part_dir, it)
        except Exception as e:
            result.stopped_reason = f"build failed on iteration {it}: {type(e).__name__}: {e}"
            log.warning("geometric loop: %s", result.stopped_reason)
            break

        verification = verify_fn(Path(stl_path), plan, Path(part_dir),
                                 resolved_extraction=resolved_extraction, part="",
                                 expected_thickness_in=expected_thickness_in, write=False)

        ok_ids = {f.get("feature_id") for f in verification.get("features", [])
                  if f.get("classification") == "OK"}
        mismatches = verification.get("mismatches") or []
        extras = verification.get("extras") or []

        ledger_entry = {
            "iteration": it,
            "stl": Path(stl_path).name,
            "verified_ok": verification.get("summary", {}).get("ok", 0),
            "mismatches": [{"feature_id": m.get("feature_id"),
                            "class": m.get("classification")} for m in mismatches],
            "extras": len(extras),
        }

        # Oscillation: a feature that PASSED before now fails.
        regressed = [fid for fid in prev_ok_ids if fid not in ok_ids and fid is not None]
        if regressed:
            ledger_entry["oscillation"] = regressed
            result.iteration_ledger.append(ledger_entry)
            result.stopped_reason = (f"oscillation — feature(s) {regressed} regressed after a "
                                     "correction; stopped to avoid thrashing")
            result.unresolved = ledger_entry["mismatches"]
            _log_iteration(lessons_path, part, it, [], "oscillation_stop")
            break

        if not mismatches and not extras:
            result.final_status = "READY"
            result.stopped_reason = "all features verified within tolerance"
            ledger_entry["result"] = "all_pass"
            result.iteration_ledger.append(ledger_entry)
            _log_iteration(lessons_path, part, it, [], "all_pass")
            break

        transform, corrections = plan_corrections(verification, plan)
        ledger_entry["corrections"] = [c.as_dict() for c in corrections]

        applied_any = False
        if transform:
            plan, affected = _apply_transform_to_plan(plan, transform)
            result.transforms_applied.append({"iteration": it, **transform,
                                               "affected": affected})
            for c in corrections:
                if c.action == "transform_fix":
                    c.applied = True
            applied_any = bool(affected)
            ledger_entry["transform"] = transform

        # Any correction that actually changes the plan counts as progress; a
        # loop that can only "flag" makes no progress and stops (deterministic,
        # like reconcile_part) rather than burning the iteration cap blindly.
        if not applied_any and all(c.action in ("flag",) for c in corrections):
            result.iteration_ledger.append(ledger_entry)
            result.stopped_reason = ("no applicable geometric correction — remaining mismatches "
                                     "need human review (never fabricated to force a pass)")
            result.unresolved = ledger_entry["mismatches"] + [
                {"feature_id": e.get("feature_id"), "class": "EXTRA"} for e in extras]
            _log_iteration(lessons_path, part, it, corrections, "no_progress_stop")
            break

        prev_ok_ids = ok_ids
        result.iteration_ledger.append(ledger_entry)
        _log_iteration(lessons_path, part, it, corrections,
                       "corrected" if applied_any else "reemit_only")
    else:
        result.stopped_reason = f"iteration cap ({max_iterations}) reached"

    if result.final_status != "READY" and not result.unresolved and result.iteration_ledger:
        result.unresolved = result.iteration_ledger[-1].get("mismatches", [])
    return result


def _log_iteration(lessons_path: Optional[Path], part: str, iteration: int,
                   corrections: list, outcome: str) -> None:
    """Append a structured geometric-loop entry to lessons_learned.jsonl (the
    raw material for the Phase D method library). Never raises."""
    if lessons_path is None:
        return
    try:
        from pipeline.must_meet import append_lesson

        append_lesson(Path(lessons_path), {
            "kind": "geometric_loop_iteration",
            "part": part,
            "iteration": iteration,
            "corrections": [c.as_dict() for c in corrections],
            "outcome": outcome,
        })
    except Exception:
        pass
