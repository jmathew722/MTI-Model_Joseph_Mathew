"""Shared quantity-language parser for US drafting hole/feature callouts.

One utility used by BOTH extraction and the overview cross-check so the two
tiers count the same way (learning-loop 2026-07-09, Issue B "hole/thread count
mismatch"). Handles the common forms:

    ".406 DIA THRU (2) HL'S"        -> 2
    ".422 DIA 6-HOLES"              -> 6
    "1/4-20 UNC 4 PLACES"           -> 4
    "(6) HLS"                       -> 6
    "3 PL"  /  "3 PLCS"  /  "2 REQD" -> 3 / 3 / 2
    "R.531 TYP"                     -> qualifier TYP (count defaults to 1)

Public entry points: :func:`parse_quantity`, :func:`is_typ`.
"""
from __future__ import annotations

import re

# Quantity patterns, tried in order; the first match wins. Each captures the
# integer count in group 1. Ordered specific -> general.
_QTY_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\((\d{1,3})\)\s*(?:hl|hls|hl's|hole|holes|pl|pls|pl's|plc|plcs|plcs?|places?)\b", re.I),
    re.compile(r"\b(\d{1,3})\s*[-–]?\s*(?:holes?|hls?|hl's)\b", re.I),
    re.compile(r"\b(\d{1,3})\s*(?:places?|plc?s?|pl's|pl)\b", re.I),
    re.compile(r"\b(\d{1,3})\s*(?:req'?d|reqd|required|x)\b", re.I),
)
_TYP_RE = re.compile(r"\btyp(ical)?\b", re.I)


def is_typ(text: str) -> bool:
    """True if the callout carries a TYP / TYPICAL qualifier (value repeats)."""
    return bool(_TYP_RE.search(text or ""))


def parse_quantity(text: str, default: int = 1) -> int:
    """The instance count a callout specifies (e.g. ``(2) HL'S`` -> 2). Returns
    ``default`` (1) when the callout names no explicit quantity."""
    t = text or ""
    for pat in _QTY_PATTERNS:
        m = pat.search(t)
        if m:
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
    return default
