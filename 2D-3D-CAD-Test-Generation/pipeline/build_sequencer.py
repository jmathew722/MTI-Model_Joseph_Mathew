"""Canonical seven-stage build sequencer (2026-07-10 redesign).

This module owns the ONE deterministic build-order pass for the pipeline. It
replaces the previous implicit ordering (extractor order + fillet/chamfer
deferral scattered in ``macro_generator``) with an explicit, staged,
completeness-based classification:

    Stage 0  reference geometry   (origin, planes, datum axes — no solid)
    Stage 1  base solid           (largest closed outer profile; exactly one)
    Stage 2  additive features    (secondary bosses / coaxial bodies)
    Stage 3  profile subtractions (notches/steps/slots — change outer topology)
    Stage 4  holes                (drilled/bored/tapped/cbore/csk)
    Stage 5  patterns             (linear/circular; reference a Stage-4 seed)
    Stage 6  edge treatments      (chamfers, then fillets — always last)
    Stage 7  non-geometric        (cosmetic threads, finish notes)

Design principle — *no type-based omission*. Every extracted feature ends in
exactly one of three states, recorded in a per-feature disposition table:

    BUILT                     — built from read values.
    BUILT_WITH_DERIVED_VALUE  — built using a constraint-graph / TYP / standard-
                                size value (flagged inferred by the resolver).
    EXCLUDED_INCOMPLETE       — excluded by the resolver completeness gate
                                because a driving dimension could not be
                                resolved (the specific missing parameter is
                                named in the resolver flag).

Category-based omission of holes and patterns does not happen here: a hole with
a resolved diameter and an X/Y position is fully autobuildable and is placed in
Stage 4. Exclusion is decided upstream by ``resolver._completeness_gate`` (which
names the missing parameter); this module only *reflects* that decision in the
disposition table — it never drops a feature for being a particular type.

Determinism: within every stage, features are ordered by an explicit stable key
that ends in the feature id, so two runs on the same extraction produce a
byte-identical ``build_order``. Cross-stage dependencies are satisfied by the
stage numbers themselves (a Stage-5 pattern always follows its Stage-4 seed; a
Stage-6 fillet always follows every Stage-3/4 cut).

The module is backend-agnostic and imports nothing from ``macro_generator`` /
``solidworks_builder`` / ``cq_prevalidate`` so it can be their single upstream.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from pipeline.schema import DrawingData, Feature, FeatureType

log = logging.getLogger(__name__)

# ── Stage identifiers ────────────────────────────────────────────────────────
STAGE_REFERENCE = 0
STAGE_BASE = 1
STAGE_ADDITIVE = 2
STAGE_PROFILE_CUT = 3
STAGE_HOLE = 4
STAGE_PATTERN = 5
STAGE_EDGE = 6
STAGE_NONGEOMETRIC = 7

STAGE_NAMES = {
    STAGE_REFERENCE: "reference_geometry",
    STAGE_BASE: "base_solid",
    STAGE_ADDITIVE: "additive_features",
    STAGE_PROFILE_CUT: "profile_subtractions",
    STAGE_HOLE: "holes",
    STAGE_PATTERN: "patterns",
    STAGE_EDGE: "edge_treatments",
    STAGE_NONGEOMETRIC: "non_geometric",
}

# ── Disposition states ───────────────────────────────────────────────────────
STATE_BUILT = "BUILT"
STATE_BUILT_DERIVED = "BUILT_WITH_DERIVED_VALUE"
STATE_EXCLUDED = "EXCLUDED_INCOMPLETE"

_BASE_TYPES = {FeatureType.EXTRUDE_BOSS, FeatureType.REVOLVE}
_EDGE_TYPES = {FeatureType.CHAMFER, FeatureType.FILLET}
_PATTERN_TYPES = {FeatureType.PATTERN, FeatureType.MIRROR}

# A DimResolution basis that means the value was read straight off the drawing
# (as opposed to derived from a chain / TYP sibling / standard size). Anything
# else on a feature's driving dimension marks it BUILT_WITH_DERIVED_VALUE.
_EXPLICIT_BASES = {
    "", "explicit_callout", "explicit", "as_read", "direct_reading",
    "direct", "read", "measured",
}
# A resolved position that counts as read (not assumed) — everything else marks
# the feature's placement as derived.
_READ_POSITIONS = {"", "dimensioned", "read", "known", "explicit"}


@dataclass
class Disposition:
    """One feature's final place + state in the build."""

    feature_id: str
    feature_type: str
    stage: int
    stage_name: str
    state: str
    sort_key: tuple
    values_used: dict[str, float] = field(default_factory=dict)
    derivation_source: str = ""
    position_xy: Optional[tuple[float, float]] = None
    flags: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "type": self.feature_type,
            "stage": self.stage,
            "stage_name": self.stage_name,
            "state": self.state,
            "values_used": self.values_used,
            "derivation_source": self.derivation_source,
            "position_xy": list(self.position_xy) if self.position_xy else None,
            "flags": self.flags,
        }

    def human_line(self) -> str:
        """Backward-compatible free-text line for the learning-loop logs."""
        pos = ("" if self.position_xy is None
               else f" @ ({self.position_xy[0]:.4g}, {self.position_xy[1]:.4g})")
        extra = ""
        if self.state == STATE_BUILT_DERIVED and self.derivation_source:
            extra = f" [derived: {self.derivation_source}]"
        elif self.state == STATE_EXCLUDED:
            why = next((f.get("human_note") for f in self.flags if f.get("human_note")), "")
            extra = f" — EXCLUDED: {why}" if why else " — EXCLUDED (incomplete)"
        return (f"[stage {self.stage} {self.stage_name}] {self.feature_id} "
                f"({self.feature_type}) {self.state}{pos}{extra}")


