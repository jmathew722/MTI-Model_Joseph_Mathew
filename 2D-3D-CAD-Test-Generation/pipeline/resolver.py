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
import re as _re
from dataclasses import dataclass, field
from typing import Any, Optional

from pipeline.schema import (
    DrawingData,
    canonicalize_applies_to,
    is_envelope_label,
)
from utils.logger import get_logger

log = get_logger()

# Commit-to-extraction mode (2026-07-11): build every extracted feature, derive
# everything derivable, flag anything inferred — never exclude, never route to
# review as a terminal state, never a [0,0] placeholder. Default ON; OFF restores
# the old exclude/review behavior for comparison.
COMMIT_MODE_DEFAULT = True

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
TIER_HUMAN = "tier_human"  # a human answer to an escalated question — outranks all
TIER_SPEC = "tier0_spec"
TIER_PER_VIEW = "tier1_per_view"
TIER_OVERVIEW = "tier2_overview"


def tier_for_basis(assumption_basis: str) -> str:
    """The priority tier that produced a resolution, from its basis keyword."""
    if assumption_basis == "human_provided":
        return TIER_HUMAN
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
    # Per hole-feature placement classification + datum-chain provenance
    # (2026-07-12): {fid: {"placement": "individual"|"pattern",
    #  "pattern_evidence": str, "position_basis": [{anchor, dim, value, axis}],
    #  "datum_points": ["DP_F00x"]}}. Consumed by the macro/build-plan layer.
    hole_placements: dict[str, dict] = field(default_factory=dict)

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

# P7 — inspection-balloon signatures: circled numerals with an optional sheet
# subscript ("1/1", "2/1", "2/2"), and inspection values suffixed "IN." A conflict
# that is really about a balloon reference is not a geometry disagreement.
_BALLOON_TAG_RE = _re.compile(r"\b\d{1,2}\s*/\s*\d{1,2}\b")
_BALLOON_IN_RE = _re.compile(r"\d+(?:\.\d+)?\s*IN\.", _re.IGNORECASE)
_BALLOON_WORD_RE = _re.compile(r"\b(balloon|inspection|bubble|circled\s+numeral)\b", _re.IGNORECASE)


def _is_inspection_balloon_conflict(text: str) -> bool:
    """True when a cross-view conflict is really about an inspection balloon
    (circled numeral / 'IN.' value / the words balloon|inspection) rather than a
    competing geometry dimension."""
    t = text or ""
    if _BALLOON_WORD_RE.search(t):
        return True
    # A '1/1'-style tag plus an inspection-style 'N.NN IN.' value together are a
    # strong balloon signature (either alone is too weak — dates, fractions).
    return bool(_BALLOON_TAG_RE.search(t) and _BALLOON_IN_RE.search(t))


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
        # P7 (2026-07-10): an inspection-balloon reference (circled numeral like
        # "1/1", "2/1", leader-attached, value suffixed "IN.") is NOT a competing
        # geometry dimension — comparing it to extracted geometry fired false HIGH
        # conflicts (A001561E 11.00 vs 10.00). Recognize it, DOWNGRADE to a LOW
        # informational note, and never let it gate as a dimension conflict.
        if _is_inspection_balloon_conflict(f"{desc} {rec}"):
            flags.append({
                "dimension_id": f"OV-{i:03d}",
                "flag_tier": "LOW",
                "human_note": (f"INSPECTION-BALLOON REFERENCE ({views or 'sheet'}): {desc} "
                               f"Recognized as an inspection dimension balloon (circled numeral / "
                               f"'IN.' value), attached to part metadata — NOT a competing geometry "
                               f"dimension, so it is excluded from dimension-conflict reconciliation."),
                "macro_behavior": behavior_for_tier("LOW"),
                "resolved_by_tier": TIER_OVERVIEW,
                "source": "overview_analysis",
                "inspection_balloon": True,
                "request_full_sheet": True,
            })
            continue
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
    from pipeline.callout_qty import classify_callout, parse_quantity

    holes = raw.get("hole_callouts", []) or []
    qtys = [int(h.get("qty") or 0) for h in holes]
    for note_obj in overview.get("global_notes", []) or []:
        note_raw = note_obj.get("note") or ""
        # P5 (2026-07-10): type the callout before counting. A RADIUS callout
        # (e.g. ".12 R. TYP." — a corner round on 4 corners) is not a hole and
        # must NOT be reconciled against the hole count (A001591E false CRITICAL).
        # It reconciles against fillet features instead.
        if classify_callout(note_raw) == "radius":
            continue
        count = note_obj.get("resolved_count")
        if not isinstance(count, int) or count <= 0:
            parsed = parse_quantity(note_raw, default=0)
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


def _decimal_shift_candidates(dim: dict) -> list[float]:
    """Candidate values for P6 decimal-plausibility. Uses the model's explicit
    alternatives when it gave ≥2, else — for a reading the model marked as
    decimal-ambiguous ("312" with no visible point) — generates the order-of-
    magnitude shifts of the digits so plausibility can separate them."""
    cands = _candidates(dim)
    if len(cands) >= 2:
        return cands
    reason = (dim.get("ambiguity_reason") or "").lower()
    if cands and any(k in reason for k in ("decimal", "no point", "point", "magnitude")):
        base = cands[0]
        shifts = {round(base * f, 4) for f in (0.001, 0.01, 0.1, 1.0, 10.0)}
        return sorted(x for x in shifts if x > 0)
    return cands


