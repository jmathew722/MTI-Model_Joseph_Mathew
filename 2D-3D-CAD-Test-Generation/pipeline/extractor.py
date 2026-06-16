"""Dimension extraction via the Claude Vision API.

Uses ``claude-sonnet-4-6`` (override with ``EXTRACTION_MODEL``) with a **forced
tool call** — Claude must respond by calling the ``report_drawing_data`` tool,
whose ``input_schema`` is generated from :class:`pipeline.schema.DrawingData`.
The tool's JSON input is then validated against the same Pydantic model.

Note: this intentionally avoids Anthropic's *strict* structured outputs
(``output_format=``), whose server-side grammar compiler cannot handle
``DrawingData``'s nested arrays of objects ("Schema is too complex" /
"Grammar compilation timed out"). Non-strict tool use has no such compiler
step, at the cost of needing a Pydantic-validation repair retry instead of a
server-side guarantee.

Public entry point: :func:`extract_drawing_data`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional, Union

from dotenv import load_dotenv
from pydantic import ValidationError

from pipeline.schema import DrawingData
from utils.logger import get_logger

log = get_logger()
load_dotenv()

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 16000
CONFIDENCE_THRESHOLD = 0.7  # below this, re-query once for a closer look
SDK_MAX_RETRIES = 3  # SDK-level retries for transient API errors

TOOL_NAME = "report_drawing_data"

# Token usage fields summed across the (possibly several) calls of one extraction.
_USAGE_FIELDS = (
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
)


def _confidence_threshold() -> float:
    raw = os.getenv("EXTRACTION_CONFIDENCE_THRESHOLD")
    if raw:
        try:
            return float(raw)
        except ValueError:
            log.warning("Ignoring non-numeric EXTRACTION_CONFIDENCE_THRESHOLD=%r", raw)
    return CONFIDENCE_THRESHOLD


def _accumulate_usage(acc: dict[str, int], response: Any) -> None:
    """Add one response's token usage into ``acc`` (no-op if absent, e.g. mocks)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    for field in _USAGE_FIELDS:
        acc[field] = acc.get(field, 0) + (getattr(usage, field, 0) or 0)
    acc["calls"] = acc.get("calls", 0) + 1