@dataclass
class SequenceResult:
    build_order: list[str]
    dispositions: list[Disposition]
    hard_failures: list[str] = field(default_factory=list)

    @property
    def disposition_table(self) -> list[dict[str, Any]]:
        return [d.as_dict() for d in self.dispositions]

    @property
    def human_lines(self) -> list[str]:
        return [d.human_line() for d in self.dispositions]


# ── Geometry helpers (self-contained; no macro_generator import) ─────────────
def _feature_dim_values(model: DrawingData, feature: Feature) -> dict[str, float]:
    """Canonical applies_to -> value for the dimensions this feature consumes."""
    out: dict[str, float] = {}
    ids = list(feature.related_dimensions or [])
    if feature.depth_dimension_id:
        ids.append(feature.depth_dimension_id)
    for did in ids:
        d = model.dimension_by_id(did)
        if d is None:
            continue
        key = d.canonical_applies_to or (d.applies_to or "").strip().lower()
        if key and (key not in out or d.value > out[key]):
            out[key] = float(d.value)
    return out


def _base_area(model: DrawingData, feature: Feature) -> float:
    """A size proxy (drawing-unit area) for choosing the largest base / ordering.

    Rectangular envelope uses length*width; a round profile uses the hole/OD
    diameter. Returns 0.0 when nothing sizes it (still deterministic — id breaks
    the tie)."""
    dims = _feature_dim_values(model, feature)
    length = dims.get("length") or dims.get("width")
    width = dims.get("width") or dims.get("length")
    if length and width:
        return float(length) * float(width)
    dia = dims.get("diameter") or dims.get("hole_diameter") or dims.get("outer_diameter")
    if dia:
        return math.pi * (float(dia) / 2.0) ** 2
    return 0.0


def _cut_volume(model: DrawingData, feature: Feature) -> float:
    """Size proxy for ordering profile subtractions largest-first."""
    dims = _feature_dim_values(model, feature)
    area = _base_area(model, feature)
    depth = dims.get("depth") or dims.get("height") or dims.get("thickness") or 1.0
    return area * float(depth)


def _feature_xy(model: DrawingData, feature: Feature) -> tuple[float, float]:
    """(x, y) drawing-frame position for deterministic ordering.

    Holes read from the linked callout (edge-referenced first instance); every
    other feature uses its own offsets. Rounded to tame float noise so the sort
    order is stable across runs."""
    h = model.hole_callout_for_feature(feature.id)
    if h is not None:
        if h.instance_positions:
            x, y = h.instance_positions[0][0], h.instance_positions[0][1]
        else:
            x, y = h.x_position, h.y_position
    else:
        x, y = feature.offset_x, feature.offset_y
    return (round(float(x), 6), round(float(y), 6))