def _decimal_plausibility(dim: dict, model: DrawingData) -> Optional["DimResolution"]:
    """Resolve an ambiguous numeral by plausibility (P6/P10). Returns a resolution
    when there are ≥2 candidates to choose between, else None (caller falls
    through). A clear winner is MEDIUM; a genuine tie is CRITICAL (and the
    completeness gate excludes the dependent feature)."""
    import math

    cands = _decimal_shift_candidates(dim)
    if len(cands) < 2:
        return None
    dim_id = dim.get("id", "?")
    token = canonicalize_applies_to(dim.get("applies_to", ""))
    units = model.units.value if hasattr(model.units, "value") else str(model.units)
    others = [d.value for d in model.dimensions
              if d.value > 0 and not d.value_unclear and d.id != dim_id]

    def _score(c: float) -> float:
        s = 0.0
        if others:
            lo, hi = min(others), max(others)
            if lo * 0.5 <= c <= hi * 2.0:               # (a) geometric self-consistency
                s += 2.0
            med = sorted(others)[len(others) // 2]
            s += -abs(math.log10(c) - math.log10(med))  # closeness to typical magnitude
            frac_sub1 = sum(1 for o in others if o < 1.0) / len(others)
            if frac_sub1 >= 0.5 and c < 1.0:            # (b) sheet leading-dot formatting
                s += 1.0
        if token in ("diameter", "hole_diameter"):       # (c) standard drill/stock tiebreak
            from pipeline.drill_sizes import is_standard_drill
            c_in = c if str(units).startswith("inch") else c / 25.4
            if is_standard_drill(c_in):
                s += 1.5
        return s

    scored = sorted(((_score(c), c) for c in cands), key=lambda t: t[0], reverse=True)
    (top_s, top_c), (second_s, _) = scored[0], scored[1]
    shown = [_fmt(c) for c in cands]
    if top_s - second_s >= 1.0:
        note = (f"Resolved {dim_id} to {_fmt(top_c)} by decimal-plausibility "
                f"(sheet magnitude/formatting"
                + (" + standard drill" if token in ("diameter", "hole_diameter") else "")
                + f"); candidates {shown} — verify against the callout.")
        return DimResolution(dim_id, top_c, True, "decimal_plausibility", [], 0.72, "MEDIUM", note)
    note = (f"{dim_id} decimal placement AMBIGUOUS among {shown}; plausibility could not "
            f"separate them. Kept {_fmt(top_c)} as a best guess and the dependent feature is "
            f"excluded from the build (Tab-3 assumption) — confirm the value and re-run.")
    return DimResolution(dim_id, top_c, True, "ambiguous_multi_candidate", [], 0.30, "CRITICAL", note)


def _resolve_dimension(dim: dict, model: DrawingData,
                       spec_vals: Optional[list[tuple[float, str]]] = None,
                       human_answers: Optional[dict[str, float]] = None) -> DimResolution:
    dim_id = dim.get("id", "?")
    applies = dim.get("applies_to", "") or dim.get("type", "value")
    cands = _candidates(dim)

    # --- STEP -1: a human answer to an escalated question (human_assist) is the
    # single highest-priority source. A person looking at the actual sheet
    # outranks every automated tier, including the operator spec — they resolved
    # exactly this ambiguity. Never fabricated: only present when a question was
    # answered. (See pipeline/human_assist.py.)
    if human_answers and dim_id in human_answers:
        value = float(human_answers[dim_id])
        note = (f"Resolved {dim_id} to {_fmt(value)} from a human answer to the "
                f"escalated question (human-provided; outranks all automated tiers).")
        return DimResolution(dim_id, value, True, "human_provided", [], 0.99, "HIGH", note)

    # Verbatim invariant (2026-07-12, "extraction is truth" — Task 2): a
    # dimension the extraction did NOT flag as unclear/ambiguous is a single
    # clean reading — it passes through unchanged at confidence 1.0, basis
    # extracted_verbatim. Sub-1.0 confidence is reserved for dimensions where
    # ≥2 genuine candidates actually existed (the branches below); a value
    # "read straight off the drawing" must never carry a lesser score, or a
    # future close-candidate reshuffle could silently flip it between runs.
    if not _needs_resolution(dim) and cands:
        closing, chain_ids, _ = _closing_candidate(dim_id, cands, model)
        if closing is not None and chain_ids:
            note = (
                f"{dim_id} ({_fmt(cands[0])}) read verbatim from the drawing callout; "
                f"also closes chain {'+'.join(chain_ids)} within tolerance — no action needed."
            )
            return DimResolution(dim_id, cands[0], False, "extracted_verbatim",
                                 chain_ids, 1.0, "HIGH", note)
        note = f"{dim_id} ({_fmt(cands[0])}) read verbatim from the drawing callout — no action needed."
        return DimResolution(dim_id, cands[0], False, "extracted_verbatim", [], 1.0, "HIGH", note)

    # --- P10(b): STOCK / (STOCK TOL.) qualifier ---
    # A stock dimension is the finished stock envelope with a loose mill tolerance.
    # Take its value as authoritative (finished envelope by default) and record the
    # stock qualifier as metadata — it is exempt from tight-tolerance ambiguity
    # routing (A001621E 3.50, .50 must not be flagged as tight-tol conflicts).
    if cands and _is_stock_dim(dim):
        note = (f"{dim_id} = {_fmt(cands[0])} carries a STOCK/(STOCK TOL.) qualifier — treated as "
                f"the finished stock envelope; the loose stock tolerance is recorded as metadata "
                f"and exempt from tight-tolerance flags.")
        return DimResolution(dim_id, cands[0], False, "stock_dimension", [], 0.90, "HIGH", note)

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

    # --- STEP 1.5: decimal-plausibility (P6/P10, 2026-07-10) ---
    # When several readings survive (a decimal-placement ambiguity like "312" ->
    # .312 / 3.12 / 31.2, or genuine multi-readings), choose by PLAUSIBILITY —
    # sheet magnitude/formatting consistency + standard drill/stock tiebreaks —
    # NOT by "most conservative" (which is just a differently-shaped guess). A
    # clear winner resolves MEDIUM; a tie stays CRITICAL and gates the feature.
    plaus = _decimal_plausibility(dim, model)
    if plaus is not None:
        return plaus

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
def _resolve_feature(feat: dict, model: DrawingData,
                     commit_mode: bool = COMMIT_MODE_DEFAULT) -> FeatureResolution:
    fid = feat.get("id", "?")
    position_known = bool(feat.get("position_known"))
    has_offset = bool(feat.get("offset_x") or feat.get("offset_y"))

    # A hole/thread feature is positioned by its callout: if the callout carries
    # explicit instance positions or position_known, the location IS resolved.
    ftype = (feat.get("type") or "").lower()
    if ftype in ("hole", "thread"):
        h = model.hole_callout_for_feature(fid)
        if h is not None and (
            h.instance_positions or getattr(h, "position_known", False)
            # A bolt-circle / multi-instance pattern IS positioned by its bolt
            # circle — do NOT route it to position review (would wrongly exclude
            # a fully-specified circular pattern, e.g. must-meet F003, under P3).
            or (getattr(h, "bolt_circle_diameter", 0) or 0) > 0
            or int(getattr(h, "qty", 1) or 1) >= 2
        ):
            position_known = True

    if position_known or has_offset:
        return FeatureResolution(
            fid, "build", True, "",
            "HIGH",
            f"{fid} position read from the drawing — no action needed.",
        )

    # Bug 1 (2026-07-11): CONSUME any extracted positional evidence BEFORE any
    # escalation. Positional applies_to labels (slot_offset / hole_position_x /
    # position) canonicalize to "" so they were invisible to the token machinery
    # and the location was dropped on the floor. Read it directly here, write it
    # into the feature's offsets, and treat the position as resolved.
    pos = _feature_positional_xy(feat, model)
    if pos is not None:
        px, py, dim_ids = pos
        feat["offset_x"], feat["offset_y"] = round(px, 6), round(py, 6)
        feat["position_known"] = True
        src = "slot anchor" if any(s.id == fid for s in model.slot_cuts) else \
              f"associated dimension(s) {', '.join(dim_ids)}"
        return FeatureResolution(
            fid, "build", True, "",
            "HIGH",
            f"{fid} position read from the drawing ({src}) — ({_fmt(px)}, {_fmt(py)}).",
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
    # P3 (2026-07-10): the parent-center LAST-RESORT guess is gone. A cut/hole
    # whose position is genuinely unknown was centered before and could land
    # entirely off the solid (A001211E F004). A knowingly-unplaceable feature is
    # now EXCLUDED from the build by the completeness gate and surfaced as a
    # Tab-3 model-derived assumption — a missing feature flagged loudly beats a
    # hole in the wrong place. The attempts made before giving up are logged.
    attempts = _log_position_attempts(feat, model)
    if commit_mode:
        # Commit-to-extraction: no human in the loop, so we never leave a feature
        # unplaced. Commit a geometrically conservative inside-parent placement,
        # written into the feature's offsets (never [0,0]), and BUILD it — flagged
        # CRITICAL with the basis and the empty rungs. The correction loop refines.
        cx, cy = _conservative_xy(feat, model)
        feat["offset_x"], feat["offset_y"] = cx, cy
        feat["position_known"] = True
        return FeatureResolution(
            fid, "build", False, "committed_conservative", "CRITICAL",
            f"POSITION COMMITTED for {fid} at a conservative inside-parent placement "
            f"({_fmt(cx)}, {_fmt(cy)}): no location dimension and no symmetry evidence. "
            f"Tried: {attempts}. Built and flagged (never left at a [0,0] placeholder) — "
            f"verify/correct the location in SolidWorks.",
        )
    return FeatureResolution(
        fid, "build", False, "needs_markup_review", "CRITICAL",
        f"POSITION UNRESOLVED for {fid}: no location dimension and no symmetry "
        f"evidence on the drawing. Tried: {attempts}. It will be EXCLUDED from the "
        f"model (not placed at a guessed center) and recorded as a Tab-3 model-derived "
        f"assumption — add an X and/or Y location dimension on the drawing and re-run.",
    )


def _log_position_attempts(feat: dict, model: DrawingData) -> str:
    """P3(d): before declaring a position UNRESOLVED, record which inference
    routes were attempted and why they failed, so the next learning-loop cycle can
    tell a genuinely undimensioned feature from an association miss."""
    fid = feat.get("id", "?")
    notes: list[str] = []
    # 1) extension-line tracing from any unassociated linear dimension.
    unassoc = [d for d in model.dimensions
               if (d.canonical_applies_to in ("length", "width") and d.value > 0
                   and fid not in (getattr(d, "feature_ref", "") or ""))]
    notes.append(f"extension-line tracing from {len(unassoc)} unassociated linear dim(s) "
                 f"(no leader reached {fid}'s centerline)")
    # 2) symmetry inference from centerline marks.
    notes.append("symmetry inference (no centerline/symmetry relationship names this feature)")
    log.info("position attempts for %s: %s", fid, "; ".join(notes))
    return "; ".join(notes)


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


def _normalize_revolves_to_extrudes(resolved: dict, result: "ResolutionResult") -> list[dict[str, Any]]:
    """Operator rule (2026-07-10): CIRCULAR geometry is ALWAYS built by sketching
    a CIRCLE and EXTRUDING it — never by revolving a rectangle profile about an
    axis. Convert every ``revolve`` feature into a circular ``extrude_boss``:

      * diameter = 2 × the largest profile radius (the bounding cylinder);
      * depth    = the profile's axial span.

    A constant-radius profile (a plain cylinder / disc / flange — exactly the
    "rectangle revolved" case) converts EXACTLY. A stepped/tapered profile becomes
    its bounding cylinder and is flagged MEDIUM so the filled steps are reviewed
    (a complete approximate solid beats a turned feature the shop rule forbids).

    Runs before the buildable-base pass, so the single resolved extraction — and
    therefore the VBA macros, the CadQuery pre-check, and the SolidWorks COM build
    — all see circle+extrude and no revolve survives downstream."""
    units = resolved.get("units") or "inch"
    flags: list[dict[str, Any]] = []
    for feat in resolved.get("features", []) or []:
        if (feat.get("type") or "").lower() != "revolve":
            continue
        profile = feat.get("revolve_profile") or []
        pts = [(float(p[0]), float(p[1])) for p in profile
               if isinstance(p, (list, tuple)) and len(p) == 2]
        radials = [r for _, r in pts if r > 0]
        axials = [a for a, _ in pts]
        fid = feat.get("id", "?")
        if not radials or not axials:
            continue  # no usable profile — leave as-is (rare); gate/builder handle it
        max_r, min_r = max(radials), min(radials)
        axial_span = max(axials) - min(axials)
        if axial_span <= 0 or max_r <= 0:
            continue
        diameter = round(2.0 * max_r, 6)
        depth = round(axial_span, 6)
        stepped = (max_r - min_r) > 1e-6

        # Convert the feature to a circular extrude.
        feat["type"] = "extrude_boss"
        feat["revolve_profile"] = []
        feat["position_known"] = False
        feat["offset_x"] = 0.0
        feat["offset_y"] = 0.0

        dia_id = _next_dim_id(resolved)
        note_d = (f"{dia_id}={_fmt(diameter)} {units}: revolve {fid} built as a circular extrude "
                  f"(Ø{_fmt(diameter)} × {_fmt(depth)}) "
                  + ("— exact for this constant-diameter part."
                     if not stepped else
                     "— bounding cylinder of a stepped/tapered profile; verify the steps."))
        dres_d = DimResolution(dia_id, diameter, True, "revolve_to_extrude", [],
                               0.9 if not stepped else 0.6,
                               "HIGH" if not stepped else "MEDIUM", note_d)
        dia_dim = {"id": dia_id, "type": "diameter", "value": diameter, "unit": units,
                   "applies_to": "diameter", "feature_ref": fid,
                   "notes": "Synthesized: revolve converted to a circle+extrude (shop rule)."}
        dia_dim.update(dres_d.as_fields())
        resolved.setdefault("dimensions", []).append(dia_dim)
        result.dim_resolutions[dia_id] = dres_d

        dep_id = _next_dim_id(resolved)
        dres_p = DimResolution(dep_id, depth, True, "revolve_to_extrude", [], 0.9, "HIGH",
                               f"{dep_id}={_fmt(depth)} {units}: axial length of revolve {fid} "
                               f"used as the extrude depth.")
        dep_dim = {"id": dep_id, "type": "depth", "value": depth, "unit": units,
                   "applies_to": "thickness", "feature_ref": fid,
                   "notes": "Synthesized: revolve axial length -> extrude depth."}
        dep_dim.update(dres_p.as_fields())
        resolved.setdefault("dimensions", []).append(dep_dim)
        result.dim_resolutions[dep_id] = dres_p
        feat["depth_dimension_id"] = dep_id

        rel = feat.setdefault("related_dimensions", [])
        for nid in (dia_id, dep_id):
            if nid not in rel:
                rel.append(nid)

        if stepped:
            flags.append({
                "dimension_id": fid, "feature_id": fid, "flag_tier": "MEDIUM",
                "human_note": (f"{fid}: turned profile built as a circular EXTRUDE (Ø{_fmt(diameter)} × "
                               f"{_fmt(depth)}, bounding cylinder) per the circle+extrude rule — the "
                               f"stepped/tapered detail is not modeled; verify against the drawing."),
                "macro_behavior": behavior_for_tier("MEDIUM"),
                "resolved_by_tier": TIER_PER_VIEW, "source": "revolve_to_extrude",
            })
        log.info("Converted revolve %s -> circular extrude_boss (Ø%.4f x %.4f, %s)",
                 fid, diameter, depth, "stepped/approx" if stepped else "exact")
    return flags


_TYP_RE = _re.compile(r"\btyp(ical)?\b", _re.IGNORECASE)


def _dim_is_typ(dim: dict) -> bool:
    """True if a dimension is marked TYP/TYPICAL (the value repeats on sibling
    features). Extraction may put the qualifier in notes, ambiguity_reason, or
    the applies_to/label text — scan them all."""
    blob = " ".join(str(dim.get(k, "")) for k in
                    ("notes", "ambiguity_reason", "applies_to", "label", "raw_text"))
    return bool(_TYP_RE.search(blob))


_STOCK_RE = _re.compile(r"\bstock\b", _re.IGNORECASE)


def _is_stock_dim(dim: dict) -> bool:
    """P10(b): True if a dimension carries a STOCK / (STOCK TOL.) qualifier — a
    finished stock-envelope value with a loose mill tolerance (A001621E 3.50, .50)."""
    blob = " ".join(str(dim.get(k, "")) for k in
                    ("notes", "ambiguity_reason", "applies_to", "label", "raw_text"))
    return bool(_STOCK_RE.search(blob))


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
                f"Do not build on this guess — it is surfaced as a Tab-3 model-derived assumption; "
                f"confirm the diameter callout on the drawing and re-run."),
            "macro_behavior": behavior_for_tier("CRITICAL"),
            "resolved_by_tier": TIER_PER_VIEW,
            "source": "illegible_dimension",
            "model_derived_assumption": True,
            "route_to_markup": True,
        })
    return flags


