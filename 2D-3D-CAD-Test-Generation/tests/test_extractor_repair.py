"""Regression: the extraction repair retry must answer EVERY tool_use block.

The Anthropic API rejects a conversation where an assistant turn contains
tool_use blocks that the next user turn does not answer with tool_result
blocks ("tool_use ids were found without tool_result blocks immediately
after"). The model may emit more than one tool_use in a single response, so
the repair message must carry one tool_result per id — this test locks that
behavior after the real 400 seen on 2026-07-04.
"""
from types import SimpleNamespace

import pytest

from pipeline import extractor as ex


def _tool_use(block_id: str, data: dict):
    return SimpleNamespace(type="tool_use", id=block_id, name=ex.TOOL_NAME, input=data)


def _response(*blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=None)


GOOD = {
    "units": "inch", "confidence": 0.9,
    "dimensions": [{"id": "D001", "type": "linear", "value": 4.0, "unit": "inch",
                    "applies_to": "length"}],
    "features": [{"id": "F001", "type": "extrude_boss", "description": "base"}],
    "build_order": ["F001"],
}
BAD = {"units": "inch"}  # missing required confidence -> ValidationError


class TestRepairAnswersEveryToolUse:
    def test_two_tool_uses_get_two_tool_results(self, monkeypatch):
        calls = []

        def fake_call(client, model, messages):
            calls.append(messages)
            if len(calls) == 1:
                # First response: TWO tool_use blocks, first one invalid.
                return _response(_tool_use("tu_1", BAD), _tool_use("tu_2", BAD))
            return _response(_tool_use("tu_3", GOOD))

        monkeypatch.setattr(ex, "_call", fake_call)
        model, resp, tu = ex._parse(client=None, model="m", messages=[
            {"role": "user", "content": "extract"}])
        assert model.confidence == 0.9

        repair_msgs = calls[1]
        assistant, user = repair_msgs[-2], repair_msgs[-1]
        assert assistant["role"] == "assistant"
        assert user["role"] == "user"
        answered = {b["tool_use_id"] for b in user["content"]
                    if b.get("type") == "tool_result"}
        emitted = {b.id for b in assistant["content"] if b.type == "tool_use"}
        assert answered == emitted == {"tu_1", "tu_2"}
        assert all(b["is_error"] for b in user["content"])

    def test_single_tool_use_repair_still_works(self, monkeypatch):
        calls = []

        def fake_call(client, model, messages):
            calls.append(messages)
            return _response(_tool_use("tu_1", BAD)) if len(calls) == 1 \
                else _response(_tool_use("tu_2", GOOD))

        monkeypatch.setattr(ex, "_call", fake_call)
        model, _, _ = ex._parse(client=None, model="m",
                                messages=[{"role": "user", "content": "extract"}])
        assert model.confidence == 0.9
        answered = [b["tool_use_id"] for b in calls[1][-1]["content"]]
        assert answered == ["tu_1"]

    def test_second_failure_raises_extraction_error(self, monkeypatch):
        monkeypatch.setattr(ex, "_call",
                            lambda c, m, msgs: _response(_tool_use("tu", BAD)))
        with pytest.raises(ex.ExtractionError, match="after repair retry"):
            ex._parse(client=None, model="m",
                      messages=[{"role": "user", "content": "extract"}])


class TestMultiviewRequeryToolResults:
    """The low-confidence multiview re-query must also answer the tool_use —
    this exact omission produced a real 400 on 2026-07-04."""

    def test_requery_turn_leads_with_tool_result(self, monkeypatch):
        low_conf = dict(GOOD)
        low_conf["confidence"] = 0.3
        low_conf["warnings"] = ["hole diameter unclear"]
        calls = []

        def fake_call(client, model, messages):
            calls.append([dict(m) for m in messages])
            return _response(_tool_use(f"tu_{len(calls)}",
                                       GOOD if len(calls) > 1 else low_conf))

        monkeypatch.setattr(ex, "_call", fake_call)
        monkeypatch.setattr(ex, "_build_client", lambda retries: None)
        monkeypatch.setattr(ex, "_confidence_threshold", lambda: 0.85)
        out = ex.extract_drawing_data_multiview(
            [("front", "aGk=", "image/png")], cache_dir=None)
        assert out["confidence"] == 0.9  # re-query result won

        assert len(calls) == 2, "low confidence must trigger exactly one re-query"
        requery_msgs = calls[1]
        assistant, user = requery_msgs[-2], requery_msgs[-1]
        assert assistant["role"] == "assistant" and user["role"] == "user"
        first = user["content"][0]
        assert first["type"] == "tool_result" and first["tool_use_id"] == "tu_1"

    def test_cache_control_blocks_capped_at_api_limit(self, monkeypatch):
        """Max 4 cache_control blocks per request (1 is the system prompt), so
        at most 3 view images may carry one — a 5-view part must not 400 — and
        the re-query must re-send views with NO cache_control at all."""
        low_conf = dict(GOOD)
        low_conf["confidence"] = 0.3
        low_conf["warnings"] = ["unclear"]
        calls = []

        def fake_call(client, model, messages):
            calls.append([dict(m) for m in messages])
            return _response(_tool_use(f"tu_{len(calls)}",
                                       GOOD if len(calls) > 1 else low_conf))

        monkeypatch.setattr(ex, "_call", fake_call)
        monkeypatch.setattr(ex, "_build_client", lambda retries: None)
        monkeypatch.setattr(ex, "_confidence_threshold", lambda: 0.85)
        views = [(v, "aGk=", "image/png")
                 for v in ("front", "top", "side", "second_side", "full")]
        ex.extract_drawing_data_multiview(views, cache_dir=None)

        first_content = calls[0][0]["content"]
        cached = [b for b in first_content
                  if isinstance(b, dict) and "cache_control" in b]
        assert len(cached) <= 3, f"{len(cached)} cached blocks + system > API limit of 4"
        requery_user = calls[1][-1]["content"]
        assert not any(isinstance(b, dict) and "cache_control" in b
                       for b in requery_user), "re-query must not add cache blocks"
