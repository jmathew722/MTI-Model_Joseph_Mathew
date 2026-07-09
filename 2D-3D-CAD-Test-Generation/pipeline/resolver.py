"""Stage 2.5 — Ambiguity Resolver (the "chief design engineer" pass).

Philosophy (carried from the project owner's directive):

    A complete approximate model is always the correct outcome.
    An incomplete model is always the wrong outcome.

Where :mod:`pipeline.validator` would *block* on an ambiguous or under-dimensioned
drawing, this stage makes the best defensible engineering decision from the data
in front of it, records exactly what it assumed, and lets the build proceed. The
human verifies the annotated assumptions in SolidWorks afterwards.

Hard guarantees this module enforces (the "prime directive"):

  * **Every dimension ends with a numeric** ``resolved_value`` — never null, never
    a string, never absent. There is no exit path that produces a non-number.
  * **Every feature gets** ``build_status == "build"``. No "skip"/"defer"/"omit".
  * Every assumption carries ``assumption_basis``, ``assumption_confidence``,
    ``flag_tier`` (HIGH/MEDIUM/LOW/CRITICAL) and an actionable, ID-naming
    ``human_note``.

This is implemented **deterministically** (not via a second LLM call): the
resolution algorithm in the spec is a fully specified decision tree with exact
numeric thresholds, and for a CAD pipeline a reproducible, testable, value-by-rule
resolver is far safer than free-form generation that could invent a wrong number.
Candidate values come from what Claude already extracted (``possible_values``,
dimension chains, adjacent dimensions) — numbers are chosen, never fabricated.

Public entry point: :func:`resolve_extraction`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from pipeline.schema import (
    DrawingData,
    canonicalize_applies_to,
    is_envelope_label,
)
from utils.logger import get_logger

log = get_logger()

# Flag tiers, ordered most-severe-last so ``max`` by index gives the worst tier.
FLAG_TIERS = ("HIGH", "MEDIUM", "LOW", "CRITICAL")
_TIER_RANK = {t: i for i, t in enumerate(FLAG_TIERS)}

# Confidence band per tier (used when a step does not get a more specific value).
_TIER_CONFIDENCE = {
    "HIGH": 0.95,
    "MEDIUM": 0.72,
    "LOW": 0.50,
    "CRITICAL": 0.30,
}

# Geometric-validity constants (inches; the spec is written in inch terms — values
# are compared in DRAWING units, which for these parts are inches).
MIN_WALL = 0.010          # minimum wall / edge clearance margin
MIN_HOLE_EDGE_CLEARANCE = 0.010  # extra clearance beyond radius from an edge

# A base extrude_boss that the drawing never dimensioned for thickness still has to
# become a solid (an empty .sldprt is the worst outcome). When no thickness can be
# read, Stage 2.5 synthesizes one — at least this nominal, and always thicker than
# the deepest known sub-feature cut so those cuts stay blind. Flagged CRITICAL.
NOMINAL_THICKNESS = {"inch": 0.5, "mm": 12.0, "cm": 1.2}
_DEPTH_TOKENS = ("depth", "thickness", "height")


def worst_tier(*tiers: str) -> str:
    """Return the most severe (closest-to-CRITICAL) of the given tiers."""
    valid = [t for t in tiers if t in _TIER_RANK]
    if not valid:
        return "HIGH"
    return max(valid, key=lambda t: _TIER_RANK[t])


# Which PRIORITY TIER resolved a value/flag (recorded on every resolution and
# flag for traceability — "why was this value chosen"):
#   tier0_spec       — operator must-meet specification (human-authoritative)
#   tier1_per_view   — per-view extraction (most precise on individual dimensions)
#   tier2_overview   — Stage 1.5 holistic overview analysis (authoritative on
#                      cross-view relationships / symmetry / through-vs-blind)
TIER_SPEC = "tier0_spec"
TIER_PER_VIEW = "tier1_per_view"
TIER_OVERVIEW = "tier2_overview"


def tier_for_basis(assumption_basis: str) -> str:
    """The priority tier that produced a resolution, from its basis keyword."""
    if assumption_basis == "spec_driven":
        return TIER_SPEC
    if assumption_basis.startswith("overview"):
        return TIER_OVERVIEW
    return TIER_PER_VIEW


@dataclass
class DimResolution:
    """The resolution record added to one dimension object."""

    dimension_id: str
    resolved_value: float
    assumption_made: bool
    assumption_basis: str          # arithmetic_chain | explicit_callout | geometric_reasonableness | ...
    chain_ids_used: list[str]
    assumption_confidence: float
    flag_tier: str                 # HIGH | MEDIUM | LOW | CRITICAL
    human_note: str

    @property
    def resolved_by_tier(self) -> str:
        return tier_for_basis(self.assumption_basis)

    def as_fields(self) -> dict[str, Any]:
        return {
            "resolved_value": self.resolved_value,
            "assumption_made": self.assumption_made,
            "assumption_basis": self.assumption_basis,
            "chain_ids_used": list(self.chain_ids_used),
            "assumption_confidence": round(self.assumption_confidence, 3),
            "flag_tier": self.flag_tier,
            "resolved_by_tier": self.resolved_by_tier,
            "human_note": self.human_note,
        }


@dataclass
class FeatureResolution:
    """The resolution record added to one feature object."""

    feature_id: str
    build_status: str              # always "build"
    position_resolved: bool
    position_assumption: str
    flag_tier: str
    human_note: str

    def as_fields(self) -> dict[str, Any]:
        return {
            "build_status": self.build_status,
            "position_resolved": self.position_resolved,
            "position_assumption": self.position_assumption,
            "flag_tier": self.flag_tier,
            "human_note": self.human_note,
        }


@dataclass
class ResolutionSummary:
    total_dimensions: int = 0
    assumptions_made: int = 0
    critical_flags: int = 0
    low_flags: int = 0
    medium_flags: int = 0
    high_flags: int = 0
    rebuild_confidence: float = 1.0
    plain_english: str = ""


@dataclass
class ResolutionResult:
    """Everything Stage 2.5 produces.

    ``resolved_extraction`` is the rich annotated dict (every dimension carries
    ``resolved_value`` + flag tier + human note) — this is the on-disk deliverable.
    ``clean_extraction`` is the same data reduced to the canonical schema fields
    (resolved values written into ``value``, soft-block flags cleared, NO extra
    keys) so it validates against the strict ``extra='forbid'`` schema and can drive
    verification + the build.
    """

    resolved_extraction: dict
    clean_extraction: dict = field(default_factory=dict)
    dim_resolutions: dict[str, DimResolution] = field(default_factory=dict)
    feature_resolutions: dict[str, FeatureResolution] = field(default_factory=dict)
    summary: ResolutionSummary = field(default_factory=ResolutionSummary)
    # Flags that should surface in the build plan / stdout (MEDIUM and worse).
    flags: list[dict[str, Any]] = field(default_factory=list)

    def dim(self, dim_id: str) -> Optional[DimResolution]:
        return self.dim_resolutions.get(dim_id)

    def feature(self, feature_id: str) -> Optional[FeatureResolution]:
        return self.feature_resolutions.get(feature_id)


# --------------------------------------------------------------------------- #
# Candidate enumeration & geometric checks
# --------------------------------------------------------------------------- #
def _candidates(dim: dict) -> list[float]:
    """All plausible numeric readings for a dimension, best-guess first.

    Always includes the extracted ``value`` (the model's best guess). Adds any
    ``possible_values`` the model offered for an unclear reading. De-duplicated,
    positive-only, never empty when a value exists.
    """
    out: list[float] = []
    val = dim.get("value")
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        out.append(float(val))
    for pv in dim.get("possible_values", []) or []:
        if isinstance(pv, (int, float)) and not isinstance(pv, bool) and pv > 0:
            if float(pv) not in out:
                out.append(float(pv))
    return out


def _general_tolerance_value(model: DrawingData) -> float:
    """A coarse numeric tolerance from the general-tolerance block text.

    Used only as a closure slack / last-resort radius. Falls back to a small
    fraction when the block can't be parsed — never blocks.
    """
    import re

    text = model.general_tolerance or ""
    # Match only the numeric magnitude (e.g. "0.005" out of "±0.005"); ".XXX" has
    # no digits after the point and is correctly ignored.
    nums = [abs(float(m)) for m in re.findall(r"\d*\.\d+", text)]
    nums = [n for n in nums if n > 0]
    return min(nums) if nums else 0.01


def _envelope(model: DrawingData) -> tuple[float, float, float]:
    """Part overall (length, width, thickness/height) in drawing units; 0 if absent."""
    length = width = thickness = 0.0
    for d in model.dimensions:
        token = d.canonical_applies_to
        if is_envelope_label(d.applies_to) and not d.is_reference:
            if token == "length" and length == 0.0:
                length = d.value
            elif token == "width" and width == 0.0:
                width = d.value
            elif token == "height" and thickness == 0.0:
                thickness = d.value
        if token in ("thickness", "depth") and thickness == 0.0:
            thickness = d.value
    return length, width, thickness


def _passes_geometry(value: float, dim: dict, model: DrawingData) -> bool:
    """Apply the spec's Step-2 geometric-validity checks to a candidate value.

    Only the checks that can be GROUNDED in extracted data are applied; when the
    data needed for a check is absent, that check passes (we never block on
    missing context). Returns True if the candidate is geometrically defensible.
    """
    length, width, thickness = _envelope(model)
    token = canonicalize_applies_to(dim.get("applies_to", ""))

    # Cut depth must not exceed the solid's thickness (else use through-all later).
    if token in ("depth",) and thickness > 0 and value > thickness + 1e-9:
        return False

    # A diameter/length feature must fit inside the part envelope.
    largest_env = max(length, width, thickness)
    if token in ("diameter", "hole_diameter", "length", "width") and largest_env > 0:
        if value > largest_env + 1e-9:
            return False

    # Wall/clearance: a positive feature smaller than the minimum wall is suspect.
    if value < MIN_WALL and token in ("thickness",):
        return False

    return True


def _closing_candidate(
    dim_id: str, cands: list[float], model: DrawingData
) -> tuple[Optional[float], list[str], float]:
    """Step 1 — does exactly one candidate close a chain this dim belongs to?

    Returns ``(value_or_None, chain_ids_used, residual)``. ``value`` is set when
    a single candidate makes some chain close within slack; ``residual`` is the
    smallest absolute mismatch seen (for the broken-chain fallback).
    """
    chains = [
        c for c in model.relationships.dimension_chains
        if dim_id == c.total_dimension_id or dim_id in c.component_dimension_ids
    ]
    if not chains:
        return None, [], float("inf")

    best_resid = float("inf")
    for chain in chains:
        total = model.dimension_by_id(chain.total_dimension_id)
        comps = [model.dimension_by_id(cid) for cid in chain.component_dimension_ids]
        if total is None or any(c is None for c in comps):
            continue
        tol = sum(abs(c.tolerance_plus) + abs(c.tolerance_minus) for c in comps) \
            + abs(total.tolerance_plus) + abs(total.tolerance_minus)
        slack = max(tol, 1e-3 * abs(total.value), _general_tolerance_value(model))
        chain_ids = [total.id, *chain.component_dimension_ids]

        closing = []
        for cand in cands:
            # Substitute the candidate in whichever role this dim plays.
            if dim_id == total.id:
                comp_sum = sum(c.value for c in comps)
                resid = abs(comp_sum - cand)
            else:
                comp_sum = sum((cand if c.id == dim_id else c.value) for c in comps)
                resid = abs(comp_sum - total.value)
            best_resid = min(best_resid, resid)
            if resid <= slack:
                closing.append(cand)
        if len(closing) == 1:
            return closing[0], chain_ids, best_resid
    return None, [], best_resid


# --------------------------------------------------------------------------- #
# Operator must-meet specifications (specs-first enforcement)
# --------------------------------------------------------------------------- #
def _spec_numbers(requirements: Optional[list[str]]) -> list[tuple[float, str]]:
    """``(value, spec_text)`` for every numeric value in the operator's
    must-meet specification lines. These are first-class inputs to ambiguity
    resolution: a spec value that matches a candidate reading takes precedence
    over the generic decision tree (and is flagged as spec-driven)."""
    import re

    out: list[tuple[float, str]] = []
    for line in requirements or []:
        text = (line or "").strip()
        if not text:
            continue
        for m in re.findall(r"\d+(?:\.\d+)?", text):
            try:
                v = float(m)
            except ValueError:
                continue
            if v > 0:
                out.append((v, text))
    return out


def _spec_match(cands: list[float],
                spec_vals: list[tuple[float, str]]) -> Optional[tuple[float, str]]:
    """The first candidate (best-guess order) that agrees with a spec value
    within 1.5% (inch<->mm conversions tried), plus the matching spec text."""
    for cand in cands:
        for sv, text in spec_vals:
            for conv in (sv, sv * 25.4, sv / 25.4):
                if conv > 0 and abs(cand - conv) / max(conv, 1e-9) <= 0.015:
                    return cand, text
    return None


# --------------------------------------------------------------------------- #
# Stage 1.5 overview analysis (tier 2 — cross-view relationships)
# --------------------------------------------------------------------------- #
_OVERVIEW_SEVERITY_TO_TIER = {
    "CRITICAL": "CRITICAL", "HIGH": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW",
}


def _overview_flags(overview: dict, raw: dict) -> list[dict[str, Any]]:
    """Build-plan flags contributed by the Stage 1.5 holistic overview analysis.

    Two sources:
      * every ``cross_view_conflicts`` entry the analysis reported (its severity
        maps to a flag tier; CRITICAL stays CRITICAL) with the recommendation
        text folded into the human note;
      * a deterministic count cross-check — a global note stating a feature
        COUNT (e.g. "(6) HLS" -> resolved_count 6) that no extracted hole
        callout group satisfies means the per-view extraction and the sheet's
        own callout disagree: exactly the class of error a cropped view cannot
        resolve alone (an occluded 6th hole). Flagged CRITICAL, never dropped.

    Every flag records ``resolved_by_tier = tier2_overview`` and
    ``source = overview_analysis`` so it is traceable to this stage.
    """
    flags: list[dict[str, Any]] = []

    for i, c in enumerate(overview.get("cross_view_conflicts", []) or [], 1):
        desc = (c.get("description") or "").strip()
        if not desc:
            continue
        rec = (c.get("recommendation") or "").strip()
        views = ", ".join(c.get("views_involved", []) or [])
        tier = _OVERVIEW_SEVERITY_TO_TIER.get(
            str(c.get("severity", "")).upper(), "MEDIUM")
        note = f"CROSS-VIEW CONFLICT ({views or 'sheet'}): {desc}"
        if rec:
            note += f" Recommendation: {rec}"
        # Fix 4.3: route tier-2 ambiguities to markup, and flag cropped/off-sheet
        # cases as needing the full sheet so they don't recur every run.
        blob = f"{desc} {rec}".lower()
        needs_full_sheet = any(k in blob for k in
                               ("crop", "title block", "off-sheet", "off sheet", "cut off", "not shown"))
        flag = {
            "dimension_id": f"OV-{i:03d}",
            "flag_tier": tier,
            "human_note": note,
            "macro_behavior": behavior_for_tier(tier),
            "resolved_by_tier": TIER_OVERVIEW,
            "source": "overview_analysis",
            "route_to_markup": True,
        }
        if needs_full_sheet:
            flag["request_full_sheet"] = True
            flag["human_note"] += (" [request-full-sheet: this looks like a cropped/off-sheet "
                                   "region — upload the complete sheet rather than re-running.]")
        flags.append(flag)

    # Deterministic count cross-check: overview note count vs extracted callouts.
    # The count comes from the note's resolved_count, else is parsed from the note
    # text with the SHARED quantity-language parser (so the overview and
    # extraction read "(6) HL'S" / "6-HOLES" / "4 PLACES" identically).
    from pipeline.callout_qty import parse_quantity

    holes = raw.get("hole_callouts", []) or []
    qtys = [int(h.get("qty") or 0) for h in holes]
    for note_obj in overview.get("global_notes", []) or []:
        count = note_obj.get("resolved_count")
        if not isinstance(count, int) or count <= 0:
            parsed = parse_quantity(note_obj.get("note") or "", default=0)
            count = parsed if parsed > 0 else None
        if not isinstance(count, int) or count <= 0 or not holes:
            continue
        # Group-aware: consistent if the note count matches ANY per-group count OR
        # the total across groups (per-group counts summing to the total are NOT a
        # mismatch — the false positive fixed in overview_check).
        if count in qtys or count == sum(qtys):
            continue
        note_text = (note_obj.get("note") or "").strip()
        flags.append({
            "dimension_id": "OV-COUNT",
            "flag_tier": "CRITICAL",
            "human_note": (
                f"HOLE-COUNT DISAGREEMENT: the sheet's global callout \"{note_text}\" "
                f"states {count} instance(s), but the per-view extraction captured "
                f"hole group(s) of {qtys} (total {sum(qtys)}). A cropped view cannot "
                f"resolve this alone — check for an occluded instance (behind the "
                f"title block or a leader line) before building, or the part will "
                f"silently miss a feature."
            ),
            "macro_behavior": behavior_for_tier("CRITICAL"),
            "resolved_by_tier": TIER_OVERVIEW,
            "source": "overview_analysis",
        })
    return flags


# --------------------------------------------------------------------------- #
# Per-dimension resolution (the Step 1-4 decision tree)
# --------------------------------------------------------------------------- #
def _needs_resolution(dim: dict) -> bool:
    return bool(
        dim.get("value_unclear")
        or dim.get("resolution_required")
        or (dim.get("ambiguity_reason") or "").strip()
    )


def _resolve_dimension(dim: dict, model: DrawingData,
                       spec_vals: Optional[list[tuple[float, str]]] = None) -> DimResolution:
    dim_id = dim.get("id", "?")
    applies = dim.get("applies_to", "") or dim.get("type", "value")
    cands = _candidates(dim)

    # A clear, unambiguous dimension is confirmed as-is (HIGH); note whether a
    # chain corroborates it so the human_note is meaningful.
    if not _needs_resolution(dim) and cands:
        closing, chain_ids, _ = _closing_candidate(dim_id, cands, model)
        if closing is not None and chain_ids:
            note = (
                f"Confirmed: {dim_id} ({_fmt(cands[0])}) closes chain "
                f"{'+'.join(chain_ids)} within tolerance — no action needed."
            )
            return DimResolution(dim_id, cands[0], False, "arithmetic_chain",
                                 chain_ids, 0.97, "HIGH", note)
        note = f"{dim_id} ({_fmt(cands[0])}) read directly from the drawing callout — no action needed."
        return DimResolution(dim_id, cands[0], False, "explicit_callout", [], 0.92, "HIGH", note)

    # Did the model offer genuine ALTERNATIVE readings to choose between?
    has_alternatives = len(cands) >= 2

    # --- STEP 0: operator must-meet specification (specs-first precedence) ---
    # A human-authored spec that clarifies an ambiguous reading takes precedence
    # over the generic decision tree: when a candidate agrees with a spec value,
    # resolve to it and flag the resolution as spec-driven.
    if cands and spec_vals:
        matched = _spec_match(cands, spec_vals)
        if matched is not None:
            value, spec_text = matched
            note = (
                f"Resolved {dim_id} to {_fmt(value)} from the operator must-meet "
                f"specification \"{spec_text}\" (spec-driven; applied at resolution "
                f"time, verified against the build afterwards)."
            )
            return DimResolution(dim_id, value, True, "spec_driven", [], 0.90, "HIGH", note)

    # --- STEP 1: arithmetic chain check ---
    if cands:
        closing, chain_ids, residual = _closing_candidate(dim_id, cands, model)
        if closing is not None:
            note = (
                f"Resolved {dim_id} to {_fmt(closing)} — the only reading that closes chain "
                f"{'+'.join(chain_ids)}; verify against the drawing callout."
            )
            return DimResolution(dim_id, closing, True, "arithmetic_chain",
                                 chain_ids, 0.88, "HIGH", note)

    # An illegible reading with NOTHING to cross-check (no closing chain, no
    # alternative candidates) is the most dangerous case: we keep the single best
    # guess but flag it CRITICAL — a human must verify before rebuild. This is the
    # schema-bound analog of the spec's "value truly absent" Step-4 outcome.
    if cands and not has_alternatives:
        applies = dim.get("applies_to", "") or dim.get("type", "value")
        note = (
            f"{dim_id} reading ({_fmt(cands[0])}) is illegible/ambiguous with no alternative or "
            f"chain to confirm it; kept as a best guess for {applies} — MUST verify before rebuild."
        )
        return DimResolution(dim_id, cands[0], True, "unverifiable_reading", [], 0.30, "CRITICAL", note)

    # --- STEP 2: geometric validity check ---
    if cands:
        passing = [c for c in cands if _passes_geometry(c, dim, model)]
        if len(passing) == 1:
            note = (
                f"Assumed {dim_id} = {_fmt(passing[0])} on geometric grounds (only reading that fits "
                f"the part envelope); verify the {applies} callout in SolidWorks."
            )
            return DimResolution(dim_id, passing[0], True, "geometric_reasonableness",
                                 [], 0.70, "MEDIUM", note)
        if len(passing) > 1:
            # --- STEP 3: conservative geometry — smallest passing candidate ---
            chosen = min(passing)
            note = (
                f"Multiple readings of {dim_id} are valid; chose the most conservative "
                f"({_fmt(chosen)}, smallest) for {applies} — verify before relying on this feature."
            )
            return DimResolution(dim_id, chosen, True, "conservative_geometry",
                                 [], 0.50, "LOW", note)
        # No candidate passes geometry: take the smallest-residual / smallest value,
        # relaxing the constraint, and flag LOW.
        chosen = min(cands)
        note = (
            f"No reading of {dim_id} fully satisfies geometric checks; used the most conservative "
            f"({_fmt(chosen)}) and relaxed the constraint — human must verify {applies} before rebuild."
        )
        return DimResolution(dim_id, chosen, True, "conservative_geometry",
                             [], 0.45, "LOW", note)

    # --- STEP 4: last resort — no candidate value at all ---
    return _last_resort(dim, model)


def _last_resort(dim: dict, model: DrawingData) -> DimResolution:
    """Step 4: dimension truly has no readable value. Derive a defensible number."""
    dim_id = dim.get("id", "?")
    token = canonicalize_applies_to(dim.get("applies_to", ""))
    length, width, thickness = _envelope(model)
    gtol = _general_tolerance_value(model)

    if token in ("depth",):
        # Missing depth -> through-all (use thickness if known, else a nominal).
        value = thickness if thickness > 0 else max(length, width, 1.0)
        note = (f"{dim_id} depth missing from drawing — defaulted to THROUGH-ALL "
                f"({_fmt(value)}); verify the blind depth in SolidWorks before rebuild.")
        return DimResolution(dim_id, value, True, "default_through_all", [], 0.30, "CRITICAL", note)

    if token in ("radius", "fillet_radius"):
        note = (f"{dim_id} radius missing — defaulted to the general tolerance value "
                f"({_fmt(gtol)}); confirm the intended radius before rebuild.")
        return DimResolution(dim_id, gtol, True, "default_general_tolerance", [], 0.30, "CRITICAL", note)

    # Generic: derive from the nearest adjacent dimension of the same token, else envelope.
    adjacent = [
        d.value for d in model.dimensions
        if d.id != dim_id and d.canonical_applies_to == token and d.value > 0
    ]
    if adjacent:
        value = adjacent[0]
        note = (f"{dim_id} missing — derived from adjacent {token} dimension ({_fmt(value)}); "
                f"verify against the drawing before rebuild.")
        return DimResolution(dim_id, value, True, "derived_from_adjacent", [], 0.35, "CRITICAL", note)

    value = max(length, width, thickness, gtol, 1.0)
    note = (f"{dim_id} missing with no adjacent reference — placed at a nominal envelope-derived "
            f"value ({_fmt(value)}); MUST be corrected in SolidWorks before rebuild.")
    return DimResolution(dim_id, value, True, "placed_at_parent_center", [], 0.20, "CRITICAL", note)


# --------------------------------------------------------------------------- #
# Per-feature resolution
# --------------------------------------------------------------------------- #
def _resolve_feature(feat: dict, model: DrawingData) -> FeatureResolution:
    fid = feat.get("id", "?")
    position_known = bool(feat.get("position_known"))
    has_offset = bool(feat.get("offset_x") or feat.get("offset_y"))

    # A hole/thread feature is positioned by its callout: if the callout carries
    # explicit instance positions or position_known, the location IS resolved.
    ftype = (feat.get("type") or "").lower()
    if ftype in ("hole", "thread"):
        h = model.hole_callout_for_feature(fid)
        if h is not None and (h.instance_positions or h.position_known):
            position_known = True

    if position_known or has_offset:
        return FeatureResolution(
            fid, "build", True, "",
            "HIGH",
            f"{fid} position read from the drawing — no action needed.",
        )
    # Only features with an independent in-plane LOCATION need a position. Fillets,
    # chamfers, threads, patterns, mirrors, shells, revolves, and the base boss
    # apply to edges/faces/the whole body — they have no X/Y to resolve, so they
    # never route to position review (Fix 3.1 must not false-flag them).
    if ftype not in ("hole", "extrude_cut", "slot", "cutout", "pocket",
                     "counterbore", "countersink"):
        return FeatureResolution(
            fid, "build", True, "", "HIGH",
            f"{fid} has no independent location (applies to edges/faces/whole body) — "
            f"no position to resolve.",
        )
    # Fix 3.1 (learning-loop 2026-07-09: 33 POSITION ASSUMED flags, the top issue,
    # causally linked to cuts that miss the solid). An undimensioned location is
    # NOT silently centered any more. Centering is defensible ONLY when the
    # drawing gives symmetry evidence (the feature sits on a marked centerline /
    # is named in a symmetry relationship); otherwise the location is genuinely
    # unknown and must go to markup review, not a center guess.
    if _has_symmetry_evidence(feat, model):
        return FeatureResolution(
            fid, "build", True, "centered_on_parent", "LOW",
            f"{fid} centered on the parent: the drawing shows symmetry (centerline / "
            f"symmetry relationship) and no offset, so the center is the defensible "
            f"placement — verify in SolidWorks.",
        )
    return FeatureResolution(
        fid, "build", False, "needs_markup_review", "MEDIUM",
        f"POSITION UNRESOLVED for {fid}: no location dimension and no symmetry evidence "
        f"on the drawing. Routed to markup review — box the feature center and its X/Y "
        f"dimensions in the Tab-1 markup tool (a center + x-dimension + y-dimension color "
        f"group is authoritative). Until then it is placed at the parent center as a "
        f"LAST-RESORT guess that will likely be wrong.",
    )


def _has_symmetry_evidence(feat: dict, model: DrawingData) -> bool:
    """True when the drawing justifies centering a feature: it is named in a
    symmetry relationship (mirrored about / lying on a plane of symmetry). This
    is the only evidence the extraction schema records for symmetry."""
    fid = feat.get("id", "")
    if not fid:
        return False
    try:
        for note in model.relationships.symmetry or []:
            if fid in (getattr(note, "feature_ids", None) or []):
                return True
    except AttributeError:
        pass
    return False


def _feature_has_depth(feat: dict, dims_by_id: dict[str, dict]) -> bool:
    """True if an extrude feature already has a usable extrude-axis dimension."""
    did = feat.get("depth_dimension_id")
    if did and did in dims_by_id:
        d = dims_by_id[did]
        if (d.get("type") or "").lower() != "angular":
            return True  # builder exposes depth_dimension_id directly as "depth"
    for rid in feat.get("related_dimensions", []) or []:
        d = dims_by_id.get(rid)
        if d and canonicalize_applies_to(d.get("applies_to", "")) in _DEPTH_TOKENS:
            return True
    return False


def _next_dim_id(resolved: dict) -> str:
    """A fresh D### id not already used by any dimension."""
    import re

    used = {d.get("id", "") for d in resolved.get("dimensions", []) or []}
    n = 900
    while f"D{n}" in used:
        n += 1
    return f"D{n}"


def _ensure_buildable_extrudes(resolved: dict, model: DrawingData, result: "ResolutionResult") -> None:
    """Guarantee every base solid has a thickness so the part can never build empty.

    For each ``extrude_boss`` with no readable extrude-axis dimension, synthesize a
    thickness (≥ the nominal for the unit system, and thicker than the deepest known
    sub-feature cut so those cuts stay blind), attach it to the feature, and flag it
    CRITICAL. The number is a defensible default, NOT a drawing reading — the human
    must confirm it. ``extrude_cut`` with no depth is left for the builder's
    through-all default (more correct than guessing a blind depth)."""
    units = (resolved.get("units") or "inch").lower()
    nominal = NOMINAL_THICKNESS.get(units, NOMINAL_THICKNESS["inch"])
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}

    # Deepest known sub-feature depth (counterbore/blind/step), in drawing units.
    sub_depths = [
        d.value for d in model.dimensions
        if (d.canonical_applies_to in _DEPTH_TOKENS or d.type.value == "depth") and d.value > 0
    ]
    deepest = max(sub_depths, default=0.0)
    thickness = round(max(nominal, deepest + nominal), 4)

    for feat in resolved.get("features", []) or []:
        if (feat.get("type") or "").lower() != "extrude_boss":
            continue
        if _feature_has_depth(feat, dims_by_id):
            continue
        new_id = _next_dim_id(resolved)
        fid = feat.get("id", "?")
        note = (
            f"THICKNESS ASSUMED for base {fid}: the drawing does not dimension the part "
            f"thickness, so {new_id}={_fmt(thickness)} {units} was synthesized so a solid "
            f"could be built — MUST set the real thickness in SolidWorks before rebuild."
        )
        new_dim = {
            "id": new_id, "type": "depth", "value": thickness, "unit": units,
            "applies_to": "thickness", "feature_ref": fid,
            "notes": "Synthesized by Stage 2.5 — thickness not dimensioned on the drawing.",
        }
        dres = DimResolution(new_id, thickness, True, "default_base_thickness", [], 0.25,
                             "CRITICAL", note)
        new_dim.update(dres.as_fields())
        resolved.setdefault("dimensions", []).append(new_dim)
        dims_by_id[new_id] = new_dim
        result.dim_resolutions[new_id] = dres
        feat["depth_dimension_id"] = new_id
        rel = feat.setdefault("related_dimensions", [])
        if new_id not in rel:
            rel.append(new_id)


