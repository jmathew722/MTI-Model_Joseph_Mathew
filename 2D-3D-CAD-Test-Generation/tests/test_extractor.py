"""Tests for pipeline.extractor with the Claude API mocked.

Extraction is done via a forced tool call (not strict structured outputs — see
extractor.py docstring), so the failure modes we guard against are: refusal,
the model not calling the tool, and tool input that fails Pydantic validation
(with a repair retry). We cover: a clean call, a low-confidence re-query,
a validation-failure repair, refusal handling, and the missing-API-key path.
"""
import pytest

from pipeline import extractor
from pipeline.extractor import TOOL_NAME, ExtractionError, extract_drawing_data
from pipeline.schema import DrawingData


def make_drawing_dict(confidence: float, n_dims: int = 2) -> dict:
    dims = [
        {"id": f"D00{i+1}", "type": "linear", "value": 10.0 + i, "unit": "mm",
         "applies_to": "length" if i == 0 else "width"}
        for i in range(n_dims)
    ]
    return {
        "part_name": "mock",
        "units": "mm",
        "confidence": confidence,
        "dimensions": dims,
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "base",
             "related_dimensions": [d["id"] for d in dims]}
        ],
        "build_order": ["F001"],
        "warnings": ["D002 was faint"] if confidence < 0.7 else [],
    }


class FakeToolUseBlock:
    def __init__(self, input_dict, tool_id="tool_1", name=TOOL_NAME):
        self.type = "tool_use"
        self.id = tool_id
        self.name = name
        self.input = input_dict


class FakeResponse:
    def __init__(self, content, stop_reason=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = None


def tool_response(input_dict, **kw):
    return FakeResponse([FakeToolUseBlock(input_dict, **kw)])


def refusal_response():
    return FakeResponse([], stop_reason="refusal")


def no_tool_response():
    return FakeResponse([], stop_reason="end_turn")


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def create(self, **kwargs):
        self.call_count += 1
        if self._responses:
            return self._responses.pop(0)
        return tool_response(make_drawing_dict(0.95))


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


@pytest.fixture
def patch_client(monkeypatch):
    """Patch _build_client to return a FakeClient with scripted responses."""
    def _install(responses):
        client = FakeClient(responses)
        monkeypatch.setattr(extractor, "_build_client", lambda *a, **k: client)
        return client

    return _install


class TestHappyPath:
    def test_valid_tool_call_returns_dict(self, patch_client):
        client = patch_client([tool_response(make_drawing_dict(0.95))])
        result = extract_drawing_data("ZmFrZQ==")  # base64 of "fake"
        assert isinstance(result, dict)
        assert result["confidence"] == 0.95
        assert len(result["dimensions"]) == 2
        assert client.messages.call_count == 1

    def test_prep_warnings_merged(self, patch_client):
        patch_client([tool_response(make_drawing_dict(0.95))])
        result = extract_drawing_data("ZmFrZQ==", prep_warnings=["low contrast"])
        assert "low contrast" in result["warnings"]


class TestLowConfidenceRequery:
    def test_low_confidence_triggers_second_call(self, patch_client):
        client = patch_client(
            [tool_response(make_drawing_dict(0.5)), tool_response(make_drawing_dict(0.92))]
        )
        result = extract_drawing_data("ZmFrZQ==")
        # The re-query should have run and adopted the higher-confidence result.
        assert client.messages.call_count == 2
        assert result["confidence"] == 0.92

    def test_high_confidence_does_not_requery(self, patch_client):
        client = patch_client([tool_response(make_drawing_dict(0.95))])
        extract_drawing_data("ZmFrZQ==")
        assert client.messages.call_count == 1

    def test_requery_failure_keeps_initial(self, patch_client):
        # First call low confidence; second call refuses -> keep the first result.
        client = patch_client(
            [tool_response(make_drawing_dict(0.5)), refusal_response()]
        )
        result = extract_drawing_data("ZmFrZQ==")
        assert client.messages.call_count == 2
        assert result["confidence"] == 0.5
        assert any("re-query failed" in w.lower() for w in result["warnings"])


class TestValidationRepair:
    def test_invalid_input_triggers_repair_retry(self, patch_client):
        bad = make_drawing_dict(0.95)
        bad["dimensions"][0]["value"] = -5.0  # fails value_must_be_positive
        good = make_drawing_dict(0.95)
        client = patch_client([tool_response(bad), tool_response(good)])
        result = extract_drawing_data("ZmFrZQ==")
        assert client.messages.call_count == 2
        assert result["dimensions"][0]["value"] > 0

    def test_repair_failure_raises(self, patch_client):
        bad = make_drawing_dict(0.95)
        bad["dimensions"][0]["value"] = -5.0
        client = patch_client([tool_response(bad), tool_response(bad)])
        with pytest.raises(ExtractionError):
            extract_drawing_data("ZmFrZQ==")
        assert client.messages.call_count == 2


class TestRefusalAndErrors:
    def test_refusal_raises(self, patch_client):
        patch_client([refusal_response()])
        with pytest.raises(ExtractionError):
            extract_drawing_data("ZmFrZQ==")

    def test_no_tool_call_raises(self, patch_client):
        patch_client([no_tool_response()])
        with pytest.raises(ExtractionError):
            extract_drawing_data("ZmFrZQ==")


class TestMissingApiKey:
    def test_missing_key_raises_environment_error(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # _build_client checks the env var before importing anthropic, so this
        # works even if the anthropic package is not installed.
        with pytest.raises(EnvironmentError):
            extractor._build_client(3)