# --------------------------------------------------------------------------- #
# P1 (2026-07-10) — Universal missing-dimension completeness gate
#
# REGRESSION ROOT CAUSE. The prior fixes (Fix 2.1 dimensionless fillet/chamfer,
# Fix 2.4 incomplete cut profile) only APPENDED a CRITICAL flag. Per this
# module's stated invariant ("Every feature gets build_status == 'build'. No
# skip/defer/omit."), the feature kept build_status='build' and stayed in
# build_order, so macro_generator AND the COM builder still emitted/attempted it
# — A001821M's chamfer F005 skipped/failed on six consecutive runs, and
# A001211E's dimensionless hole/pattern reached the build and failed there. The
# flag never gated. This gate closes the path: a feature whose driving dimension
# is genuinely missing after every resolution step is REMOVED from build_order
# (the single choke point macro_generator, solidworks_builder, and the CadQuery
# build plan all iterate), so it can never appear as a build/macro step, and is
# surfaced instead as a Tab-3 model-derived assumption. Base bodies are never
# excluded (an excluded base = no model at all — the worst outcome); their
# thickness is synthesized upstream by _ensure_buildable_extrudes.
# --------------------------------------------------------------------------- #
_PROFILE_CUT_TYPES = ("extrude_cut", "slot", "cutout", "pocket",
                      "counterbore", "countersink")