import re as _re

_TYP_RE = _re.compile(r"\btyp(ical)?\b", _re.IGNORECASE)


def _dim_is_typ(dim: dict) -> bool:
    """True if a dimension is marked TYP/TYPICAL (the value repeats on sibling
    features). Extraction may put the qualifier in notes, ambiguity_reason, or
    the applies_to/label text — scan them all."""
    blob = " ".join(str(dim.get(k, "")) for k in
                    ("notes", "ambiguity_reason", "applies_to", "label", "raw_text"))
    return bool(_TYP_RE.search(blob))


def _propagate_typ(resolved: dict, result: "ResolutionResult") -> None:
    """Fix 2.2 (learning-loop 2026-07-09, e.g. 16247 '.531 R. TYP' on one of two
    notches): a radius/chamfer callout marked TYP applies to EVERY geometrically
    similar sibling. When a fillet/chamfer feature has no driving dimension but a
    TYP value of its kind exists on the sheet, attach that value as an INFERRED
    dimension so the sibling is no longer dimensionless. Never overrides a real
    reading; the inferred value is flagged so a human can confirm."""
    dims = resolved.get("dimensions", []) or []
    feats = resolved.get("features", []) or []
    by_id = {d.get("id"): d for d in dims}

    def _typ_source(tokens: tuple[str, ...]) -> Optional[dict]:
        for d in dims:
            if (_dim_is_typ(d)
                    and canonicalize_applies_to(d.get("applies_to", "")) in tokens
                    and isinstance(d.get("value"), (int, float)) and d.get("value", 0) > 0):
                return d
        return None

    typ_radius = _typ_source(("fillet_radius", "radius"))
    typ_chamfer = _typ_source(("chamfer",))
    if typ_radius is None and typ_chamfer is None:
        return

    def _feature_has_role(feat: dict, tokens: tuple[str, ...]) -> bool:
        rel = list(feat.get("related_dimensions", []) or [])
        did = feat.get("depth_dimension_id")
        if did:
            rel.append(did)
        for rid in rel:
            d = by_id.get(rid)
            if d and canonicalize_applies_to(d.get("applies_to", "")) in tokens:
                return True
        return False

    plan = (("fillet", typ_radius, ("fillet_radius", "radius"), "fillet_radius"),
            ("chamfer", typ_chamfer, ("chamfer",), "chamfer"))
    for ftype, src, tokens, applies in plan:
        if src is None:
            continue
        for feat in feats:
            if (feat.get("type") or "").lower() != ftype:
                continue
            if _feature_has_role(feat, tokens):
                continue  # already has its own reading — never overridden
            fid = feat.get("id", "?")
            new_id = _next_dim_id(resolved)
            val = round(float(src["value"]), 6)
            note = (f"{new_id}={_fmt(val)} inferred for {ftype} {fid} from the TYP "
                    f"callout {src.get('id', '?')} ({_fmt(val)} {src.get('unit', '')}) — "
                    f"same value repeats on geometrically similar features; verify.")
            new_dim = {
                "id": new_id, "type": "radius" if ftype == "fillet" else "linear",
                "value": val, "unit": src.get("unit", "inch"), "applies_to": applies,
                "feature_ref": fid,
                "notes": f"Inferred by Stage 2.5 TYP propagation from {src.get('id', '?')}.",
            }
            dres = DimResolution(new_id, val, True, "typ_propagation", [src.get("id", "?")],
                                 0.7, "MEDIUM", note)
            new_dim.update(dres.as_fields())
            resolved.setdefault("dimensions", []).append(new_dim)
            by_id[new_id] = new_dim
            result.dim_resolutions[new_id] = dres
            rel = feat.setdefault("related_dimensions", [])
            if new_id not in rel:
                rel.append(new_id)


