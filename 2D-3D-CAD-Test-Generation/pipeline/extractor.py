"""Dimension extraction via the Claude Vision API.

Uses ``claude-opus-4-8`` with **structured outputs** — the response is forced to
conform to the :class:`pipeline.schema.DrawingData` schema, so we get a
schema-valid object back with no regex/JSON-repair fallback. The Anthropic SDK
handles API-level retry/backoff (429/5xx) automatically; on top of that we add a
domain-specific re-query when the model reports low confidence.

Public entry point: :func:`extract_drawing_data`.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv

from pipeline.schema import DrawingData
from utils.logger import get_logger

log = get_logger()
load_dotenv()

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
CONFIDENCE_THRESHOLD = 0.7  # below this, re-query once for a closer look
SDK_MAX_RETRIES = 3  # SDK-level retries for transient API errors

SYSTEM_PROMPT = """\
You are a precision engineering drawing interpreter. Extract ALL information \
from this 2D engineering drawing with exact accuracy into the required structured \
schema.

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
"""

INITIAL_USER_TEXT = (
    "Extract all dimensions, tolerances, views, geometric tolerances, and build "
    "features from this engineering drawing into the structured schema."
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


def _parse(client, model: str, messages: list[dict[str, Any]]) -> tuple[DrawingData, Any]:
    """Call messages.parse and return (validated DrawingData, raw response).

    Raises ExtractionError if the model refused or returned no parsed output.
    """
    response = client.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=messages,
        output_format=DrawingData,
    )
    if getattr(response, "stop_reason", None) == "refusal":
        raise ExtractionError(
            "Model refused the extraction request "
            f"(stop_details={getattr(response, 'stop_details', None)})."
        )
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise ExtractionError(
            "Structured output could not be parsed into DrawingData "
            f"(stop_reason={getattr(response, 'stop_reason', None)})."
        )
    return parsed, response


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
        ExtractionError: if the model refuses or returns unparseable output.
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

    data, response = _parse(client, model, messages)
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
                    _image_block(image_b64, media_type),
                    {
                        "type": "text",
                        "text": (
                            "Re-examine the drawing carefully, paying special attention "
                            f"to these unclear areas: {unclear}. Zoom mentally into the "
                            "dimension callouts and produce a corrected, complete "
                            "extraction. Improve confidence only if the drawing genuinely "
                            "supports it."
                        ),
                    },
                ],
            }
        )
        try:
            requeried, _ = _parse(client, model, messages)
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
