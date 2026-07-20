"""MTI_Codex: OpenAI provider adapter (pipeline/ai_provider.py).

The adapter's job is to make ``AI_PROVIDER=openai`` invisible to every call
site (extractor.py, overview_analysis.py, must_meet.py, overview_check.py):
they only ever see Anthropic-shaped requests/responses. These tests exercise
the translation logic directly (no network) plus the provider-selection and
pricing plumbing. No API key or network access required.
"""
import json
from types import SimpleNamespace

import pytest

from pipeline.ai_provider import (
    _image_url_from_block,
    _system_text,
    _tool_call_message,
    _translate_messages,
    _translate_response,
    _translate_tool_choice,
    _translate_tools,
    default_model,
    get_provider,
    is_nonretryable_status,
    is_transient_error,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
def test_default_provider_is_anthropic(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    assert get_provider() == "anthropic"
    assert default_model() == "claude-sonnet-5"


def test_openai_provider_selected_by_env(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    assert get_provider() == "openai"
    assert default_model() == "gpt-5.6"


def test_provider_env_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "OpenAI")
    assert get_provider() == "openai"


def test_unrecognized_provider_falls_back_to_anthropic(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "some-typo")
    assert default_model() == "claude-sonnet-5"


# --------------------------------------------------------------------------- #
# Transient-error classification (used by both SDKs' exception naming)
# --------------------------------------------------------------------------- #
class _FakeConnectionError(Exception):
    pass


class _FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__("status")
        self.status_code = status_code


_FakeConnectionError.__name__ = "APIConnectionError"
_FakeStatusError.__name__ = "APIStatusError"


def test_connection_error_is_transient():
    assert is_transient_error(_FakeConnectionError())
    assert not is_nonretryable_status(_FakeConnectionError())


@pytest.mark.parametrize("code", [429, 500, 502, 503, 529])
def test_transient_status_codes(code):
    e = _FakeStatusError(code)
    assert is_transient_error(e)
    assert not is_nonretryable_status(e)


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_nonretryable_status_codes(code):
    e = _FakeStatusError(code)
    assert not is_transient_error(e)
    assert is_nonretryable_status(e)


def test_unrelated_exception_is_neither():
    e = ValueError("not an API error")
    assert not is_transient_error(e)
    assert not is_nonretryable_status(e)


# --------------------------------------------------------------------------- #
# Message translation: Anthropic content blocks -> OpenAI Chat Completions
# --------------------------------------------------------------------------- #
def test_system_text_handles_string_and_block_list():
    assert _system_text("plain string") == "plain string"
    assert _system_text([{"type": "text", "text": "a"},
                         {"type": "text", "text": "b"}]) == "a\n\nb"
    assert _system_text(None) == ""


def test_image_block_becomes_data_url():
    block = {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": "AAAA"}}
    assert _image_url_from_block(block) == "data:image/png;base64,AAAA"


def test_user_text_and_image_translate_to_content_parts():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "hello"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": "ZZZZ"}},
    ]}]
    out = _translate_messages(messages)
    assert out == [{"role": "user", "content": [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,ZZZZ"}},
    ]}]


def test_plain_string_content_passes_through():
    messages = [{"role": "user", "content": "just text"}]
    assert _translate_messages(messages) == [{"role": "user", "content": "just text"}]


def test_assistant_tool_use_becomes_tool_calls():
    block = SimpleNamespace(type="tool_use", id="call_1", name="report_data",
                            input={"a": 1})
    messages = [{"role": "assistant", "content": [block]}]
    out = _translate_messages(messages)
    assert out[0]["role"] == "assistant"
    assert out[0]["tool_calls"][0]["id"] == "call_1"
    assert out[0]["tool_calls"][0]["function"]["name"] == "report_data"
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


def test_tool_result_becomes_tool_role_message_before_new_content():
    """A low-confidence re-query bundles a tool_result with fresh images/text
    in ONE Anthropic user turn — the adapter must split it: the tool message
    first, then a new user message, matching OpenAI's ordering requirement."""
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "ack", "is_error": False},
        {"type": "text", "text": "look again"},
    ]}]
    out = _translate_messages(messages)
    assert out[0] == {"role": "tool", "tool_call_id": "call_1", "content": "ack"}
    assert out[1]["role"] == "user"
    assert out[1]["content"] == [{"type": "text", "text": "look again"}]


def test_tool_result_error_is_prefixed():
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "bad schema",
         "is_error": True},
    ]}]
    out = _translate_messages(messages)
    assert out[0]["content"] == "ERROR: bad schema"


def test_multiple_tool_results_each_become_their_own_message():
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "a", "is_error": False},
        {"type": "tool_result", "tool_use_id": "call_2", "content": "dup ignored",
         "is_error": True},
    ]}]
    out = _translate_messages(messages)
    assert len(out) == 2
    assert {m["tool_call_id"] for m in out} == {"call_1", "call_2"}


# --------------------------------------------------------------------------- #
# Tool / tool_choice translation
# --------------------------------------------------------------------------- #
def test_tool_schema_translation():
    tools = [{"name": "report", "description": "desc",
             "input_schema": {"type": "object", "properties": {}}}]
    out = _translate_tools(tools)
    assert out == [{"type": "function", "function": {
        "name": "report", "description": "desc",
        "parameters": {"type": "object", "properties": {}},
    }}]