def _illegible_diameter_flags(resolved: dict, result: "ResolutionResult") -> list[dict[str, Any]]:
    """Fix 3.2 (learning-loop 2026-07-09: part 102 D008/D009, the whole part's
    hole diameter kept as an unverified guess). For every hole/diameter that was
    resolved from an illegible/last-resort reading, cross-check it against the
    standard drill-size table: a match is evidence the guess is right; a non-match
    at low confidence routes the callout to markup for human transcription — a
    'must verify' note that does not stop the build is not a gate."""
    from pipeline.drill_sizes import is_standard_drill, nearest_drill

    weak = {"unverifiable_reading", "value_only_fallback", "conservative_geometry",
            "geometric_reasonableness", "default_general_tolerance"}
    flags: list[dict[str, Any]] = []
    for dim in resolved.get("dimensions", []) or []:
        if canonicalize_applies_to(dim.get("applies_to", "")) not in ("diameter", "hole_diameter"):
            continue
        dres = result.dim_resolutions.get(dim.get("id"))
        if dres is None or not dres.assumption_made or dres.assumption_basis not in weak:
            continue
        val = float(dim.get("value") or 0)
        units = (resolved.get("units") or "inch").lower()
        val_in = val if units.startswith("inch") else val / 25.4
        if is_standard_drill(val_in):
            # Plausible reading — annotate the resolution, no new flag.
            dres.human_note += (f" [plausibility: {_fmt(val)} matches a standard drill size — "
                                f"supports the reading.]")
            continue
        near, diff = nearest_drill(val_in)
        flags.append({
            "dimension_id": dim.get("id", "?"),
            "flag_tier": "CRITICAL",
            "human_note": (
                f"ILLEGIBLE DIAMETER {dim.get('id', '?')} = {_fmt(val)}: read at low confidence "
                f"and NOT a standard drill size (nearest {near:.4f} in, off by {diff:.4f} in). "
                f"Do not build on this guess — box the diameter callout in the Tab-1 markup tool "
                f"and transcribe it, then re-run. Routed to markup review."),
            "macro_behavior": behavior_for_tier("CRITICAL"),
            "resolved_by_tier": TIER_PER_VIEW,
            "source": "illegible_dimension",
            "route_to_markup": True,
        })
    return flags


