"""Pydantic v2 models for extracted drawing data.

This module is the **single source of truth** for the data shape. The same
models are used two ways:

1. As the ``input_schema`` of the forced tool call Claude must make (see
   pipeline/extractor.py — strict structured outputs choke on this schema's
   nested object arrays, so non-strict forced tool use is used instead).
2. As the validation layer for any data the pipeline ingests (including the
   ``--debug`` JSON files and test fixtures).

CONVENTION: no Optional/None fields — every field has a non-null default
("", 0.0, empty list). Tool-use extraction emits these far more reliably than
nullable fields.

Field-level guarantees (positivity, enums, ranges) live here. Cross-field
*build-readiness* rules (base feature exists, build_order dependencies satisfied,
dimensional closure, pattern envelopes) live in :mod:`pipeline.validator`, which
produces a rich human-readable report instead of a raw ValidationError.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Units(str, Enum):
    MM = "mm"
    CM = "cm"
    INCH = "inch"


class DimensionType(str, Enum):
    LINEAR = "linear"
    RADIAL = "radial"
    DIAMETER = "diameter"
    ANGULAR = "angular"
    DEPTH = "depth"
    THREAD = "thread"


class ToleranceType(str, Enum):
    BILATERAL = "bilateral"
    UNILATERAL = "unilateral"
    LIMIT = "limit"
    REFERENCE = "reference"


class FeatureType(str, Enum):
    """Canonical SolidWorks feature operations.

    Note these are the *canonical* names. The first feature in a build must be
    an ``extrude_boss`` (a solid base body) before any cut/hole can run.
    """

    EXTRUDE_BOSS = "extrude_boss"
    EXTRUDE_CUT = "extrude_cut"
    REVOLVE = "revolve"
    HOLE = "hole"
    FILLET = "fillet"
    CHAMFER = "chamfer"
    THREAD = "thread"
    PATTERN = "pattern"
    SHELL = "shell"


# Aliases Claude (or a hand-written fixture) might emit, mapped to canonical names.
_FEATURE_ALIASES = {
    "extrude": "extrude_boss",
    "boss": "extrude_boss",
    "extrude_base": "extrude_boss",
    "base_extrude": "extrude_boss",
    "cut": "extrude_cut",
    "cut_extrude": "extrude_cut",
    "extruded_cut": "extrude_cut",
    "hole_wizard": "hole",
    "round": "fillet",
    "bevel": "chamfer",
}


# --------------------------------------------------------------------------- #
# applies_to canonicalization (fixes failure class E010)
# --------------------------------------------------------------------------- #
# In production, Claude emits verbose, view-qualified applies_to labels such as
# "width (top view, overall horizontal)" or "thru hole diameter (4 places)".
# The generator/validator key off canonical tokens (length/width/height/...), so
# an exact-string match silently misses these and the build fails with
# "profile needs a diameter or length+width". canonicalize_applies_to() maps the
# free text to a canonical token. Rules are ordered MOST-SPECIFIC FIRST: compound
# hole-feature labels (counterbore/countersink/drill) must win before the plain
# "depth"/"diameter" rules, because e.g. "counterbore depth" contains "depth".
CANONICAL_APPLIES_TO = (
    "length", "width", "height", "thickness",
    "hole_diameter", "diameter", "radius", "fillet_radius",
    "chamfer", "depth", "spacing", "angle",
    "cbore_diameter", "cbore_depth", "csink_diameter", "csink_angle",
)

_APPLIES_TO_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Compound hole features FIRST (these substrings also contain depth/diameter).
    ("cbore_diameter", ("counterbore diameter", "counterbore dia", "cbore diameter", "cbore dia", "c'bore dia")),
    ("cbore_depth", ("counterbore depth", "cbore depth", "c'bore depth")),
    ("csink_diameter", ("countersink diameter", "countersink dia", "csink dia")),
    ("csink_angle", ("countersink angle", "csink angle")),
    ("hole_diameter", ("hole diameter", "thru hole dia", "through hole dia", "drill diameter",
                       "drill dia", "bore diameter", "hole dia")),
    ("fillet_radius", ("fillet radius", "fillet", "corner radius")),
    ("chamfer", ("chamfer",)),
    ("spacing", ("spacing", "pitch", "center-to-center", "center to center")),
    ("thickness", ("thickness", "material thick", "plate thick")),
    ("depth", ("drill depth", "hole depth", "blind depth", "depth")),
    ("diameter", ("diameter", "dia")),
    ("radius", ("radius",)),
    ("angle", ("angle",)),
    ("length", ("length",)),
    ("width", ("width",)),
    ("height", ("height",)),
)


def canonicalize_applies_to(label: str) -> str:
    """Map a free-text applies_to label to a canonical token (or "" if none).

    Exact canonical tokens pass through unchanged. Otherwise the first matching
    (most-specific-first) substring rule wins. Returns "" when nothing matches,
    so callers can distinguish "unknown" from a real token.
    """
    s = (label or "").lower().strip()
    if not s:
        return ""
    if s in CANONICAL_APPLIES_TO:
        return s
    for token, needles in _APPLIES_TO_RULES:
        if any(n in s for n in needles):
            return token
    return ""


def is_envelope_label(label: str) -> bool:
    """True for a label that denotes a part OVERALL envelope dimension.

    Accepts the clean canonical tokens (length/width/height) and verbose labels
    that say "overall" (e.g. "width (top view, overall horizontal)"), but NOT
    feature-local sizes like "width (front view, small feature)" — those would
    corrupt the envelope used for hole-centering and feasibility checks.
    """
    s = (label or "").lower().strip()
    token = canonicalize_applies_to(s)
    if token not in ("length", "width", "height"):
        return False
    return s == token or "overall" in s


class HoleType(str, Enum):
    THRU = "thru"
    BLIND = "blind"
    COUNTERBORE = "counterbore"
    COUNTERSINK = "countersink"
    SPOTFACE = "spotface"
    TAPPED = "tapped"


class PatternKind(str, Enum):
    NONE = "none"
    LINEAR = "linear"
    CIRCULAR = "circular"


class View(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_type: str = Field(description="Front/Top/Right/Isometric/Section/Detail/Auxiliary")
    description: str = Field(description="What this view shows")
    dimensions_shown: list[str] = Field(
        default_factory=list, description="IDs of dimensions readable in this view"
    )
    visible_features: list[str] = Field(
        default_factory=list, description="Feature IDs visible (solid lines) in this view"
    )
    hidden_features: list[str] = Field(
        default_factory=list, description="Feature IDs shown hidden (dashed lines) in this view"
    )
    centerline_notes: str = Field(
        default="",
        description="Center lines present and what they imply (holes, symmetry, revolved geometry); empty if none",
    )


class HoleCallout(BaseModel):
    """One hole callout (possibly covering multiple instances via qty/pattern)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable identifier, e.g. H001")
    type: HoleType
    diameter: float = Field(description="Nominal hole diameter in drawing units")
    thru: bool = Field(default=True, description="True for THRU holes; False for blind")
    depth: float = Field(default=0.0, description="Blind depth in drawing units; 0.0 if THRU")
    thread_spec: str = Field(default="", description='Thread callout, e.g. "1/4-20 UNC"; empty if not tapped')
    cbore_diameter: float = Field(default=0.0, description="Counterbore diameter; 0.0 if none")
    cbore_depth: float = Field(default=0.0, description="Counterbore depth; 0.0 if none")
    csink_diameter: float = Field(default=0.0, description="Countersink diameter; 0.0 if none")
    csink_angle: float = Field(default=0.0, description="Countersink included angle in degrees; 0.0 if none")
    qty: int = Field(default=1, description="Number of instances this callout covers")
    x_position: float = Field(
        default=0.0,
        description="X of the first instance from the part center/origin, drawing units; 0.0 if centered/unknown",
    )
    y_position: float = Field(
        default=0.0,
        description="Y of the first instance from the part center/origin, drawing units; 0.0 if centered/unknown",
    )
    position_known: bool = Field(
        default=False, description="True only if x/y positions were read from the drawing"
    )
    pattern: PatternKind = Field(default=PatternKind.NONE)
    pattern_spacing: float = Field(default=0.0, description="Pattern spacing in drawing units; 0.0 if no pattern")
    feature_ref: str = Field(default="", description="Feature ID (F###) this callout corresponds to; empty if none")
    view: str = Field(default="", description="View the callout appears in")

    @field_validator("diameter")
    @classmethod
    def diameter_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"hole diameter must be positive, got {v}")
        return v

    @field_validator("qty")
    @classmethod
    def qty_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"qty must be >= 1, got {v}")
        return v