_PATTERN_TYPES = ("circular_pattern", "linear_pattern", "pattern")
# What each feature type must have before it can be sketched/built.
_DRIVING_LABEL = {
    "fillet": "radius", "chamfer": "distance",
    "hole": "diameter", "thread": "diameter",
}


def _present_tokens(feat: dict, dims_by_id: dict) -> set[str]:
    """Canonical applies_to tokens the feature actually carries a value for."""
    rel = list(feat.get("related_dimensions", []) or [])
    did = feat.get("depth_dimension_id")
    if did:
        rel.append(did)
    toks: set[str] = set()
    for rid in rel:
        d = dims_by_id.get(rid) or {}
        # A value that is present but STILL AMBIGUOUS (decimal placement could not
        # be resolved, P6) does not count as a usable driving dimension — the gate
        # then excludes the feature rather than build on an unresolved guess.
        if d.get("assumption_basis") == "ambiguous_multi_candidate":
            continue
        if (d.get("value") or 0) > 0:
            t = canonicalize_applies_to(d.get("applies_to", ""))
            if t:
                toks.add(t)
    return toks


# Thread designations → nominal major diameter (inch). A thread callout names an
# unambiguous standard size, so it is a valid step-3 "standard-size" substitution
# for a hole that never had a diameter read (marked inferred, human verifies).
_UNC_MAJOR_IN = {
    "0": 0.060, "1": 0.073, "2": 0.086, "3": 0.099, "4": 0.112, "5": 0.125,
    "6": 0.138, "8": 0.164, "10": 0.190, "12": 0.216,
}
_THREAD_METRIC = _re.compile(r"\bM\s*(\d+(?:\.\d+)?)\s*(?:x\s*[\d.]+)?\b", _re.I)
_THREAD_FRAC = _re.compile(r"\b(\d+)\s*/\s*(\d+)\s*[-\s]\s*\d+\b")
_THREAD_NUM = _re.compile(r"\b#?(\d+)\s*[-]\s*\d+\b")


def _thread_major_diameter(text: str, units: str) -> Optional[float]:
    """Nominal major diameter for a thread callout, in DRAWING units, or None."""
    if not text:
        return None
    is_mm = str(units).lower().startswith("mm")
    m = _THREAD_METRIC.search(text)
    if m:
        d_mm = float(m.group(1))
        return d_mm if is_mm else round(d_mm / 25.4, 4)
    m = _THREAD_FRAC.search(text)
    if m:
        d_in = float(m.group(1)) / float(m.group(2))
        return round(d_in * 25.4, 4) if is_mm else round(d_in, 4)
    m = _THREAD_NUM.search(text)
    if m and m.group(1) in _UNC_MAJOR_IN:
        d_in = _UNC_MAJOR_IN[m.group(1)]
        return round(d_in * 25.4, 4) if is_mm else d_in
    return None


def _synthesize_dim(resolved: dict, result: "ResolutionResult", feat: dict, *,
                    value: float, applies_to: str, basis: str, tier: str,
                    note: str, dim_type: str = "linear", confidence: float = 0.4) -> str:
    """Create + link an inferred dimension for ``feat`` (numbers are chosen from
    derivation, never invented arbitrarily). Returns the new dimension id."""
    new_id = _next_dim_id(resolved)
    val = round(float(value), 6)
    new_dim = {
        "id": new_id, "type": dim_type, "value": val,
        "unit": (resolved.get("units") or "inch"), "applies_to": applies_to,
        "feature_ref": feat.get("id", "?"),
        "notes": f"Inferred by Stage 2.5 completeness gate ({basis}).",
    }
    dres = DimResolution(new_id, val, True, basis, [], confidence, tier, note)
    new_dim.update(dres.as_fields())
    resolved.setdefault("dimensions", []).append(new_dim)
    result.dim_resolutions[new_id] = dres
    rel = feat.setdefault("related_dimensions", [])
    if new_id not in rel:
        rel.append(new_id)
    return new_id


def _derive_from_chain(resolved: dict, model: DrawingData, result: "ResolutionResult",
                       feat: dict, needed_tokens: tuple[str, ...]) -> bool:
    """Resolution step 1 — derive a missing driving dimension from the constraint
    graph: a dimension chain with exactly one unknown component closes to
    ``total - sum(known)`` (reuses the closure machinery). If that solved value
    belongs to a dimension applying to one of ``needed_tokens`` and the feature
    references it, fill it in. Conservative: only fires on a single clean unknown."""
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    rel = set(feat.get("related_dimensions", []) or [])
    try:
        chains = list(model.relationships.dimension_chains)
    except AttributeError:
        return False
    for chain in chains:
        total = dims_by_id.get(chain.total_dimension_id)
        comp_ids = list(chain.component_dimension_ids)
        if total is None or not (total.get("value") or 0) > 0:
            continue
        comps = [dims_by_id.get(cid) for cid in comp_ids]
        if any(c is None for c in comps):
            continue
        unknown = [c for c in comps if not (c.get("value") or 0) > 0]
        if len(unknown) != 1:
            continue
        u = unknown[0]
        if canonicalize_applies_to(u.get("applies_to", "")) not in needed_tokens:
            continue
        if u.get("id") not in rel:
            continue  # only derive a value the feature actually consumes
        known_sum = sum(float(c.get("value") or 0) for c in comps if c is not u)
        solved = round(float(total["value"]) - known_sum, 6)
        if solved <= 0:
            continue
        u["value"] = solved
        u["value_unclear"] = False
        u["resolution_required"] = False
        note = (f"{u.get('id')}={_fmt(solved)} derived from chain "
                f"{chain.total_dimension_id}-Σcomponents — the only unknown span; verify.")
        dres = DimResolution(u.get("id", "?"), solved, True, "arithmetic_chain",
                             [chain.total_dimension_id, *comp_ids], 0.8, "HIGH", note)
        u.update(dres.as_fields())
        result.dim_resolutions[u.get("id", "?")] = dres
        return True
    return False


def _expected_region(feat: dict) -> dict:
    """Best-effort drawing-frame region for a Tab-3 assumption flag — the feature's
    read offsets when known, else its host, never fabricated pixel coordinates."""
    if feat.get("position_known"):
        return {"kind": "offset", "x": feat.get("offset_x", 0.0), "y": feat.get("offset_y", 0.0)}
    if feat.get("parent_feature"):
        return {"kind": "on_parent", "parent_feature": feat.get("parent_feature")}
    return {"kind": "undimensioned"}


def _region_text(feat: dict) -> str:
    r = _expected_region(feat)
    if r["kind"] == "offset":
        return f"Expected near drawing-frame ({_fmt(r['x'])}, {_fmt(r['y'])})."
    if r["kind"] == "on_parent":
        return f"Expected on host feature {r['parent_feature']}."
    return "The drawing did not dimension its location."


# --------------------------------------------------------------------------- #
# Commit-to-extraction helpers (see COMMIT_MODE_DEFAULT near the top).
# --------------------------------------------------------------------------- #
# RAW applies_to substrings that mark a dimension as a LOCATION (canonical
# applies_to returns "" for all of these, which is exactly why the position was
# being dropped — Bug 1). Detected on the raw label, not the canonical token.
_POS_X_HINTS = ("position_x", "offset_x", "x_position", "pos_x", "slot_offset")
_POS_Y_HINTS = ("position_y", "offset_y", "y_position", "pos_y")
_POS_ANY_HINTS = ("position", "offset", "location", "anchor")


def _dim_is_positional(applies_to: str) -> tuple[bool, str]:
    """(is_positional, axis) for a raw applies_to label. axis is 'x', 'y', or
    '' (unknown/either)."""
    a = (applies_to or "").lower()
    if any(h in a for h in _POS_X_HINTS) or a.endswith("_x"):
        return True, "x"
    if any(h in a for h in _POS_Y_HINTS) or a.endswith("_y"):
        return True, "y"
    if any(h in a for h in _POS_ANY_HINTS):
        return True, ""
    return False, ""