def test_no_tools_translates_to_none():
    assert _translate_tools(None) is None
    assert _translate_tools([]) is None


def test_tool_choice_translation():
    assert _translate_tool_choice({"type": "tool", "name": "report"}) == {
        "type": "function", "function": {"name": "report"}}
    assert _translate_tool_choice(None) is None


# --------------------------------------------------------------------------- #
# Response translation: OpenAI ChatCompletion -> Anthropic-shaped response
# --------------------------------------------------------------------------- #
def _fake_openai_response(*, tool_calls=None, content=None, refusal=None,
                          finish_reason="tool_calls", prompt_tokens=100,
                          completion_tokens=20, cached_tokens=0):
    message = SimpleNamespace(tool_calls=tool_calls, content=content, refusal=refusal)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens))
    return SimpleNamespace(choices=[choice], usage=usage)


def _fake_tool_call(call_id, name, args_dict):
    return SimpleNamespace(id=call_id,
                           function=SimpleNamespace(name=name, arguments=json.dumps(args_dict)))


def test_tool_call_translates_to_tool_use_block():
    resp = _fake_openai_response(tool_calls=[_fake_tool_call("call_1", "report", {"x": 1})])
    out = _translate_response(resp)
    assert len(out.content) == 1
    assert out.content[0].type == "tool_use"
    assert out.content[0].id == "call_1"
    assert out.content[0].name == "report"
    assert out.content[0].input == {"x": 1}
    assert out.stop_reason == "tool_use"


def test_usage_excludes_cached_from_input_tokens():
    """Mirrors Anthropic's convention: input_tokens is the UNCACHED portion,
    cache_read_input_tokens is the cached portion — together they sum to the
    model's total prompt tokens, so estimate_cost() prices each correctly."""
    resp = _fake_openai_response(tool_calls=[_fake_tool_call("c", "t", {})],
                                 prompt_tokens=1000, completion_tokens=50,
                                 cached_tokens=300)
    out = _translate_response(resp)
    assert out.usage.input_tokens == 700
    assert out.usage.cache_read_input_tokens == 300
    assert out.usage.cache_creation_input_tokens == 0
    assert out.usage.output_tokens == 50


def test_malformed_tool_arguments_become_empty_dict_not_a_crash():
    bad_call = SimpleNamespace(id="c1", function=SimpleNamespace(name="report",
                                                                 arguments="{not json"))
    resp = _fake_openai_response(tool_calls=[bad_call])
    out = _translate_response(resp)
    assert out.content[0].input == {}  # fails the caller's Pydantic validation instead


def test_refusal_maps_to_refusal_stop_reason():
    resp = _fake_openai_response(tool_calls=None, refusal="I can't help with that",
                                 finish_reason="stop")
    out = _translate_response(resp)
    assert out.stop_reason == "refusal"


def test_plain_text_response_with_no_tool_calls():
    resp = _fake_openai_response(tool_calls=None, content="just some text",
                                 finish_reason="stop")
    out = _translate_response(resp)
    assert out.content[0].type == "text"
    assert out.content[0].text == "just some text"
    assert out.stop_reason == "end_turn"


def test_length_finish_reason_maps_to_max_tokens():
    resp = _fake_openai_response(tool_calls=None, content="truncated", finish_reason="length")
    out = _translate_response(resp)
    assert out.stop_reason == "max_tokens"


# --------------------------------------------------------------------------- #
# Client construction (no network — just key-presence/error-path checks)
# --------------------------------------------------------------------------- #
def test_build_client_openai_requires_key(monkeypatch):
    from pipeline.ai_provider import build_client

    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EnvironmentError, match="OPENAI_API_KEY"):
        build_client(3)


def test_build_client_openai_returns_adapter_with_key(monkeypatch):
    from pipeline.ai_provider import build_client

    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
    client = build_client(3)
    assert hasattr(client, "messages")
    assert hasattr(client.messages, "create")


def test_extractor_default_model_follows_provider(monkeypatch):
    """extractor.DEFAULT_MODEL is a module-level constant computed ONCE at
    import time (matching the original ``DEFAULT_MODEL = "claude-sonnet-5"``
    plain-constant design) via ``_default_model()``. Test that underlying
    function directly under both env states rather than reloading the module:
    ``importlib.reload`` would replace class identities module-wide (e.g.
    ExtractionError becomes a NEW class object), silently breaking
    ``pytest.raises(ExtractionError)`` in every other test that imported the
    old class reference before the reload — a real test-pollution hazard,
    not a hypothetical one."""
    import pipeline.extractor as extractor_module

    monkeypatch.setenv("AI_PROVIDER", "openai")
    assert extractor_module._default_model() == "gpt-5.6"
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    assert extractor_module._default_model() == "claude-sonnet-5"


def test_usage_log_pricing_includes_gpt_5_6_tiers():
    from pipeline.usage_log import PRICING, estimate_cost

    assert PRICING["gpt-5.6"]["input"] == 5.00
    assert PRICING["gpt-5.6"]["output"] == 30.00
    usage = {"input_tokens": 1000, "output_tokens": 1000, "cache_read_input_tokens": 0}
    cost = estimate_cost(usage, "gpt-5.6")
    assert cost == pytest.approx(0.005 + 0.030)