def _incomplete_cut_profile_flags(resolved: dict) -> list[dict[str, Any]]:
    """Fix 2.4 (learning-loop 2026-07-09: A001821M F004 'profile needs a diameter
    or length+width; got [height]' ×3; A001211E F005). An extrude cut/boss whose
    profile has neither a diameter nor BOTH in-plane sides can't be sketched. Do
    not emit a known-incomplete macro silently every run — surface it as a
    CRITICAL review item naming the missing parameter, routed to markup (box the
    two chain dimensions that bound the notch)."""
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    flags: list[dict[str, Any]] = []
    for feat in resolved.get("features", []) or []:
        if (feat.get("type") or "").lower() not in ("extrude_cut", "extrude_boss"):
            continue
        rel = list(feat.get("related_dimensions", []) or [])
        tokens = {canonicalize_applies_to((dims_by_id.get(r) or {}).get("applies_to", ""))
                  for r in rel if (dims_by_id.get(r) or {}).get("value", 0) > 0}
        has_diameter = bool(tokens & {"diameter", "hole_diameter"})
        has_rect = ("length" in tokens) and ("width" in tokens)
        if has_diameter or has_rect:
            continue
        fid = feat.get("id", "?")
        missing = "width" if "length" in tokens else ("length" if "width" in tokens else "length+width")
        flags.append({
            "dimension_id": fid,
            "flag_tier": "CRITICAL",
            "human_note": (
                f"INCOMPLETE CUT PROFILE for {fid}: has {sorted(t for t in tokens if t)} but needs "
                f"a diameter OR both length+width — missing {missing}. A notch width is often the "
                f"difference of two chain dimensions; box those two dimensions (or the missing "
                f"{missing}) in the Tab-1 markup tool and transcribe, then re-run. Routed to markup."),
            "macro_behavior": behavior_for_tier("CRITICAL"),
            "resolved_by_tier": TIER_PER_VIEW,
            "source": "incomplete_profile",
            "route_to_markup": True,
        })
    return flags