def _hole_subtype_rank(model: DrawingData, feature: Feature) -> int:
    """Within Stage 4: plain THRU (0) -> counterbore/countersink (1) -> tapped (2)."""
    h = model.hole_callout_for_feature(feature.id)
    if h is None:
        # A THREAD feature with no callout is cosmetic — but it is routed to
        # Stage 7 before this is reached; treat a bare thread type as tapped.
        return 2 if feature.type == FeatureType.THREAD else 0
    if h.thread_spec or feature.type == FeatureType.THREAD:
        return 2
    if h.cbore_diameter > 0 or h.csink_diameter > 0:
        return 1
    return 0


# ── Stage classification ─────────────────────────────────────────────────────
def classify_stage(model: DrawingData, feature: Feature) -> int:
    """Map a feature to its canonical stage by geometric ROLE (never by a
    confidence/omission policy). Base vs. additive is finalized in
    :func:`sequence_build_order` (the single largest base is Stage 1)."""
    t = feature.type
    if t in _BASE_TYPES:
        return STAGE_BASE  # provisional; largest wins, rest -> additive
    if t == FeatureType.EXTRUDE_CUT:
        return STAGE_PROFILE_CUT
    if t == FeatureType.HOLE:
        return STAGE_HOLE
    if t == FeatureType.THREAD:
        # A tapped hole (has a drillable callout) is Stage 4; a bare cosmetic
        # thread is non-geometric metadata (Stage 7).
        return STAGE_HOLE if model.hole_callout_for_feature(feature.id) is not None \
            else STAGE_NONGEOMETRIC
    if t in _PATTERN_TYPES:
        return STAGE_PATTERN
    if t == FeatureType.CHAMFER:
        return STAGE_EDGE
    if t == FeatureType.FILLET:
        return STAGE_EDGE
    if t == FeatureType.SHELL:
        # Prohibited for automation but still a real subtractive operation —
        # keep it in the build order (macro_generator emits its MANUAL step) at
        # the topology-changing stage rather than dropping it.
        return STAGE_PROFILE_CUT
    # Unreachable for the closed FeatureType enum; kept so an unknown role is
    # surfaced (a per-feature extraction bug) rather than silently ordered.
    return STAGE_PROFILE_CUT


def _sort_key(model: DrawingData, feature: Feature, stage: int) -> tuple:
    """Deterministic within-stage ordering key. Always ends with the feature id
    so the total order is unique (byte-identical build_order across runs)."""
    fid = feature.id
    if stage in (STAGE_BASE, STAGE_ADDITIVE):
        return (-_base_area(model, feature), fid)
    if stage == STAGE_PROFILE_CUT:
        x, y = _feature_xy(model, feature)
        return (-_cut_volume(model, feature), x, y, fid)
    if stage == STAGE_HOLE:
        x, y = _feature_xy(model, feature)
        return (_hole_subtype_rank(model, feature), x, y, fid)
    if stage == STAGE_PATTERN:
        x, y = _feature_xy(model, feature)
        return (x, y, fid)
    if stage == STAGE_EDGE:
        # Chamfers before fillets (a chamfer must precede a fillet on a shared
        # edge for predictable results).
        edge_rank = 0 if feature.type == FeatureType.CHAMFER else 1
        return (edge_rank, fid)
    return (fid,)


# ── Disposition state ────────────────────────────────────────────────────────
def _derivation_source(feature: Feature, resolution) -> str:
    """Non-empty derivation basis if any driving value of this feature was
    inferred rather than read; '' when everything was read directly."""
    if resolution is None:
        return ""
    ids = list(feature.related_dimensions or [])
    if feature.depth_dimension_id:
        ids.append(feature.depth_dimension_id)
    for did in ids:
        dr = resolution.dim_resolutions.get(did) if resolution.dim_resolutions else None
        if dr is None:
            continue
        basis = (dr.assumption_basis or "").strip().lower()
        if dr.assumption_made and basis and basis not in _EXPLICIT_BASES:
            return basis
    fr = resolution.feature_resolutions.get(feature.id) if resolution.feature_resolutions else None
    if fr is not None and not fr.position_resolved:
        pa = (fr.position_assumption or "").strip().lower()
        if pa and pa not in _READ_POSITIONS:
            return f"position:{pa}"
    return ""


