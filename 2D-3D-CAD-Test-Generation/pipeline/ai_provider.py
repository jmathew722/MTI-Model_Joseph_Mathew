"""AI provider abstraction — MTI_Codex branch (2026-07-20).

Every call site in this pipeline (``extractor.py``, ``overview_analysis.py``,
``must_meet.py``, ``overview_check.py``) talks to its model client through ONE
uniform contract, unchanged since it was written for Anthropic:

    client.messages.create(model=, max_tokens=, system=, messages=, tools=,
                           tool_choice=) -> response
    response.content            # list of blocks; tool_use blocks have
                                 # .type=="tool_use", .id, .name, .input (dict)
    response.usage.<field>      # input_tokens / output_tokens /
                                 # cache_creation_input_tokens / cache_read_input_tokens
    response.stop_reason        # "refusal" is the only branched-on value

This module lets ``AI_PROVIDER=openai`` swap the underlying model WITHOUT
touching any of those call sites: :func:`build_client` returns either the real
``anthropic.Anthropic`` client (unchanged, default) or an
:class:`_OpenAIAdapterClient` that exposes the identical ``.messages.create``
surface and internally translates Anthropic-shaped requests/responses to and
from the OpenAI Chat Completions API. The "wire format" used throughout the
pipeline's Python stays Anthropic's shape either way — only this module knows
about OpenAI's actual message/tool/response format.

Model choice (GPT-5.6, current as of 2026-07; see
``docs/research/OPENAI_PROVIDER_NOTES.md`` for the lineup check): ``gpt-5.6``
(alias for ``gpt-5.6-sol``, the frontier tier) is vision-capable AND the
strongest available reasoning model, so it is used for every stage — vision
extraction, Stage 1.5 overview analysis, must-meet spec parsing, and the final
overview cross-check all reason over either images or structured JSON with the
same model.

Never imports the ``openai`` package unless ``AI_PROVIDER=openai`` — mirrors
the existing "imported here so the module imports without the package present"
convention for ``anthropic``.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
def get_provider() -> str:
    return (os.getenv("AI_PROVIDER") or "anthropic").strip().lower()


def default_model() -> str:
    """The provider's default model, used when ``EXTRACTION_MODEL`` is unset.

    GPT-5.6 is used for every stage on the OpenAI path (vision extraction,
    Stage 1.5 overview, must-meet parsing, overview cross-check) — it is both
    the strongest current vision model and the strongest reasoning model, so
    one model covers every call site without a second env var to configure.
    """
    return "gpt-5.6" if get_provider() == "openai" else "claude-sonnet-5"


def is_transient_error(e: Exception) -> bool:
    """True for a transient (retry-worthy) API error, for EITHER SDK.

    Both the ``anthropic`` and ``openai`` Python SDKs name their exceptions
    ``APIConnectionError``/``APIStatusError`` and expose ``.status_code`` on
    the latter, so this works without importing either SDK directly.
    """
    cls_name = type(e).__name__
    if cls_name == "APIConnectionError":
        return True
    if cls_name == "APIStatusError":
        return getattr(e, "status_code", None) in (429, 500, 502, 503, 529)
    return False


def is_nonretryable_status(e: Exception) -> bool:
    """True when a bare ``APIStatusError`` should be re-raised immediately
    (auth/bad-request class errors) rather than retried."""
    return type(e).__name__ == "APIStatusError" and not is_transient_error(e)


# --------------------------------------------------------------------------- #
# Client construction
# --------------------------------------------------------------------------- #
def build_client(max_retries: int):
    """The provider-appropriate client, exposing ``.messages.create(...)``."""
    if get_provider() == "openai":
        return _build_openai_client(max_retries)
    import anthropic  # imported here so this module loads without the package

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not found. Copy .env.template to .env and set your key."
        )
    return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)


def _build_openai_client(max_retries: int) -> "_OpenAIAdapterClient":
    import openai  # imported here so this module loads without the package

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "OPENAI_API_KEY not found. Set AI_PROVIDER=openai and OPENAI_API_KEY "
            "in .env (see .env.example)."
        )
    raw = openai.OpenAI(api_key=api_key, max_retries=max_retries)
    return _OpenAIAdapterClient(raw)


# --------------------------------------------------------------------------- #
# Anthropic <-> OpenAI translation helpers
# --------------------------------------------------------------------------- #
def _get(block: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-dict access — Anthropic content blocks are sometimes plain
    dicts (as built by the call sites) and sometimes SDK/adapter objects (when
    an earlier response's ``.content`` is fed back in during a repair retry)."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(_get(b, "text", "") for b in system)


def _image_url_from_block(block: Any) -> str:
    source = _get(block, "source") or {}
    media_type = _get(source, "media_type", "image/png")
    data = _get(source, "data", "")
    return f"data:{media_type};base64,{data}"


def _tool_call_message(block: Any) -> dict[str, Any]:
    """One assistant tool_use block -> the OpenAI tool_calls entry."""
    return {
        "id": _get(block, "id", ""),
        "type": "function",
        "function": {
            "name": _get(block, "name", ""),
            "arguments": json.dumps(_get(block, "input", {}) or {}),
        },
    }


def _translate_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped messages -> OpenAI Chat Completions messages.

    Tool results become their own ``role: "tool"`` messages (OpenAI's
    contract), emitted before any remaining new content in the same turn, so
    a low-confidence re-query (tool_result + fresh images + text, all one
    Anthropic "user" turn) still orders correctly for OpenAI.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            tool_blocks = [b for b in content if _get(b, "type") == "tool_use"]
            text_blocks = [b for b in content if _get(b, "type") == "text"]
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(_get(b, "text", "") for b in text_blocks) or None,
            }
            if tool_blocks:
                entry["tool_calls"] = [_tool_call_message(b) for b in tool_blocks]
            out.append(entry)
            continue

        # role == "user": split tool_result blocks (-> tool messages) from
        # everything else (-> one new user message with translated parts).
        tool_results = [b for b in content if _get(b, "type") == "tool_result"]
        other = [b for b in content if _get(b, "type") != "tool_result"]
        for tr in tool_results:
            text = _get(tr, "content", "")
            if _get(tr, "is_error", False):
                text = f"ERROR: {text}"
            out.append({"role": "tool", "tool_call_id": _get(tr, "tool_use_id", ""),
                       "content": text})
        if other:
            parts: list[dict[str, Any]] = []
            for b in other:
                btype = _get(b, "type")
                if btype == "text":
                    parts.append({"type": "text", "text": _get(b, "text", "")})
                elif btype == "image":
                    parts.append({"type": "image_url",
                                  "image_url": {"url": _image_url_from_block(b)}})
            out.append({"role": "user", "content": parts})
    return out


def _translate_tools(tools: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
    if not tools:
        return None
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        },
    } for t in tools]


def _translate_tool_choice(tool_choice: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not tool_choice:
        return None
    if tool_choice.get("type") == "tool":
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return tool_choice


_FINISH_REASON_MAP = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _translate_response(resp: Any) -> SimpleNamespace:
    """OpenAI ChatCompletion -> the Anthropic-shaped response object every
    call site already knows how to read."""
    choice = resp.choices[0]
    message = choice.message
    content: list[SimpleNamespace] = []
    for tc in (getattr(message, "tool_calls", None) or []):
        try:
            parsed = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            # Malformed JSON from the model: an empty dict fails the caller's
            # Pydantic validation (extra="forbid"/required fields), which
            # routes into the SAME repair-retry path a schema mismatch does —
            # no separate error handling needed here.
            parsed = {}
        content.append(SimpleNamespace(type="tool_use", id=tc.id,
                                       name=tc.function.name, input=parsed))
    if getattr(message, "content", None):
        content.append(SimpleNamespace(type="text", text=message.content))

    refused = bool(getattr(message, "refusal", None))
    stop_reason = "refusal" if refused else _FINISH_REASON_MAP.get(
        choice.finish_reason, choice.finish_reason)

    usage = getattr(resp, "usage", None)
    if usage is not None:
        cached = getattr(getattr(usage, "prompt_tokens_details", None),
                         "cached_tokens", 0) or 0
        usage_ns = SimpleNamespace(
            input_tokens=max(usage.prompt_tokens - cached, 0),
            output_tokens=usage.completion_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cached,
        )
    else:
        usage_ns = None

    return SimpleNamespace(content=content, usage=usage_ns, stop_reason=stop_reason,
                           stop_details=getattr(message, "refusal", None))


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
class _OpenAIAdapterClient:
    """Exposes ``.messages.create(...)`` with Anthropic's exact call/return
    contract, backed by the OpenAI Chat Completions API."""

    def __init__(self, raw_client):
        self.messages = _OpenAIMessagesAdapter(raw_client)


class _OpenAIMessagesAdapter:
    def __init__(self, raw_client):
        self._client = raw_client

    def create(self, *, model: str, max_tokens: int, system: Any = None,
              messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
              tool_choice: Optional[dict[str, Any]] = None) -> SimpleNamespace:
        openai_messages: list[dict[str, Any]] = []
        sys_text = _system_text(system)
        if sys_text:
            openai_messages.append({"role": "system", "content": sys_text})
        openai_messages.extend(_translate_messages(messages))

        kwargs: dict[str, Any] = dict(
            model=model,
            max_completion_tokens=max_tokens,
            messages=openai_messages,
        )
        openai_tools = _translate_tools(tools)
        if openai_tools:
            kwargs["tools"] = openai_tools
            # GPT-5.6 (and the wider reasoning-model family) rejects function
            # tools on /v1/chat/completions together with any non-"none"
            # reasoning_effort ("Function tools with reasoning_effort are not
            # supported ... use /v1/responses or set reasoning_effort to
            # 'none'" — live-verified 2026-07-20). This pipeline's forced
            # tool-calling design has nothing that consumes a reasoning trace,
            # so disabling it is the correct choice, not a workaround.
            if model.startswith("gpt-5") or model.startswith("o"):
                kwargs["reasoning_effort"] = "none"
        openai_tool_choice = _translate_tool_choice(tool_choice)
        if openai_tool_choice:
            kwargs["tool_choice"] = openai_tool_choice

        resp = self._client.chat.completions.create(**kwargs)
        return _translate_response(resp)