def _dimensionless_feature_flags(resolved: dict) -> list[dict[str, Any]]:
    """Fix 2.1 (regression: A001821M chamfer F005 'no distance dimension' ×3;
    A001211E fillet F006). A fillet with no radius, or a chamfer with no distance
    (even after TYP propagation), must NOT be built blindly — surface it as a
    CRITICAL review item routed to the Tab-1 markup queue so a human transcribes
    the radius/distance, rather than the build silently skipping it every run."""
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    need = {"fillet": ("fillet_radius", "radius"), "chamfer": ("chamfer",)}
    flags: list[dict[str, Any]] = []
    for feat in resolved.get("features", []) or []:
        ftype = (feat.get("type") or "").lower()
        tokens = need.get(ftype)
        if not tokens:
            continue
        rel = list(feat.get("related_dimensions", []) or [])
        if feat.get("depth_dimension_id"):
            rel.append(feat["depth_dimension_id"])
        has_driving = any(
            canonicalize_applies_to((dims_by_id.get(rid) or {}).get("applies_to", "")) in tokens
            and (dims_by_id.get(rid) or {}).get("value", 0) > 0
            for rid in rel)
        if has_driving:
            continue
        fid = feat.get("id", "?")
        what = "radius" if ftype == "fillet" else "distance"
        flags.append({
            "dimension_id": fid,
            "flag_tier": "CRITICAL",
            "human_note": (
                f"MISSING DIMENSION: {ftype} {fid} has no {what} value (none read, and no "
                f"TYP value of its kind to inherit). Do not build it blindly — highlight the "
                f"{what} callout on the drawing in the Tab-1 markup tool and transcribe the "
                f"value, then re-run. Routed to markup review."),
            "macro_behavior": behavior_for_tier("CRITICAL"),
            "resolved_by_tier": TIER_PER_VIEW,
            "source": "missing_dimension",
            "route_to_markup": True,
        })
    return flags