def _feature_positional_xy(feat: dict, model: DrawingData) -> Optional[tuple[float, float, list[str]]]:
    """Consume any EXTRACTED positional evidence for a feature into an (x, y)
    location BEFORE any escalation runs (Bug 1). Sources, in order:
      1. a ``slot_cuts`` record — its anchor_offset gives the near-edge location;
      2. related dimensions whose RAW applies_to marks them positional.
    Returns (x, y, dim_ids) or None when there is genuinely zero positional
    evidence. Missing axis defaults to 0 (edge of the part) rather than fabricated."""
    fid = feat.get("id", "")
    # 1) slot anchor
    slot = next((s for s in model.slot_cuts if s.id == fid), None)
    if slot is not None:
        edge = (slot.open_edge or "").lower()
        a = float(slot.anchor_offset)
        if slot.anchor_semantics == "edge_to_centerline":
            a = a - float(slot.width) / 2.0
        if edge in ("left", "right", "top", "bottom", ""):
            x = a if edge in ("top", "bottom", "") else 0.0
            y = a if edge in ("left", "right") else 0.0
            return (x, y, [slot.anchor_dimension_id] if slot.anchor_dimension_id else [])
    # 2) associated positional dimensions
    dims_by_id = {d.id: d for d in model.dimensions}
    x = y = None
    used: list[str] = []
    for rid in (feat.get("related_dimensions") or []):
        d = dims_by_id.get(rid)
        if d is None or not (d.value and d.value > 0):
            continue
        is_pos, axis = _dim_is_positional(d.applies_to or "")
        if not is_pos:
            continue
        used.append(rid)
        if axis == "x" and x is None:
            x = float(d.value)
        elif axis == "y" and y is None:
            y = float(d.value)
        elif axis == "" and x is None:
            x = float(d.value)
    if x is not None or y is not None:
        return (x or 0.0, y or 0.0, used)
    return None


def _conservative_xy(feat: dict, model: DrawingData) -> tuple[float, float]:
    """A geometrically conservative in-plane placement for a feature with NO
    positional evidence: a quarter-inset from the lower-left corner of the
    envelope, which keeps a modest feature fully inside the parent (the
    intersection precheck / correction loop refine it). Never [0,0]."""
    length = width = 0.0
    for d in model.dimensions:
        a = (d.canonical_applies_to or "").lower()
        if a == "length":
            length = max(length, float(d.value or 0))
        elif a in ("width", "height"):
            width = max(width, float(d.value or 0))
    return (round((length or 4.0) * 0.25, 4), round((width or 4.0) * 0.25, 4))


def _sibling_diameter(feat: dict, resolved: dict) -> Optional[float]:
    """The most common hole diameter among the part's OTHER holes — a hole
    marked '(2) HOLES' / 'second instance' inherits its sibling's diameter
    (M_121-B F005) before any standard-size fallback."""
    fid = feat.get("id")
    dias: list[float] = []
    for h in resolved.get("hole_callouts", []) or []:
        if h.get("feature_ref") != fid and (h.get("diameter") or 0) > 0:
            dias.append(round(float(h["diameter"]), 4))
    for d in resolved.get("dimensions", []) or []:
        if d.get("feature_ref") != fid and (d.get("value") or 0) > 0 \
                and canonicalize_applies_to(d.get("applies_to", "")) in ("diameter", "hole_diameter"):
            dias.append(round(float(d["value"]), 4))
    if not dias:
        return None
    from collections import Counter
    return Counter(dias).most_common(1)[0][0]


def _derive_profile_delta(resolved: dict, model: DrawingData, result: "ResolutionResult",
                          feat: dict, need: tuple[str, ...]) -> bool:
    """Task 3 — derive a step/notch cut's rectangle from the OUTER PROFILE
    envelope minus the cut's partial anchor dims, instead of requiring
    feature-local length+width callouts.

    A step cut removes a rectangular region between an anchor (an extracted
    position/partial-height dim it carries) and an envelope edge:
        missing width  -> length_envelope - x_anchor
        missing length -> width_envelope  - y_anchor (partial height)
    When an anchor for an axis is absent, the span defaults to the full envelope
    on that axis (a conservative full-edge step). Fills the missing dims tagged
    ``profile_delta`` and flagged derived. Returns True if it filled anything."""
    length = width = 0.0
    for d in model.dimensions:
        a = (d.canonical_applies_to or "").lower()
        if a == "length":
            length = max(length, float(d.value or 0))
        elif a in ("width", "height"):
            width = max(width, float(d.value or 0))
    if not (length and width):
        return False

    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    # Partial anchors the feature already carries (positional or partial spans).
    x_anchor = y_anchor = None
    for rid in (feat.get("related_dimensions") or []):
        d = dims_by_id.get(rid) or {}
        v = float(d.get("value") or 0)
        if v <= 0:
            continue
        is_pos, axis = _dim_is_positional(d.get("applies_to", ""))
        canon = canonicalize_applies_to(d.get("applies_to", ""))
        if (is_pos and axis == "x") and x_anchor is None:
            x_anchor = v
        elif (is_pos and axis == "y") and y_anchor is None:
            y_anchor = v
        elif canon == "height" and y_anchor is None:
            # a partial height splits the envelope: the step spans the remainder
            y_anchor = v

    filled = False
    units = resolved.get("units") or "inch"
    if "width" in need:
        w = round(length - x_anchor, 6) if (x_anchor and length - x_anchor > 0) else round(length, 6)
        _synthesize_dim(resolved, result, feat, value=w, applies_to="width",
                        basis="profile_delta", tier="CRITICAL", confidence=0.5,
                        note=(f"{feat.get('id')} width={_fmt(w)} {units} DERIVED from the outer "
                              f"profile (envelope length {_fmt(length)} − step anchor "
                              f"{_fmt(x_anchor or 0)}); no feature-local width callout. Verify."))
        filled = True
    if "length" in need:
        ln = round(width - y_anchor, 6) if (y_anchor and width - y_anchor > 0) else round(width, 6)
        _synthesize_dim(resolved, result, feat, value=ln, applies_to="length",
                        basis="profile_delta", tier="CRITICAL", confidence=0.5,
                        note=(f"{feat.get('id')} length={_fmt(ln)} {units} DERIVED from the outer "
                              f"profile (envelope height {_fmt(width)} − partial "
                              f"{_fmt(y_anchor or 0)}); no feature-local length callout. Verify."))
        filled = True
    return filled


def _pattern_covered_by_parent(feat: dict, resolved: dict, model: DrawingData) -> Optional[tuple[str, int]]:
    """Task 3 (2026-07-12, 158-C F004) — callout-arithmetic reconciliation
    BEFORE any feature reaches exclusion. A ``pattern``/``mirror`` feature whose
    ``parent_feature`` is a hole already carrying a callout with
    ``qty >= this feature's own quantity`` corresponds to NOTHING on the drawing
    beyond what the parent already built: the sheet's hole accounting (e.g.
    "6-HLS") is already satisfied by the parent's instances. Returns
    ``(parent_id, qty)`` when this feature is such a phantom duplicate, else
    ``None``. Mirrors ``macro_generator._pattern_covered_by`` (which runs too
    late — after the gate has already dropped the feature from build_order)."""
    parent_id = feat.get("parent_feature")
    if not parent_id:
        return None
    parent = model.feature_by_id(parent_id)
    if parent is None:
        return None
    h = next((h for h in resolved.get("hole_callouts", []) or [] if h.get("feature_ref") == parent_id), None)
    if h is None:
        return None
    qty = int(h.get("qty") or 1)
    want = max(int(feat.get("quantity") or 1), 1)
    if qty >= want and qty >= 2:
        return parent_id, qty
    return None


