"""Tests for pipeline.validator build-readiness checks."""
import copy

import pytest

from pipeline.schema import DrawingData
from pipeline.validator import DrawingValidationError, validate_drawing_data


def valid_drawing() -> dict:
    """A minimal but fully build-ready drawing: extruded block with a hole."""
    return {
        "part_name": "test_block",
        "units": "mm",
        "confidence": 0.95,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 100.0, "unit": "mm", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 50.0, "unit": "mm", "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 20.0, "unit": "mm", "applies_to": "height"},
            {"id": "D004", "type": "diameter", "value": 10.0, "unit": "mm", "applies_to": "hole_diameter"},
        ],
        "features": [
            {
                "id": "F001",
                "type": "extrude_boss",
                "description": "Base block",
                "related_dimensions": ["D001", "D002"],
                "depth_dimension_id": "D003",
                "sketch_plane": "Top",
            },
            {
                "id": "F002",
                "type": "hole",
                "description": "Through hole",
                "related_dimensions": ["D004"],
            },
        ],
        "build_order": ["F001", "F002"],
        "warnings": [],
    }


class TestValidData:
    def test_valid_passes(self):
        result = validate_drawing_data(valid_drawing())
        assert isinstance(result, DrawingData)
        assert result.part_name == "test_block"

    def test_valid_no_raise_mode(self):
        result = validate_drawing_data(valid_drawing(), raise_on_error=False)
        assert isinstance(result, DrawingData)


class TestBaseFeature:
    def test_missing_base_feature_fails(self):
        data = valid_drawing()
        # Make the first built feature a cut (not a solid base).
        data["features"][0]["type"] = "extrude_cut"
        with pytest.raises(DrawingValidationError) as exc:
            validate_drawing_data(data)
        assert "solid base" in str(exc.value).lower()

    def test_no_features_fails(self):
        data = valid_drawing()
        data["features"] = []
        data["build_order"] = []
        with pytest.raises(DrawingValidationError):
            validate_drawing_data(data)


class TestGeometrySanity:
    def test_zero_value_dimension_fails(self):
        data = valid_drawing()
        data["dimensions"][0]["value"] = 0.0
        # Caught at the schema layer (positive constraint) and surfaced as a
        # build-readiness failure.
        with pytest.raises(DrawingValidationError):
            validate_drawing_data(data)

    def test_negative_value_dimension_fails(self):
        data = valid_drawing()
        data["dimensions"][0]["value"] = -5.0
        with pytest.raises(DrawingValidationError):
            validate_drawing_data(data)


class TestBuildOrder:
    def test_mismatched_build_order_id_fails(self):
        data = valid_drawing()
        data["build_order"] = ["F001", "F999"]  # F999 doesn't exist
        with pytest.raises(DrawingValidationError) as exc:
            validate_drawing_data(data)
        assert "F999" in str(exc.value)

    def test_empty_build_order_fails(self):
        data = valid_drawing()
        data["build_order"] = []
        with pytest.raises(DrawingValidationError):
            validate_drawing_data(data)


class TestUnitConsistency:
    def test_mixed_units_fails(self):
        data = valid_drawing()
        data["dimensions"][0]["unit"] = "inch"  # drawing is mm
        with pytest.raises(DrawingValidationError) as exc:
            validate_drawing_data(data)
        assert "unit" in str(exc.value).lower()


class TestFeatureDependency:
    def test_feature_references_unknown_dimension_fails(self):
        data = valid_drawing()
        data["features"][1]["related_dimensions"] = ["D404"]  # nonexistent
        with pytest.raises(DrawingValidationError) as exc:
            validate_drawing_data(data)
        assert "D404" in str(exc.value)


class TestFeatureAliasNormalization:
    def test_extrude_alias_maps_to_boss(self):
        data = valid_drawing()
        data["features"][0]["type"] = "extrude"  # alias for extrude_boss
        result = validate_drawing_data(data)
        assert result.features[0].type.value == "extrude_boss"