def _resolve_hole_position_flag(hole: dict, fid_note: str) -> Optional[dict]:
    """A hole callout with unknown positions contributes a build-plan flag."""
    if hole.get("instance_positions") or hole.get("position_known"):
        return None
    hid = hole.get("id", "?")
    return {
        "dimension_id": hid,
        "flag_tier": "LOW",
        "human_note": (
            f"POSITION ASSUMED for hole {hid}: centered/laid-out from the envelope because the "
            f"drawing did not dimension each instance — verify hole locations in SolidWorks."
        ),
        "macro_behavior": "msgbox_on_run",
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _fmt(v: float) -> str:
    return f"{v:.6g}"


_BEHAVIOR_BY_TIER = {
    "HIGH": "comment_only",
    "MEDIUM": "msgbox_on_run",
    "LOW": "msgbox_on_run",
    "CRITICAL": "confirm_on_run",
}


def behavior_for_tier(tier: str) -> str:
    """Macro behavior keyword for a flag tier (read by the macro generator)."""
    return _BEHAVIOR_BY_TIER.get(tier, "comment_only")


# Keys the resolver adds on top of the canonical schema — stripped to make a
# schema-valid (extra='forbid') copy for verification / model building.
_DIM_EXTRA_KEYS = (
    "resolved_value", "assumption_made", "assumption_basis", "chain_ids_used",
    "assumption_confidence", "flag_tier", "resolved_by_tier", "human_note",
)
_FEATURE_EXTRA_KEYS = (
    "build_status", "position_resolved", "position_assumption", "flag_tier", "human_note",
)
_TOP_EXTRA_KEYS = ("resolution", "overview_analysis")


def schema_clean(resolved: dict) -> dict:
    """A schema-valid copy of a resolved extraction: resolved values are kept (in
    ``value``) but the Stage-2.5 annotation keys are removed so the strict
    ``extra='forbid'`` :class:`~pipeline.schema.DrawingData` accepts it."""
    clean = copy.deepcopy(resolved)
    for k in _TOP_EXTRA_KEYS:
        clean.pop(k, None)
    for dim in clean.get("dimensions", []) or []:
        for k in _DIM_EXTRA_KEYS:
            dim.pop(k, None)
    for feat in clean.get("features", []) or []:
        for k in _FEATURE_EXTRA_KEYS:
            feat.pop(k, None)
    return clean


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def resolve_extraction(raw: dict,
                       requirements: Optional[list[str]] = None,
                       overview_analysis: Optional[dict] = None) -> ResolutionResult:
    """Run the Stage 2.5 resolution pass over a raw extraction dict.

    ``requirements`` (operator must-meet specification lines) are a first-class
    input: a spec value that clarifies an ambiguous dimension takes precedence
    over the generic decision tree, and that resolution is flagged spec-driven.

    ``overview_analysis`` (the Stage 1.5 holistic pass over the FULL drawing
    sheet) is the tier-2 input: authoritative on cross-view relationships that
    a single cropped view cannot determine. Its cross-view conflicts (and a
    deterministic callout-count cross-check) become flags sourced
    ``overview_analysis`` with ``resolved_by_tier = tier2_overview``. Priority
    when sources disagree: tier0 spec > tier1 per-view values > tier2 overview
    relationships; every flag records which tier resolved it.

    Returns a :class:`ResolutionResult` carrying the enriched
    ``resolved_extraction`` dict (every dimension has ``resolved_value`` and a
    flag tier; every feature has ``build_status == "build"``), the structured
    resolution records, the surfaced flags, and a plain-English summary.

    Never raises on ambiguity — that is the whole point. If the raw dict cannot
    even be coerced into the schema, the original dict is returned unchanged with
    a single CRITICAL summary note so the caller can still proceed/report.
    """
    resolved = copy.deepcopy(raw)
    result = ResolutionResult(resolved_extraction=resolved)
    spec_vals = _spec_numbers(requirements)

    try:
        model = DrawingData.model_validate(raw)
    except Exception as e:  # shape is wrong; resolve what we trivially can
        log.warning("resolver: extraction did not validate (%s); applying value-only resolution", e)
        _value_only_resolution(resolved, result)
        return result

    # --- Dimensions ---
    dims = resolved.get("dimensions", []) or []
    for dim in dims:
        res = _resolve_dimension(dim, model, spec_vals)
        result.dim_resolutions[res.dimension_id] = res
        dim.update(res.as_fields())
        # Once resolved, the value becomes the resolved value and the soft-block
        # flags are cleared so downstream verification reads it as READY.
        dim["value"] = res.resolved_value
        dim["resolution_required"] = False
        # Keep value_unclear=True visible ONLY as history is not needed; clearing it
        # lets the existing validator pass. The assumption is preserved in the new
        # fields + human_note.
        if res.assumption_made:
            dim["value_unclear"] = False

    # --- Features ---
    feats = resolved.get("features", []) or []
    for feat in feats:
        fres = _resolve_feature(feat, model)
        result.feature_resolutions[fres.feature_id] = fres
        feat.update(fres.as_fields())

    # TYP propagation: a radius/chamfer callout marked TYP fills in dimensionless
    # sibling fillets/chamfers (before the buildable-base pass so any synthesized
    # dims are consistent).
    _propagate_typ(resolved, result)

    # Guarantee a buildable base: synthesize a thickness for any extrude_boss the
    # drawing never dimensioned, so the part can never produce an empty solid.
    _ensure_buildable_extrudes(resolved, model, result)

    # --- Surface flags (MEDIUM and worse) for the build plan & stdout ---
    # Every flag records which priority tier resolved it (tier0 spec / tier1
    # per-view / tier2 overview) so the choice of value is always traceable.
    for res in result.dim_resolutions.values():
        if res.flag_tier in ("MEDIUM", "LOW", "CRITICAL"):
            result.flags.append({
                "dimension_id": res.dimension_id,
                "flag_tier": res.flag_tier,
                "human_note": res.human_note,
                "macro_behavior": behavior_for_tier(res.flag_tier),
                "resolved_by_tier": res.resolved_by_tier,
            })
    for fres in result.feature_resolutions.values():
        if fres.flag_tier in ("MEDIUM", "LOW", "CRITICAL"):
            flag = {
                "dimension_id": fres.feature_id,
                "flag_tier": fres.flag_tier,
                "human_note": fres.human_note,
                "macro_behavior": behavior_for_tier(fres.flag_tier),
                "resolved_by_tier": TIER_PER_VIEW,
            }
            if fres.position_assumption == "needs_markup_review":
                flag["source"] = "position_unresolved"
                flag["route_to_markup"] = True
            result.flags.append(flag)
    for hole in resolved.get("hole_callouts", []) or []:
        flag = _resolve_hole_position_flag(hole, "")
        if flag:
            flag["resolved_by_tier"] = TIER_PER_VIEW
            result.flags.append(flag)

    # Fix 2.1: dimensionless fillet/chamfer features → CRITICAL review routed to
    # markup (so the emphasized A001821M chamfer regression surfaces as a review
    # task, not a silent per-run build skip).
    result.flags.extend(_dimensionless_feature_flags(resolved))
    # Fix 2.4: extrude cut/boss with an incomplete profile → CRITICAL review.
    result.flags.extend(_incomplete_cut_profile_flags(resolved))
    # Fix 3.2: low-confidence hole diameters cross-checked against drill sizes;
    # implausible ones route to markup instead of building on a guess.
    result.flags.extend(_illegible_diameter_flags(resolved, result))

    # --- Tier 2: Stage 1.5 holistic overview analysis (cross-view conflicts
    # + callout-count cross-check). Only adds flags — never changes a tier-1
    # extracted value (per-view extraction stays authoritative on dimensions).
    if overview_analysis:
        ov_flags = _overview_flags(overview_analysis, resolved)
        result.flags.extend(ov_flags)
        resolved["overview_analysis"] = {
            "overall_shape_summary": overview_analysis.get("overall_shape_summary", ""),
            "views_detected": overview_analysis.get("views_detected", []),
            "symmetry": overview_analysis.get("symmetry", {}),
            "n_conflicts": len(overview_analysis.get("cross_view_conflicts", []) or []),
            "flags_contributed": ov_flags,
        }
        for f in ov_flags:
            log.info("overview flag [%s]: %s", f["flag_tier"], f["human_note"])

    _summarize(result)
    # Overview-contributed flags count toward the summary tiers (they are not
    # dimension resolutions, so _summarize alone would miss them).
    for f in result.flags:
        if f.get("source") == "overview_analysis":
            t = f.get("flag_tier")
            if t == "CRITICAL":
                result.summary.critical_flags += 1
                result.summary.rebuild_confidence = min(
                    result.summary.rebuild_confidence, 0.55)
            elif t == "MEDIUM":
                result.summary.medium_flags += 1
            elif t == "LOW":
                result.summary.low_flags += 1
    # Stamp the summary onto the resolved extraction for traceability.
    resolved["resolution"] = _summary_dict(result.summary)
    # The schema-valid twin that drives verification + the build.
    result.clean_extraction = schema_clean(resolved)
    return result


def _summary_dict(s: ResolutionSummary) -> dict:
    return {
        "total_dimensions": s.total_dimensions,
        "assumptions_made": s.assumptions_made,
        "critical_flags": s.critical_flags,
        "low_flags": s.low_flags,
        "medium_flags": s.medium_flags,
        "high_flags": s.high_flags,
        "rebuild_confidence": round(s.rebuild_confidence, 3),
        "plain_english": s.plain_english,
    }


def _value_only_resolution(resolved: dict, result: ResolutionResult) -> None:
    """Fallback when the extraction can't be schema-validated: still guarantee a
    numeric ``resolved_value`` on every dimension and ``build_status`` on features."""
    for dim in resolved.get("dimensions", []) or []:
        val = dim.get("value")
        if not (isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0):
            pvs = [p for p in (dim.get("possible_values") or [])
                   if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0]
            val = float(pvs[0]) if pvs else 1.0
        res = DimResolution(dim.get("id", "?"), float(val), True, "value_only_fallback",
                            [], 0.30, "CRITICAL",
                            f"{dim.get('id', '?')} resolved by value-only fallback (extraction did "
                            f"not validate) — verify everything in SolidWorks before rebuild.")
        result.dim_resolutions[res.dimension_id] = res
        dim.update(res.as_fields())
        dim["value"] = res.resolved_value
        dim["resolution_required"] = False
        dim["value_unclear"] = False
    for feat in resolved.get("features", []) or []:
        fres = FeatureResolution(feat.get("id", "?"), "build", False, "value_only_fallback",
                                "CRITICAL", f"{feat.get('id', '?')} build forced by fallback — verify.")
        result.feature_resolutions[fres.feature_id] = fres
        feat.update(fres.as_fields())
    _summarize(result)
    resolved["resolution"] = _summary_dict(result.summary)
    result.clean_extraction = schema_clean(resolved)


def _summarize(result: ResolutionResult) -> None:
    s = result.summary
    dims = list(result.dim_resolutions.values())
    s.total_dimensions = len(dims)
    s.assumptions_made = sum(1 for d in dims if d.assumption_made)

    all_tiers = [d.flag_tier for d in dims] + [f.flag_tier for f in result.feature_resolutions.values()]
    s.critical_flags = sum(1 for t in all_tiers if t == "CRITICAL")
    s.low_flags = sum(1 for t in all_tiers if t == "LOW")
    s.medium_flags = sum(1 for t in all_tiers if t == "MEDIUM")
    s.high_flags = sum(1 for t in all_tiers if t == "HIGH")

    # Rebuild confidence: mean of per-dimension confidences, floored by the worst
    # flag tier so a single CRITICAL assumption can't read as a confident model.
    if dims:
        mean_conf = sum(d.assumption_confidence for d in dims) / len(dims)
    else:
        mean_conf = 1.0
    ceiling = 1.0
    if s.critical_flags:
        ceiling = 0.55
    elif s.low_flags:
        ceiling = 0.75
    elif s.medium_flags:
        ceiling = 0.88
    s.rebuild_confidence = min(mean_conf, ceiling)

    # Plain-English narrative naming the most severe assumption(s).
    worst = [d for d in dims if d.flag_tier == "CRITICAL"] or \
            [d for d in dims if d.flag_tier == "LOW"] or \
            [d for d in dims if d.flag_tier == "MEDIUM"]
    if not worst:
        s.plain_english = (
            "All dimensions confirmed by arithmetic chain or explicit callout. "
            "Model is complete and buildable as-is."
        )
    else:
        lead = worst[0]
        s.plain_english = (
            f"{lead.human_note} "
            f"{s.assumptions_made} of {s.total_dimensions} dimension(s) required an engineering "
            f"assumption ({s.critical_flags} critical, {s.low_flags} low, {s.medium_flags} medium). "
            f"Model is complete and buildable as-is; verify flagged items in SolidWorks."
        )