# Defense-in-depth (2026-07-12 Task 3): if extraction guidance is ever missed
# and a BOM/balloon/applied-item note IS synthesized as a feature anyway, this
# backstop catches it by its own description text — independent of whether it
# has a matching parent to be "covered by".
_METADATA_ONLY_WORDS = ("bom item", "bill of material", "applied per bom",
                        "purchased item", "balloon callout", "weatherstrip",
                        "weather stripping", "mcmaster-carr", "sponge rubber")


def _is_metadata_only_feature(feat: dict) -> bool:
    desc = (str(feat.get("description", "")) + " " + str(feat.get("notes", ""))).lower()
    return any(w in desc for w in _METADATA_ONLY_WORDS)


def _reconcile_phantom_duplicate(feat: dict, resolved: dict, model: DrawingData,
                                 result: "ResolutionResult") -> bool:
    """If ``feat`` corresponds to nothing beyond an already-built sibling (a
    duplicate pattern), or is itself a BOM/balloon/applied-item note that was
    mistakenly synthesized as a feature, reclassify it (never EXCLUDED, never
    an open item) and return True. The feature is removed from build_order
    (nothing new to draw) but its disposition is a distinct phantom state, not
    EXCLUDED_INCOMPLETE — the difference the checklist/reconciliation report
    must represent explicitly."""
    fid = feat.get("id", "?")
    covered = _pattern_covered_by_parent(feat, resolved, model)
    if covered is not None:
        parent_id, qty = covered
        note = (f"{fid} corresponds to nothing on the drawing beyond feature {parent_id}: the "
                f"sheet's hole accounting ({qty}-place callout) is already fully satisfied by "
                f"{parent_id}'s built instances. Reclassified as a duplicate — not excluded, "
                f"not an open item.")
        duplicate_of = parent_id
    elif _is_metadata_only_feature(feat):
        note = (f"{fid} ('{feat.get('description', '')[:80]}') reads as a BOM/balloon/applied-item "
                f"note, not geometry — reclassified as metadata, not excluded, not an open item.")
        duplicate_of = ""
    else:
        return False

    feat["build_status"] = "duplicate_reclassified"
    fr = result.feature_resolutions.get(fid)
    if fr is not None:
        fr.build_status = "duplicate_reclassified"
    flag = {
        "dimension_id": fid, "feature_id": fid, "flag_tier": "LOW",
        "human_note": note, "macro_behavior": "comment_only", "resolved_by_tier": TIER_PER_VIEW,
        "source": "phantom_duplicate",
    }
    if duplicate_of:
        flag["duplicate_of"] = duplicate_of
    result.flags.append(flag)
    log.info("phantom reconciliation: %s reclassified (%s)", fid,
             f"duplicate of {duplicate_of}" if duplicate_of else "metadata-only")
    return True


def _commit_missing_dim(resolved: dict, model: DrawingData, result: "ResolutionResult",
                        feat: dict, token: str, tried: str) -> None:
    """Last-resort commit-mode fill for a missing driving dimension: a
    geometrically conservative value (a modest fraction of the envelope), applied
    and built, with a CRITICAL flag naming the value, the basis, and the empty
    rungs. Never excludes."""
    length = width = 0.0
    for d in model.dimensions:
        a = (d.canonical_applies_to or "").lower()
        if a == "length":
            length = max(length, float(d.value or 0))
        elif a in ("width", "height"):
            width = max(width, float(d.value or 0))
    base = min([v for v in (length, width) if v] or [1.0])
    if token in ("fillet_radius", "chamfer"):
        # An edge treatment committed at the same 25% envelope fraction as a
        # bore/rectangle would be a comically oversized fillet/chamfer — use a
        # small, shop-typical default instead (a light break, not a feature).
        value = round(min(0.0625, max(0.01, base * 0.01)), 4)
    else:
        value = round(max(0.1, base * 0.25), 4)
    units = resolved.get("units") or "inch"
    dim_type = ("diameter" if token in ("diameter", "hole_diameter")
               else "radial" if token == "fillet_radius" else "linear")
    _synthesize_dim(resolved, result, feat, value=value, applies_to=token,
                    basis="committed_conservative", tier="CRITICAL", confidence=0.2,
                    dim_type=dim_type,
                    note=(f"{feat.get('id')} {token}={_fmt(value)} {units} COMMITTED (conservative "
                          f"{'shop-typical edge-break' if token in ('fillet_radius', 'chamfer') else 'envelope fraction'}) "
                          f"so the feature builds. No value was read or derived ({tried}). Built and "
                          f"flagged — verify/correct in SolidWorks."))


# --------------------------------------------------------------------------- #
# Hole-group classification + datum-chain provenance (2026-07-12, A001271E)
# --------------------------------------------------------------------------- #
_EDGE_WORDS = ("left edge", "right edge", "top edge", "bottom edge", "edge to", "from edge")
_HOLE_ANCHOR_WORDS = ("between", "spacing", "pair", "stagger", "hole center", "centerline",
                      "hole to", "column", "adjacent")
_X_WORDS = ("horizontal", "left", "right", "length", "x ", "across", "column")
_Y_WORDS = ("vertical", "top", "bottom", "height", "y ", "down", "up ")


def _anchor_of(dim: dict) -> tuple[str, str]:
    """(anchor, axis) inferred from a positional/spacing dimension's notes +
    applies_to. anchor ∈ {left_edge,right_edge,top_edge,bottom_edge,hole_center,
    origin}; axis ∈ {x,y,''}."""
    note = (str(dim.get("notes", "")) + " " + str(dim.get("applies_to", ""))).lower()
    if "left edge" in note:
        anchor = "left_edge"
    elif "right edge" in note:
        anchor = "right_edge"
    elif "top edge" in note:
        anchor = "top_edge"
    elif "bottom edge" in note:
        anchor = "bottom_edge"
    elif any(w in note for w in _HOLE_ANCHOR_WORDS):
        anchor = "hole_center"
    elif any(w in note for w in _EDGE_WORDS):
        anchor = "edge"
    else:
        anchor = "origin"
    axis = "x" if any(w in note for w in _X_WORDS) else ("y" if any(w in note for w in _Y_WORDS) else "")
    return anchor, axis


def _classify_hole_groups(resolved: dict, model: DrawingData, result: "ResolutionResult") -> None:
    """Task 1/2 — for every hole feature record its placement classification
    (pattern vs individual, with the evidence used) and its datum-chain
    ``position_basis`` (each positional/spacing dim → its anchor). A callout is a
    verified pattern only with uniform pitch or a bolt circle; everything else is
    individual (each instance owns its coordinate). Hole-to-hole anchors record a
    ``DP_<fid>`` datum point. Additive: writes only to ``result.hole_placements``,
    never mutating the schema-clean feature dicts."""
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    hole_feats = [f for f in resolved.get("features", []) or []
                  if (f.get("type") or "").lower() in ("hole", "thread")]
    # group by nominal diameter (from the linked diameter dim / callout)
    def _dia(f):
        for rid in (f.get("related_dimensions") or []):
            d = dims_by_id.get(rid) or {}
            if canonicalize_applies_to(d.get("applies_to", "")) in ("diameter", "hole_diameter") \
                    and (d.get("value") or 0) > 0:
                return round(float(d["value"]), 4)
        h = next((h for h in resolved.get("hole_callouts", []) or []
                  if h.get("feature_ref") == f.get("id")), None)
        return round(float(h.get("diameter", 0)), 4) if h else 0.0

    groups: dict[float, list[dict]] = {}
    for f in hole_feats:
        groups.setdefault(_dia(f), []).append(f)

    for dia, feats in groups.items():
        # verified pattern? a callout in the group with uniform pitch / bolt circle
        evidence = "none->individual"
        for f in feats:
            h = next((h for h in resolved.get("hole_callouts", []) or []
                      if h.get("feature_ref") == f.get("id")), None)
            if h is None:
                continue
            if (h.get("bolt_circle_diameter") or 0) > 0:
                evidence = f"bolt_circle_{_fmt(h['bolt_circle_diameter'])}"
            elif (h.get("pattern_spacing") or 0) > 0 and int(h.get("qty") or 1) >= 2 \
                    and len(feats) <= 1:
                evidence = f"uniform_pitch_{_fmt(h['pattern_spacing'])}"
        # A multi-feature group is individual by construction (each feature is one
        # instance); a single feature may be a verified pattern.
        placement = "pattern" if (evidence != "none->individual" and len(feats) <= 1) else "individual"
        for f in feats:
            basis = []
            datum_points = []
            for rid in (f.get("related_dimensions") or []):
                d = dims_by_id.get(rid) or {}
                if not (d.get("value") and float(d["value"]) > 0):
                    continue
                canon = canonicalize_applies_to(d.get("applies_to", ""))
                is_pos, _ax = _dim_is_positional(d.get("applies_to", ""))
                if not (is_pos or canon == "spacing" or "offset" in (d.get("applies_to") or "").lower()
                        or "spacing" in (d.get("applies_to") or "").lower()):
                    continue
                anchor, axis = _anchor_of(d)
                basis.append({"anchor": anchor, "dim": rid,
                              "value": round(float(d["value"]), 6), "axis": axis})
                if anchor == "hole_center":
                    # Align to the reference-geometry naming contract (REF_PT_<fid>)
                    # so the build-plan record and the emitted 01a datum point match.
                    datum_points.append(f"REF_PT_{f.get('id')}")
            result.hole_placements[f.get("id", "?")] = {
                "placement": placement,
                "pattern_evidence": evidence,
                "diameter": dia,
                "position_basis": basis,
                "datum_points": sorted(set(datum_points)),
            }