class DimensionChain(BaseModel):
    """A closed dimensional loop: total = sum of components (closure-checkable)."""

    model_config = ConfigDict(extra="forbid")

    total_dimension_id: str = Field(description="ID of the overall/envelope dimension")
    component_dimension_ids: list[str] = Field(
        description="IDs of the dimensions that should sum to the total"
    )
    description: str = Field(default="", description="What this chain represents")


class SymmetryNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plane: str = Field(description="Plane of symmetry: Front/Top/Right or described")
    feature_ids: list[str] = Field(default_factory=list, description="Features mirrored about this plane")
    description: str = Field(default="")


class ConcentricGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_ids: list[str] = Field(description="Features that are coaxial/concentric")
    description: str = Field(default="")


class SpacingNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_ref: str = Field(description="Feature or hole-callout ID with equal spacing")
    qty: int = Field(description="Number of equally spaced instances")
    spacing_value: float = Field(description="Computed spacing in drawing units")
    computed_from: str = Field(
        default="", description="How spacing was derived, e.g. 'overall 80mm / 4 gaps'"
    )


class RelationshipMap(BaseModel):
    """Explicit geometric relationships mapped before any build."""

    model_config = ConfigDict(extra="forbid")

    symmetry: list[SymmetryNote] = Field(default_factory=list)
    concentric_groups: list[ConcentricGroup] = Field(default_factory=list)
    equal_spacing: list[SpacingNote] = Field(default_factory=list)
    dimension_chains: list[DimensionChain] = Field(
        default_factory=list,
        description="Closed dimension loops where components should sum to a total",
    )
    derived_dimension_ids: list[str] = Field(
        default_factory=list,
        description="IDs of dimensions that were computed (implied by symmetry/spacing), not read directly",
    )
    reference_dimension_ids: list[str] = Field(
        default_factory=list,
        description="IDs of REF / parenthesized dimensions — non-controlling",
    )


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable identifier, e.g. D001")
    type: DimensionType
    value: float = Field(description="Numeric magnitude only — never a string with units")
    unit: Units
    tolerance_plus: float = Field(default=0.0, description="Upper tolerance, e.g. 0.1 (0 if none)")
    tolerance_minus: float = Field(default=0.0, description="Lower tolerance, e.g. 0.1 (0 if none)")
    tolerance_type: ToleranceType = Field(
        default=ToleranceType.REFERENCE, description="Use 'reference' if no tolerance is given"
    )
    applies_to: str = Field(
        default="", description="length/width/height/hole_diameter/fillet_radius/etc, or empty if unknown"
    )
    feature_ref: str = Field(
        default="", description="Feature ID (F###) this dimension controls, or empty if unknown"
    )
    view: str = Field(default="", description="Which view this dimension is read from, or empty if unclear")
    datum_ref: str = Field(default="", description="GD&T datum reference, e.g. A; empty if none")
    gdt_symbol: str = Field(
        default="", description="GD&T symbol (flatness/perpendicularity/etc); empty if none"
    )
    is_reference: bool = Field(
        default=False, description="True for REF / parenthesized (non-controlling) dimensions"
    )
    value_unclear: bool = Field(
        default=False,
        description="True if the printed value is illegible/ambiguous — `value` is then a best guess",
    )
    ambiguity_reason: str = Field(
        default="", description="Why the value is unclear (overlapping lines, smudge, ...); empty if clear"
    )
    possible_values: list[float] = Field(
        default_factory=list,
        description="Candidate readings when value_unclear (best guess first); empty if clear",
    )
    resolution_required: bool = Field(
        default=False,
        description="True if a human must resolve this dimension before building",
    )
    notes: str = Field(default="", description="Special callouts or notes, or empty if none")

    @field_validator("value")
    @classmethod
    def value_must_be_positive(cls, v: float) -> float:
        # A zero or negative magnitude can never define real geometry.
        if v <= 0:
            raise ValueError(f"dimension value must be positive, got {v}")
        return v

    @property
    def canonical_applies_to(self) -> str:
        """The applies_to label mapped to a canonical token ("" if unknown)."""
        return canonicalize_applies_to(self.applies_to)

    @property
    def is_envelope(self) -> bool:
        """True if this dimension denotes a part OVERALL envelope dimension."""
        return is_envelope_label(self.applies_to) and not self.is_reference


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable identifier, e.g. F001")
    type: FeatureType
    description: str
    related_dimensions: list[str] = Field(
        default_factory=list, description="IDs of dimensions this feature consumes"
    )
    sketch_plane: str = Field(default="", description="Front/Top/Right/custom, or empty for default")
    depth_dimension_id: str = Field(
        default="", description="ID of the dimension giving this feature's depth/length, or empty if none"
    )
    parent_feature: str = Field(
        default="",
        description="Feature ID this one depends on (e.g. pattern seed, fillet's host); empty for base",
    )
    offset_x: float = Field(
        default=0.0,
        description="Sketch-center X offset from the part origin in drawing units; 0.0 if centered/unknown",
    )
    offset_y: float = Field(
        default=0.0,
        description="Sketch-center Y offset from the part origin in drawing units; 0.0 if centered/unknown",
    )
    position_known: bool = Field(
        default=False, description="True only if the offsets were read from the drawing"
    )
    quantity: int = Field(default=1, description="Instance count (for patterns)")

    @field_validator("type", mode="before")
    @classmethod
    def normalize_feature_type(cls, v):
        # Map common synonyms to canonical names before enum validation.
        if isinstance(v, str):
            return _FEATURE_ALIASES.get(v.strip().lower(), v.strip().lower())
        return v

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"quantity must be >= 1, got {v}")
        return v


