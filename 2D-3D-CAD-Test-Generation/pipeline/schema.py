"""Pydantic v2 models for extracted drawing data.

This module is the **single source of truth** for the data shape. The same
models are used two ways:

1. As the JSON schema Claude must conform to (passed to ``messages.parse`` via
   ``output_config.format``) — structured outputs guarantee a schema-valid
   response, so no regex/JSON-repair fallback is needed.
2. As the validation layer for any data the pipeline ingests (including the
   ``--debug`` JSON files and test fixtures).

Field-level guarantees (positivity, enums, ranges) live here. Cross-field
*build-readiness* rules (base feature exists, build_order dependencies satisfied,
sketch definability) live in :mod:`pipeline.validator`, which produces a rich
human-readable report instead of a raw ValidationError.
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


class View(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_type: str = Field(description="Front/Top/Right/Isometric/Section/Detail")
    description: str = Field(description="What this view shows")


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
    view: str = Field(default="", description="Which view this dimension is read from, or empty if unclear")
    notes: str = Field(default="", description="Special callouts or notes, or empty if none")

    @field_validator("value")
    @classmethod
    def value_must_be_positive(cls, v: float) -> float:
        # A zero or negative magnitude can never define real geometry.
        if v <= 0:
            raise ValueError(f"dimension value must be positive, got {v}")
        return v


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

    part_name: str = Field(default="", description="Part name/number, or empty if not given")
    drawing_standard: str = Field(default="", description="ASME/ISO/DIN, or empty if not given")
    units: Units = Field(description="Drawing's declared unit system")
    scale: str = Field(default="", description='e.g. "1:1" or "as shown", or empty if not given')
    material: str = Field(default="", description="Material, or empty if not given")
    finish: str = Field(default="", description="Surface finish, or empty if not given")
    views: list[View] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    features: list[Feature] = Field(default_factory=list)
    geometric_tolerances: list[GeometricTolerance] = Field(default_factory=list)
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