def _completeness_gate(resolved: dict, model: DrawingData,
                       result: "ResolutionResult",
                       commit_mode: bool = COMMIT_MODE_DEFAULT) -> list[dict[str, Any]]:
    """P1 (2026-07-10) — the single unconditional completeness gate. Runs AFTER
    TYP propagation and buildable-base synthesis. For every buildable feature it
    checks the driving dimension(s) for its type and, when missing, applies the
    resolution order (constraint-graph derivation → standard-size substitution;
    TYP already ran). If still missing, the feature is EXCLUDED from build_order
    and surfaced as a Tab-3 model-derived assumption — never emitted as a doomed
    build/macro step. See the module-level regression note above."""
    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
    build_order = list(resolved.get("build_order")
                       or [f.get("id") for f in resolved.get("features", []) or []])
    # Holes/patterns often carry their driving values on the linked HOLE CALLOUT
    # (diameter, bolt-circle, spacing, qty), NOT on a related dimension — index
    # them by feature_ref so the gate does not wrongly exclude a fully-specified
    # hole whose diameter lives on its callout.
    holes_by_ref: dict[str, list[dict]] = {}
    for h in resolved.get("hole_callouts", []) or []:
        ref = h.get("feature_ref")
        if ref:
            holes_by_ref.setdefault(ref, []).append(h)
    flags: list[dict[str, Any]] = []
    # A canonical slot_cut carries its geometry on the slot record, not on the
    # feature's related_dimensions — never gate such a feature out for "missing"
    # dims; its slot decomposition (mandatory rectangle) owns the size/position.
    slot_feature_ids = {s.get("id") for s in resolved.get("slot_cuts", []) or []}

    for feat in resolved.get("features", []) or []:
        ftype = (feat.get("type") or "").lower()
        fid = feat.get("id", "?")
        if fid not in build_order:
            continue  # already dropped (e.g. validator) — nothing to gate
        if fid in slot_feature_ids:
            continue  # backed by a slot_cut — geometry lives on the slot record
        toks = _present_tokens(feat, dims_by_id)
        missing: Optional[tuple[str, str]] = None  # (what, source)

        if ftype == "fillet":
            if not (toks & {"fillet_radius", "radius"}):
                missing = ("radius", "missing_dimension")
        elif ftype == "chamfer":
            if "chamfer" not in toks:
                missing = ("distance", "missing_dimension")
        elif ftype in ("hole", "thread"):
            callout_dia = any((h.get("diameter") or 0) > 0 for h in holes_by_ref.get(fid, []))
            if not (toks & {"diameter", "hole_diameter"}) and not callout_dia:
                units = resolved.get("units") or "inch"
                blob = " ".join(str(feat.get(k, "")) for k in ("description", "notes"))
                dia = _thread_major_diameter(blob, units)
                sib = _sibling_diameter(feat, resolved) if commit_mode else None
                if dia:
                    _synthesize_dim(
                        resolved, result, feat, value=dia, applies_to="hole_diameter",
                        basis="standard_thread_size", tier="MEDIUM", dim_type="diameter",
                        confidence=0.55,
                        note=(f"{fid} diameter inferred as {_fmt(dia)} {units} from the thread "
                              f"callout (standard major diameter) — verify against the drawing."))
                    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
                elif sib:
                    # A '(2) HOLES' / 'second instance' hole inherits its sibling's
                    # diameter (M_121-B F005) — the value WAS extracted, on a peer.
                    _synthesize_dim(
                        resolved, result, feat, value=sib, applies_to="hole_diameter",
                        basis="sibling_hole", tier="MEDIUM", dim_type="diameter",
                        confidence=0.6,
                        note=(f"{fid} diameter inherited as {_fmt(sib)} {units} from a sibling hole "
                              f"of the same callout ('(N) HOLES') — verify against the drawing."))
                    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
                else:
                    missing = ("diameter", "missing_dimension")
        elif ftype in _PATTERN_TYPES:
            callouts = holes_by_ref.get(fid, [])
            count = max([int(feat.get("quantity") or 0)]
                        + [int(h.get("qty") or 0) for h in callouts])
            has_count = count >= 2
            has_spacing = ("spacing" in toks) or (feat.get("pattern_spacing") or 0) > 0 \
                or any((h.get("pattern_spacing") or 0) > 0
                       or (h.get("bolt_circle_diameter") or 0) > 0 for h in callouts) \
                or any((dims_by_id.get(r) or {}).get("value", 0) > 0
                       and canonicalize_applies_to((dims_by_id.get(r) or {}).get("applies_to", "")) == "spacing"
                       for r in (feat.get("related_dimensions", []) or []))
            if not (has_count and has_spacing):
                missing = ("both a spacing and an instance count (>=2)", "missing_pattern_param")
        elif ftype in _PROFILE_CUT_TYPES:
            has_dia = bool(toks & {"diameter", "hole_diameter"})
            has_rect = ("length" in toks) and ("width" in toks)
            if not (has_dia or has_rect):
                need = ("length", "width") if not has_dia else ()
                if need and _derive_from_chain(resolved, model, result, feat, need):
                    dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
                    toks = _present_tokens(feat, dims_by_id)
                    has_rect = ("length" in toks) and ("width" in toks)
                # Task 3: derive a step/notch rectangle from the outer-profile
                # envelope minus the cut's partial anchor dims (M_121-B F002/F003).
                if commit_mode and not (("length" in toks) and ("width" in toks)):
                    still = tuple(t for t in ("length", "width") if t not in toks)
                    if still and _derive_profile_delta(resolved, model, result, feat, still):
                        dims_by_id = {d.get("id"): d for d in resolved.get("dimensions", []) or []}
                        toks = _present_tokens(feat, dims_by_id)
                        has_rect = ("length" in toks) and ("width" in toks)
                if not (bool(toks & {"diameter", "hole_diameter"}) or has_rect):
                    have = sorted(t for t in toks if t)
                    miss = "width" if "length" in toks else ("length" if "width" in toks else "length+width")
                    missing = (f"a diameter OR both length+width (has {have}; missing {miss})",
                               "incomplete_profile")
        # extrude_boss/revolve base bodies: never excluded (thickness synthesized
        # upstream); any other type has no single driving dimension to gate on.

        # P3 (2026-07-10): a cut/hole whose position is genuinely UNRESOLVED is
        # excluded too — the parent-center last-resort guess is gone (it could
        # land entirely off the solid, A001211E F004). Symmetry-centered (LOW)
        # and dimensioned placements are kept and build normally.
        if (not commit_mode) and missing is None and ftype in ("hole",) + _PROFILE_CUT_TYPES:
            fr0 = result.feature_resolutions.get(fid)
            if fr0 is not None and getattr(fr0, "position_assumption", "") == "needs_markup_review":
                missing = ("a location (no X/Y dimension, no symmetry evidence)",
                           "position_unresolved")

        if missing is None:
            continue

        # Callout-arithmetic reconciliation (Task 3, 2026-07-12, 158-C F004) —
        # BEFORE any exclusion or commit decision: a feature whose parent already
        # accounts for it on the sheet (e.g. a "pattern" duplicating a hole
        # callout's own qty) corresponds to nothing new. Reclassify as a
        # duplicate, never excluded, never an open item, regardless of commit_mode.
        if _reconcile_phantom_duplicate(feat, resolved, model, result):
            build_order = [x for x in build_order if x != fid]
            continue

        what, source = missing

        # Commit-to-extraction: never EXCLUDE buildable geometry — of ANY type.
        # Fill the missing driving value(s) with a declared-basis conservative
        # committed value and BUILD it (flagged CRITICAL). Exclusion survives
        # only when commit_mode is OFF (comparison).
        if commit_mode:
            if ftype in ("hole", "thread"):
                tokens_to_commit = ["hole_diameter"]
            elif ftype in _PROFILE_CUT_TYPES:
                tokens_to_commit = [t for t in ("length", "width") if t not in toks] or ["length"]
            elif ftype == "fillet":
                tokens_to_commit = ["fillet_radius"]
            elif ftype == "chamfer":
                tokens_to_commit = ["chamfer"]
            elif ftype in _PATTERN_TYPES:
                # Nothing derivable and no covering parent (else the phantom-
                # duplicate check above would have handled it): commit a
                # conservative spacing so the pattern builds at its extracted
                # quantity rather than being dropped.
                tokens_to_commit = ["spacing"]
                if int(feat.get("quantity") or 1) < 2:
                    feat["quantity"] = 2
            else:  # pragma: no cover — every gated type is covered above
                tokens_to_commit = []
            for tok in tokens_to_commit:
                _commit_missing_dim(resolved, model, result, feat, tok, tried=f"missing {what}")
            log.warning("commit-mode COMMITTED %s (%s): %s via conservative value",
                        fid, ftype, what)
            continue

        build_order = [x for x in build_order if x != fid]
        feat["build_status"] = "excluded"
        fr = result.feature_resolutions.get(fid)
        if fr is not None:
            fr.build_status = "excluded"
        flags.append({
            "dimension_id": fid,
            "feature_id": fid,
            "flag_tier": "CRITICAL",
            "human_note": (
                f"EXCLUDED FROM BUILD — {ftype} {fid} is missing {what}. No value was "
                f"read, and none could be derived from the constraint graph, a TYP sibling, "
                f"or a standard size. Rather than emit a build step that fails or places wrong "
                f"geometry, {fid} was left OUT of the model and recorded as a model-derived "
                f"assumption in Tab 3. {_region_text(feat)} Add the missing value on the drawing "
                f"and re-run to include this feature."),
            "macro_behavior": behavior_for_tier("CRITICAL"),
            "resolved_by_tier": TIER_PER_VIEW,
            "source": source,
            "excluded_from_build": True,
            "model_derived_assumption": True,
            "expected_region": _expected_region(feat),
            # Legacy routing flag (internal only; no UI consumer) — kept True so
            # existing regression tests and downstream filters continue to work.
            "route_to_markup": True,
        })
        log.warning("completeness gate EXCLUDED %s (%s): missing %s", fid, ftype, what)

    resolved["build_order"] = build_order
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
                       overview_analysis: Optional[dict] = None,
                       human_answers: Optional[dict[str, float]] = None,
                       commit_mode: bool = COMMIT_MODE_DEFAULT) -> ResolutionResult:
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
        res = _resolve_dimension(dim, model, spec_vals, human_answers)
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
        fres = _resolve_feature(feat, model, commit_mode=commit_mode)
        result.feature_resolutions[fres.feature_id] = fres
        feat.update(fres.as_fields())

    # TYP propagation: a radius/chamfer callout marked TYP fills in dimensionless
    # sibling fillets/chamfers (before the buildable-base pass so any synthesized
    # dims are consistent).
    _propagate_typ(resolved, result)

    # Shop rule: circular geometry is ALWAYS a sketched circle + extrude, never a
    # rectangle revolved about an axis. Convert every revolve to a circular
    # extrude_boss BEFORE the buildable-base pass so the whole downstream (macros,
    # CadQuery, COM build) sees circle+extrude.
    _revolve_flags = _normalize_revolves_to_extrudes(resolved, result)

    # Guarantee a buildable base: synthesize a thickness for any extrude_boss the
    # drawing never dimensioned, so the part can never produce an empty solid.
    _ensure_buildable_extrudes(resolved, model, result)

    # Canonical slot / U-notch decomposition (2026-07-11): fold the legacy
    # extrude_cut+fillet pattern into a first-class slot_cut, then validate every
    # slot (fit / radius / anchor-semantics / through-all). A slot is always a
    # mandatory rectangle + deferred corner fillets — never an arc-bearing sketch.
    try:
        from pipeline.slot_cut import normalize_legacy_slots, validate_slot

        normalize_legacy_slots(resolved, result.flags.append)
        if resolved.get("slot_cuts"):
            model = DrawingData.model_validate(schema_clean(resolved))  # re-coerce (schema-clean)
            for slot in model.slot_cuts:
                for fl in validate_slot(slot, model):
                    fl.setdefault("macro_behavior", behavior_for_tier(fl["flag_tier"]))
                    fl.setdefault("resolved_by_tier", TIER_PER_VIEW)
                    result.flags.append(fl)
            # validate_slot may reclassify (obround) / set anchor_semantics —
            # persist those back into the resolved dict.
            resolved["slot_cuts"] = [s.model_dump() for s in model.slot_cuts]
    except Exception as e:
        log.warning("slot decomposition/validation failed (non-fatal): %s", e)

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
        # POSITION-UNRESOLVED features are owned by the completeness gate below
        # (it excludes them from the build and emits the single Tab-3 flag), so
        # skip them here to avoid a duplicate flag for the same feature.
        if fres.position_assumption == "needs_markup_review":
            continue
        if fres.flag_tier in ("MEDIUM", "LOW", "CRITICAL"):
            flag = {
                "dimension_id": fres.feature_id,
                "flag_tier": fres.flag_tier,
                "human_note": fres.human_note,
                "macro_behavior": behavior_for_tier(fres.flag_tier),
                "resolved_by_tier": TIER_PER_VIEW,
            }
            result.flags.append(flag)
    for hole in resolved.get("hole_callouts", []) or []:
        flag = _resolve_hole_position_flag(hole, "")
        if flag:
            flag["resolved_by_tier"] = TIER_PER_VIEW
            result.flags.append(flag)

    # P1 (2026-07-10) — universal completeness gate. Subsumes the old
    # dimensionless-feature (Fix 2.1) and incomplete-cut-profile (Fix 2.4) flag
    # passes, and CLOSES the regression: a feature still missing its driving
    # dimension after derivation/TYP/standard-size is EXCLUDED from build_order
    # (never emitted as a doomed macro/COM step) and surfaced as a Tab-3
    # model-derived assumption. Runs after TYP + buildable-base synthesis.
    result.flags.extend(_completeness_gate(resolved, model, result, commit_mode=commit_mode))
    # Task 1/2 (2026-07-12): classify each hole group pattern-vs-individual and
    # record per-instance datum-chain provenance (position_basis) for the build
    # plan. Never shares placement logic except for a verified regular pattern.
    try:
        _classify_hole_groups(resolved, model, result)
    except Exception as e:  # provenance is additive — never break a run
        log.warning("hole-group classification failed (non-fatal): %s", e)
    # Revolve->circle+extrude conversions that approximated a stepped/tapered
    # profile as a bounding cylinder surface as MEDIUM review items.
    result.flags.extend(_revolve_flags)
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
