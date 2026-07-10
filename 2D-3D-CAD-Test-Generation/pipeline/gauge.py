"""Sheet-metal gauge-callout parser (learning-loop 2026-07-10, P2).

A sheet-metal thickness is often given as a GAUGE number, not a decimal — and
the standard drafting practice writes the decimal equivalent in parentheses:

    "12 GA. (.105)"   -> thickness .105 in, gauge 12 (decimal wins)
    "16 GA"           -> thickness from the material's gauge table
    "10 GAUGE"        -> ditto

Four thickness-reconciliation false positives this cycle (A001591E 12.0 vs .105,
A001561E 12.0 vs 10.0, A001581E 12.0/1.0 vs 1.88) all came from a naive
first-number grab that captured the GAUGE (12) as if it were the thickness. This
parser recognizes the gauge token, prefers the parenthetical decimal when present,
and otherwise converts the gauge via the material-appropriate table:

  * Manufacturers' Standard Gauge (steel / galvanized / stainless);
  * Brown & Sharpe / AWG (aluminum and other non-ferrous).

A gauge number ALONE is ambiguous across materials, so when the material is
unknown the parser refuses to guess and reports ``needs_material=True`` — the
caller flags rather than fabricating a thickness.

Public entry point: :func:`parse_thickness_callout`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Manufacturers' Standard Gauge (uncoated steel), thickness in inches. Used for
# steel / stainless / galvanized (the ferrous family) per common shop practice.
_MSG_STEEL_IN = {
    3: 0.2391, 4: 0.2242, 5: 0.2092, 6: 0.1943, 7: 0.1793, 8: 0.1644,
    9: 0.1495, 10: 0.1345, 11: 0.1196, 12: 0.1046, 13: 0.0897, 14: 0.0747,
    15: 0.0673, 16: 0.0598, 17: 0.0538, 18: 0.0478, 19: 0.0418, 20: 0.0359,
    21: 0.0329, 22: 0.0299, 23: 0.0269, 24: 0.0239, 25: 0.0209, 26: 0.0179,
    27: 0.0164, 28: 0.0149, 29: 0.0135, 30: 0.0120,
}
# Brown & Sharpe / AWG, thickness in inches. Used for aluminum & other non-ferrous.
_BROWN_SHARPE_IN = {
    3: 0.2294, 4: 0.2043, 5: 0.1819, 6: 0.1620, 7: 0.1443, 8: 0.1285,
    9: 0.1144, 10: 0.1019, 11: 0.0907, 12: 0.0808, 13: 0.0720, 14: 0.0641,
    15: 0.0571, 16: 0.0508, 17: 0.0453, 18: 0.0403, 19: 0.0359, 20: 0.0320,
    21: 0.0285, 22: 0.0253, 23: 0.0226, 24: 0.0201, 25: 0.0179, 26: 0.0159,
    27: 0.0142, 28: 0.0126, 29: 0.0113, 30: 0.0100,
}

# GAUGE token: "12 GA", "12 GA.", "12 GAUGE", "12GA", "#12 GA" (leading # tolerated).
_GAUGE_RE = re.compile(r"#?\s*(\d{1,2})\s*(?:ga\b\.?|gauge\b|gage\b)", re.I)
# A parenthetical decimal: "(.105)" or "(0.105)".
_PAREN_DEC_RE = re.compile(r"\(\s*(\d*\.\d+)\s*\)")
# Any bare decimal (leading-dot or normal), for the no-gauge case.
_DECIMAL_RE = re.compile(r"(\d*\.\d+)")

_FERROUS = ("steel", "stainless", "galv", "crs", "hrs", "iron", "ss ", "ss,", "carbon")
_NONFERROUS = ("alum", "aluminium", "aluminum", "6061", "5052", "3003", "brass",
               "copper", "bronze", "al ", "al,")


def _material_table(material: Optional[str]):
    """Pick the gauge table for a material string, or None if unknown/ambiguous."""
    if not material:
        return None
    m = f" {material.lower()} "
    if any(k in m for k in _FERROUS):
        return _MSG_STEEL_IN
    if any(k in m for k in _NONFERROUS):
        return _BROWN_SHARPE_IN
    return None


@dataclass
class ThicknessReading:
    thickness_in: Optional[float]  # decimal thickness in inches, if resolved
    gauge: Optional[int]           # gauge number, if the callout stated one
    source: str                    # decimal_with_gauge | gauge_table | decimal | none
    needs_material: bool = False   # gauge-only with unknown material -> caller flags
    note: str = ""


def parse_thickness_callout(text: str, material: Optional[str] = None) -> ThicknessReading:
    """Parse a thickness/gauge callout into a decimal thickness + gauge metadata.

    Resolution order:
      1. gauge token + parenthetical decimal  -> the decimal IS the thickness,
         the gauge is recorded as metadata (``decimal_with_gauge``);
      2. gauge token alone -> convert via the material table (``gauge_table``);
         unknown material -> ``needs_material=True``, no thickness (do not guess);
      3. no gauge token, a bare decimal -> that decimal (``decimal``);
      4. nothing usable -> ``source="none"``.
    """
    t = text or ""
    gm = _GAUGE_RE.search(t)
    if gm:
        gauge = int(gm.group(1))
        pd = _PAREN_DEC_RE.search(t)
        if pd:
            return ThicknessReading(
                thickness_in=float(pd.group(1)), gauge=gauge,
                source="decimal_with_gauge",
                note=f"{gauge} GA with stated decimal {pd.group(1)} in — decimal is authoritative.")
        table = _material_table(material)
        if table is not None and gauge in table:
            return ThicknessReading(
                thickness_in=table[gauge], gauge=gauge, source="gauge_table",
                note=f"{gauge} GA converted to {table[gauge]:.4f} in via the "
                     f"{'Manufacturers Standard (ferrous)' if table is _MSG_STEEL_IN else 'Brown & Sharpe (non-ferrous)'} "
                     f"table for material {material!r}.")
        return ThicknessReading(
            thickness_in=None, gauge=gauge, source="gauge_table", needs_material=True,
            note=(f"{gauge} GA with no parenthetical decimal and "
                  f"{'unknown' if not material else 'unrecognized'} material "
                  f"({material!r}) — gauge is ambiguous across materials; flag, do not guess."))
    # No gauge token: a bare decimal is the thickness as written.
    dm = _DECIMAL_RE.search(t)
    if dm:
        return ThicknessReading(thickness_in=float(dm.group(1)), gauge=None,
                                source="decimal", note="")
    return ThicknessReading(thickness_in=None, gauge=None, source="none")