# --- Extraction cache: skip the API entirely for an already-seen image ------- #
def _cache_key(image_b64: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\0")
    h.update(image_b64.encode("utf-8"))
    return h.hexdigest()


def _cache_lookup(cache_dir: Optional[Union[str, Path]], key: str) -> Optional[dict]:
    if not cache_dir:
        return None
    path = Path(cache_dir) / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cache_store(cache_dir: Optional[Union[str, Path]], key: str, data: dict) -> None:
    if not cache_dir:
        return
    try:
        d = Path(cache_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write extraction cache: %s", e)

SYSTEM_PROMPT = """\
You are a senior design engineer interpreting a 2D engineering drawing with exact \
accuracy. Fully extract, interpret, and cross-check the drawing, then report it by \
calling the required tool. A bad extraction becomes a wrong 3D model — be thorough \
and honest about uncertainty.

TITLE BLOCK & STANDARD:
- Read the title block: part_name, part_number, revision, scale, material, finish,
  and the general tolerance block text (general_tolerance).
- Identify drawing_standard (ASME/ISO/DIN) and units (inch vs mm; fractional inches
  are still "inch").

VIEWS:
- Identify and classify EVERY view (Front/Top/Right/Section/Detail/Auxiliary/...).
- For each view list which dimension ids are readable in it (dimensions_shown),
  which features are visible vs hidden (dashed), and what the center lines imply
  (holes, symmetry, revolved geometry) in centerline_notes.

DIMENSIONS:
- Extract EVERY dimension — miss nothing. Stable ids D001, D002, ...
- All numeric values are numbers, never strings (25.4, not "25.4mm"); unit goes in `unit`.
- Record tolerances (explicit, or note when only the general block applies),
  GD&T symbols and datum references where present.
- Mark REF / parenthesized dimensions with is_reference=true — they are non-controlling.
- If a value is illegible or ambiguous: set value to your best guess, value_unclear=true,
  fill ambiguity_reason and possible_values (best guess first), and set
  resolution_required=true if a human MUST resolve it before building. NEVER silently skip.
- Set feature_ref to the feature (F###) each dimension controls when determinable.

HOLE CALLOUTS:
- Extract every hole callout into hole_callouts (H001, ...): type
  (thru/blind/counterbore/countersink/spotface/tapped), diameter, depth or thru,
  thread_spec (e.g. "1/4-20 UNC"), cbore/csink data, qty, pattern and spacing.
- If the drawing dimensions the hole position from the part origin/center, set
  x_position/y_position and position_known=true. Otherwise leave position_known=false.

FEATURES & BUILD ORDER (F001, F002, ...):
- Use ONLY these feature `type` values: extrude_boss, extrude_cut, revolve, hole,
  fillet, chamfer, thread, pattern, shell.
- build_order: base solid first (largest extrude_boss or revolve), then primary
  cuts/through holes, then counterbores/countersinks/taps, fillets and chamfers
  LAST, patterns after their seed. The FIRST feature MUST be extrude_boss or revolve.
- related_dimensions / depth_dimension_id must reference existing dimension ids.
- Set parent_feature for dependent features (pattern seed, fillet's host feature).
- If a feature's sketch center is dimensioned from the part origin/center, set
  offset_x/offset_y and position_known=true.

RELATIONSHIPS (fill the relationships object — this enables arithmetic verification):
- symmetry: planes of symmetry and which features mirror about them.
- concentric_groups: coaxial holes/bosses.
- equal_spacing: equally spaced patterns with computed spacing (state the arithmetic
  in computed_from, e.g. "overall 80 / 4 gaps = 20").
- dimension_chains: EVERY closed dimension loop you can identify, e.g. overall
  length = left offset + feature + right offset. These are arithmetically checked.
- derived_dimension_ids: dimensions you computed (implied by symmetry/spacing) rather
  than read. Also add each derived dimension to `dimensions` with a note.
- reference_dimension_ids: ids of REF dimensions.

GENERAL:
- confidence: 1.0 = everything clear; below 0.7 triggers a closer second look. Be honest.
- If you genuinely cannot determine the units, use millimeters and add a warning.
- Use empty string "" (not null) for any text field you cannot determine; use 0.0 for
  unknown numeric fields that have a 0.0 default.
- Put any remaining doubts in `warnings`.
"""

INITIAL_USER_TEXT = (
    "Extract all dimensions, tolerances, views, geometric tolerances, and build "
    "features from this engineering drawing, then call the tool with the result."
)


class ExtractionError(Exception):
    """Raised when extraction cannot produce a valid DrawingData object."""


def _build_client(max_retries: int):
    """Construct the Anthropic client, failing clearly if the key is absent."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not found. Copy .env.template to .env and set your key."
        )
    import anthropic  # imported here so the module imports without the package present

    return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)


def _image_block(image_b64: str, media_type: str, cache: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
    }
    if cache:
        # Cache the image so the low-confidence re-query reads it from cache
        # (~90% cheaper) instead of re-paying full image input tokens.
        block["cache_control"] = {"type": "ephemeral"}
    return block


def _drawing_data_tool() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Report the fully extracted structured data for this engineering drawing.",
        "input_schema": DrawingData.model_json_schema(),
    }


def _call(client, model: str, messages: list[dict[str, Any]]):
    # cache_control on the system block caches the static prefix (tools + system
    # ~4.6k tokens), which is byte-identical across every call and every drawing
    # in a batch — so within the cache window nearly all calls read it at ~10%
    # cost. The image carries its own cache breakpoint (see _image_block).
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    return client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        tools=[_drawing_data_tool()],
        tool_choice={"type": "tool", "name": TOOL_NAME},
    )


def _find_tool_use(response) -> Any:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block
    raise ExtractionError(
        f"Model did not call {TOOL_NAME!r} (stop_reason={getattr(response, 'stop_reason', None)})."
    )


def _tool_result_message(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }
        ],
    }


def _parse(
    client, model: str, messages: list[dict[str, Any]], usage: Optional[dict[str, int]] = None
) -> tuple[DrawingData, Any, Any]:
    """Call the model and return (validated DrawingData, response, tool_use block).

    Raises ExtractionError if the model refused, didn't call the tool, or the
    tool input fails Pydantic validation even after one repair retry. Token usage
    from every underlying call is accumulated into ``usage`` when provided.
    """
    response = _call(client, model, messages)
    if usage is not None:
        _accumulate_usage(usage, response)
    if getattr(response, "stop_reason", None) == "refusal":
        raise ExtractionError(
            "Model refused the extraction request "
            f"(stop_details={getattr(response, 'stop_details', None)})."
        )
    tool_use = _find_tool_use(response)

    try:
        return DrawingData.model_validate(tool_use.input), response, tool_use
    except ValidationError as e:
        log.warning("Tool input failed validation; requesting a repair: %s", e)
        repair_messages = messages + [
            {"role": "assistant", "content": response.content},
            _tool_result_message(
                tool_use.id,
                f"Your input did not match the required schema:\n{e}\n"
                f"Call {TOOL_NAME} again with corrected input.",
                is_error=True,
            ),
        ]
        response2 = _call(client, model, repair_messages)
        if usage is not None:
            _accumulate_usage(usage, response2)
        if getattr(response2, "stop_reason", None) == "refusal":
            raise ExtractionError(
                "Model refused the repair request "
                f"(stop_details={getattr(response2, 'stop_details', None)})."
            )
        tool_use2 = _find_tool_use(response2)
        try:
            return DrawingData.model_validate(tool_use2.input), response2, tool_use2
        except ValidationError as e2:
            raise ExtractionError(f"Tool input failed validation after repair retry: {e2}") from e2


def extract_drawing_data(
    image_b64: str,
    media_type: str = "image/png",
    model: Optional[str] = None,
    prep_warnings: Optional[list[str]] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    usage_out: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    """Extract structured drawing data from a base64 image.

    Args:
        image_b64: Base64-encoded image (from :func:`utils.image_prep.prepare_image`).
        media_type: MIME type of the image (default ``image/png``).
        model: Model override; defaults to ``$EXTRACTION_MODEL`` or ``claude-sonnet-4-6``.
        prep_warnings: Warnings from image preparation, merged into the result so the
            operator sees them alongside the model's own warnings.
        cache_dir: If given, an on-disk extraction cache keyed by image+model hash;
            an exact re-run returns the cached result with NO API call (zero tokens).
        usage_out: If given, a dict updated with summed token usage for this call
            (``input_tokens``/``output_tokens``/cache fields/``calls``).

    Returns:
        A JSON-serializable dict conforming to :class:`DrawingData`.

    Raises:
        EnvironmentError: if the API key is missing.
        ExtractionError: if the model refuses or returns unvalidatable output.
    """
    model = model or os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL

    # --- Extraction cache: identical image+model returns the saved result free ---
    key = _cache_key(image_b64, model)
    cached = _cache_lookup(cache_dir, key)
    if cached is not None:
        log.info("Extraction cache HIT (%s…) — skipping API call.", key[:12])
        if usage_out is not None:
            usage_out.setdefault("cache_hits", 0)
            usage_out["cache_hits"] += 1
        return cached

    client = _build_client(SDK_MAX_RETRIES)
    usage: dict[str, int] = {}

    log.info("Extracting drawing data with model %s", model)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                _image_block(image_b64, media_type, cache=True),
                {"type": "text", "text": INITIAL_USER_TEXT},
            ],
        }
    ]

    data, response, tool_use = _parse(client, model, messages, usage)
    log.info(
        "Initial extraction: %d dimensions, %d features, confidence=%.2f",
        len(data.dimensions),
        len(data.features),
        data.confidence,
    )

    # --- Domain-specific re-query on low confidence (once) ---
    # Only re-query when the model flagged something specific to re-examine
    # (unclear/resolution-required dims or warnings); a blind re-query with
    # nothing to point at rarely helps and doubles the tokens.
    threshold = _confidence_threshold()
    has_focus = bool(data.warnings) or any(
        d.value_unclear or d.resolution_required for d in data.dimensions
    )
    if data.confidence < threshold and has_focus:
        log.warning(
            "Confidence %.2f < %.2f — re-querying for a closer look.",
            data.confidence,
            threshold,
        )
        unclear = "; ".join(data.warnings) or "any dimensions you were unsure about"
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": "Received. Take another look before finalizing.",
                    },
                    _image_block(image_b64, media_type, cache=True),
                    {
                        "type": "text",
                        "text": (
                            "Re-examine the drawing carefully, paying special attention "
                            f"to these unclear areas: {unclear}. Zoom mentally into the "
                            "dimension callouts and produce a corrected, complete "
                            "extraction by calling the tool again. Improve confidence "
                            "only if the drawing genuinely supports it."
                        ),
                    },
                ],
            }
        )
        try:
            requeried, _, _ = _parse(client, model, messages, usage)
            log.info(
                "Re-query result: %d dimensions, confidence %.2f -> %.2f",
                len(requeried.dimensions),
                data.confidence,
                requeried.confidence,
            )
            data = requeried
        except ExtractionError as e:
            # Keep the first (valid) result; just note the re-query failed.
            log.warning("Re-query failed (%s); keeping initial extraction.", e)
            data.warnings.append(f"Low-confidence re-query failed: {e}")

    # Merge image-prep warnings so nothing is lost downstream.
    if prep_warnings:
        data.warnings.extend(prep_warnings)

    if usage:
        log.info(
            "Token usage: %d calls, input=%d (cache read=%d, write=%d), output=%d",
            usage.get("calls", 0), usage.get("input_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0), usage.get("output_tokens", 0),
        )
    if usage_out is not None:
        for k, v in usage.items():
            usage_out[k] = usage_out.get(k, 0) + v

    result = data.model_dump(mode="json")
    _cache_store(cache_dir, key, result)
    return result