def _feature_flags(feature_id: str, resolution) -> list[dict[str, Any]]:
    if resolution is None or not resolution.flags:
        return []
    return [f for f in resolution.flags
            if f.get("feature_id") == feature_id or f.get("dimension_id") == feature_id]


def disposition_state(feature: Feature, resolution, in_build_order: bool) -> str:
    """Three-state classification for one feature."""
    if not in_build_order:
        return STATE_EXCLUDED
    return STATE_BUILT_DERIVED if _derivation_source(feature, resolution) else STATE_BUILT


# ── Main entry point ─────────────────────────────────────────────────────────
def sequence_build_order(model: DrawingData, resolution=None) -> SequenceResult:
    """Produce the deterministic staged ``build_order`` and the disposition table.

    ``model.build_order`` on input is the resolver's gate-filtered set (excluded
    features have already been dropped). This function re-orders the survivors
    into the canonical seven-stage sequence and records EVERY feature — built or
    excluded — in the disposition table. It does not mutate the model.
    """
    buildable = list(model.build_order or [])
    buildable_set = set(buildable)

    # Finalize base vs. additive: among base-type features that survive the gate,
    # the single largest-area one is the Stage-1 base; the rest become Stage-2
    # additive bosses. (The extractor is asked for base-first, but we do not
    # trust order — we pick by size, deterministically.)
    base_candidates = [
        model.feature_by_id(fid) for fid in buildable
        if (f := model.feature_by_id(fid)) is not None and f.type in _BASE_TYPES
    ]
    base_candidates = [f for f in base_candidates if f is not None]
    chosen_base_id: Optional[str] = None
    if base_candidates:
        chosen_base_id = min(
            base_candidates, key=lambda f: (-_base_area(model, f), f.id)
        ).id

    hard_failures: list[str] = []
    if not base_candidates:
        # The one legitimate hard failure (no closed outer profile to build from).
        # Surfaced for the caller; we do not raise so the rest of the pipeline
        # (reports, macros, learning loop) still runs.
        hard_failures.append(
            "No base solid: no extrude_boss/revolve survived the completeness "
            "gate, so there is no closed outer profile to build from."
        )

    def _stage_of(feat: Feature) -> int:
        st = classify_stage(model, feat)
        if st == STAGE_BASE and feat.id != chosen_base_id:
            return STAGE_ADDITIVE
        return st

    # Order the survivors by (stage, within-stage key).
    ordered: list[tuple[int, tuple, str]] = []
    for fid in buildable:
        feat = model.feature_by_id(fid)
        if feat is None:
            continue
        stage = _stage_of(feat)
        ordered.append((stage, _sort_key(model, feat, stage), fid))
    ordered.sort(key=lambda t: (t[0], t[1]))
    build_order = [fid for _, _, fid in ordered]

    # Build the disposition table over ALL features (built + excluded), ordered
    # deterministically by (stage, state-rank, key) so the table itself is stable.
    _STATE_RANK = {STATE_BUILT: 0, STATE_BUILT_DERIVED: 1, STATE_EXCLUDED: 2}
    dispositions: list[Disposition] = []
    for feat in model.features:
        in_order = feat.id in buildable_set
        stage = _stage_of(feat)
        key = _sort_key(model, feat, stage)
        state = disposition_state(feat, resolution, in_order)
        deriv = _derivation_source(feat, resolution) if in_order else ""
        dispositions.append(Disposition(
            feature_id=feat.id,
            feature_type=feat.type.value,
            stage=stage,
            stage_name=STAGE_NAMES[stage],
            state=state,
            sort_key=key,
            values_used=_feature_dim_values(model, feat),
            derivation_source=deriv,
            position_xy=_feature_xy(model, feat),
            flags=_feature_flags(feat.id, resolution),
        ))
    dispositions.sort(key=lambda d: (d.stage, _STATE_RANK.get(d.state, 9), d.sort_key))

    log.info(
        "build sequencer: %d built across stages %s; %d excluded; base=%s",
        len(build_order),
        sorted({d.stage for d in dispositions if d.state != STATE_EXCLUDED}),
        sum(1 for d in dispositions if d.state == STATE_EXCLUDED),
        chosen_base_id,
    )
    return SequenceResult(build_order=build_order, dispositions=dispositions,
                          hard_failures=hard_failures)
