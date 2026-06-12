"""Dimension extraction via the Claude Vision API.

Uses ``claude-opus-4-8`` with a **forced tool call** — Claude must respond by
calling the ``report_drawing_data`` tool, whose ``input_schema`` is generated
from :class:`pipeline.schema.DrawingData`. The tool's JSON input is then
validated against the same Pydantic model.

Note: this intentionally avoids Anthropic's *strict* structured outputs
(``output_format=``), whose server-side grammar compiler cannot handle
``DrawingData``'s nested arrays of objects ("Schema is too complex" /
"Grammar compilation timed out"). Non-strict tool use has no such compiler
step, at the cost of needing a Pydantic-validation repair retry instead of a
server-side guarantee.

Public entry point: :func:`extract_drawing_data`.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import ValidationError

from pipeline.schema import DrawingData
from utils.logger import get_logger

log = get_logger()
load_dotenv()

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
CONFIDENCE_THRESHOLD = 0.7  # below this, re-query once for a closer look
SDK_MAX_RETRIES = 3  # SDK-level retries for transient API errors

TOOL_NAME = "report_drawing_data"

SYSTEM_PROMPT = """\
You are a precision engineering drawing interpreter. Extract ALL information \
from this 2D engineering drawing with exact accuracy, then report it by calling \
the required tool.

CRITICAL RULES:
- Extract EVERY visible dimension — miss nothing. Each gets a stable id (D001, D002, ...).
- If a dimension is unclear, still include it and add a note to `warnings`; do not skip it.
- All numeric values must be numbers, never strings (e.g. 25.4, not "25.4mm"). Put the
  unit in the `unit` field.
- `features` describe how to build the part. Each gets a stable id (F001, F002, ...).
  Use ONLY these feature `type` values: extrude_boss, extrude_cut, revolve, hole,
  fillet, chamfer, thread, pattern, shell.
- `build_order` lists feature ids in logical SolidWorks build order. The FIRST feature
  MUST be a solid base body — an `extrude_boss` (or `revolve`) — before any cut/hole.
- `related_dimensions` / `depth_dimension_id` must reference ids that exist in `dimensions`.
- `confidence`: 1.0 = every dimension is clear and unambiguous; 0.0 = the drawing is
  very unclear. Be honest — a low score triggers a closer second look.
- If you genuinely cannot determine the units, use millimeters and add a warning.
- Use empty string "" (not null) for any text field you cannot determine.
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


def _image_block(image_b64: str, media_type: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
    }


def _drawing_data_tool() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Report the fully extracted structured data for this engineering drawing.",
        "input_schema": DrawingData.model_json_schema(),
    }


def _call(client, model: str, messages: list[dict[str, Any]]):
    return client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
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


def _parse(client, model: str, messages: list[dict[str, Any]]) -> tuple[DrawingData, Any, Any]:
    """Call the model and return (validated DrawingData, response, tool_use block).

    Raises ExtractionError if the model refused, didn't call the tool, or the
    tool input fails Pydantic validation even after one repair retry.
    """
    response = _call(client, model, messages)
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
) -> dict[str, Any]:
    """Extract structured drawing data from a base64 image.

    Args:
        image_b64: Base64-encoded image (from :func:`utils.image_prep.prepare_image`).
        media_type: MIME type of the image (default ``image/png``).
        model: Model override; defaults to ``$EXTRACTION_MODEL`` or ``claude-opus-4-8``.
        prep_warnings: Warnings from image preparation, merged into the result so the
            operator sees them alongside the model's own warnings.

    Returns:
        A JSON-serializable dict conforming to :class:`DrawingData`.

    Raises:
        EnvironmentError: if the API key is missing.
        ExtractionError: if the model refuses or returns unvalidatable output.
    """
    model = model or os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL
    client = _build_client(SDK_MAX_RETRIES)

    log.info("Extracting drawing data with model %s", model)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                _image_block(image_b64, media_type),
                {"type": "text", "text": INITIAL_USER_TEXT},
            ],
        }
    ]

    data, response, tool_use = _parse(client, model, messages)
    log.info(
        "Initial extraction: %d dimensions, %d features, confidence=%.2f",
        len(data.dimensions),
        len(data.features),
        data.confidence,
    )

    # --- Domain-specific re-query on low confidence (once) ---
    if data.confidence < CONFIDENCE_THRESHOLD:
        log.warning(
            "Confidence %.2f < %.2f — re-querying for a closer look.",
            data.confidence,
            CONFIDENCE_THRESHOLD,
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
                    _image_block(image_b64, media_type),
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
            requeried, _, _ = _parse(client, model, messages)
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

    return data.model_dump(mode="json")