class GeometricTolerance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="flatness/perpendicularity/cylindricity/etc")
    value: float
    datum: str = Field(default="", description="Datum reference, e.g. A, or empty if none")


class DrawingData(BaseModel):
    """Top-level structured representation of one engineering drawing."""

    model_config = ConfigDict(extra="forbid")

    part_name: str = Field(default="", description="Part name from the title block, or empty if not given")
    part_number: str = Field(default="", description="Part number from the title block, or empty if not given")
    revision: str = Field(default="", description="Drawing revision, e.g. A; empty if not given")
    drawing_standard: str = Field(default="", description="ASME/ISO/DIN, or empty if not given")
    units: Units = Field(description="Drawing's declared unit system")
    scale: str = Field(default="", description='e.g. "1:1" or "as shown", or empty if not given')
    material: str = Field(default="", description="Material, or empty if not given")
    finish: str = Field(default="", description="Surface finish, or empty if not given")
    general_tolerance: str = Field(
        default="",
        description="General tolerance block text (e.g. '.XX ±0.01, .XXX ±0.005'); empty if not given",
    )
    views: list[View] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    hole_callouts: list[HoleCallout] = Field(default_factory=list)
    features: list[Feature] = Field(default_factory=list)
    geometric_tolerances: list[GeometricTolerance] = Field(default_factory=list)
    relationships: RelationshipMap = Field(default_factory=RelationshipMap)
    build_order: list[str] = Field(
        default_factory=list,
        description="Feature IDs in logical SolidWorks build order; first must be a base solid",
    )
    warnings: list[str] = Field(default_factory=list)
    confidence: float = Field(description="0.0 (unclear) .. 1.0 (all dimensions clear)")

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0.0, 1.0], got {v}")
        return v

    # --- convenience lookups used by the validator and builder ---

    def feature_by_id(self, feature_id: str) -> Optional[Feature]:
        return next((f for f in self.features if f.id == feature_id), None)

    def dimension_by_id(self, dim_id: str) -> Optional[Dimension]:
        return next((d for d in self.dimensions if d.id == dim_id), None)

    def hole_callout_by_id(self, hole_id: str) -> Optional[HoleCallout]:
        return next((h for h in self.hole_callouts if h.id == hole_id), None)

    def hole_callout_for_feature(self, feature_id: str) -> Optional[HoleCallout]:
        return next((h for h in self.hole_callouts if h.feature_ref == feature_id), None)

    @property
    def display_name(self) -> str:
        """Best available name for files/folders: part_number > part_name > 'part'."""
        base = self.part_number or self.part_name or "part"
        if self.revision:
            base = f"{base}-Rev{self.revision}"
        return base
