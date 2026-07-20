"""Static self-audit of the COM builder's OWN source (mirror of macro_audit.py).

`pipeline/macro_audit.py` scans generated ``.vba`` text for banned/invented APIs.
There was no equivalent guard for `pipeline/solidworks_builder.py` itself — the
direct-COM builder that runs under both ``--engine com`` and ``--engine vba``. This
module is that guard: it scans the builder's Python source for

  * invented / nonexistent SOLIDWORKS APIs (E004: ``GetModelBoundingBox``);
  * the E006 anti-pattern of re-selecting a closed sketch BY NAME
    (``SelectByID2(<name>, "SKETCH", …)``) instead of consuming the active sketch;
  * inline VARIANT construction that bypasses :mod:`pipeline.com_marshal`
    (enforces Step 2's centralization — makes it structural, not aspirational).

Severity mirrors macro_audit: ``error`` must never ship. :func:`audit_builder` is
wired into the test suite (``tests/test_com_builder_audit.py``) so a regression that
reintroduces any of these fails CI/pytest, exactly like macro_audit's contract.

Design note — why not an allowlist of *every* SOLIDWORKS method: enumerating every
valid API to flag "unknown" calls is brittle (false-positives on legitimate new
APIs and on ordinary Python method calls in the same source). The high-value,
low-false-positive checks are the specific banned patterns + the VARIANT-bypass
rule, which is what this implements.
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline.macro_audit import AuditReport, Finding

BUILDER_PATH = Path(__file__).resolve().parent / "solidworks_builder.py"

# (regex, rule_id, human reason). Matched case-sensitively on non-comment source.
_BANNED_SOURCE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\bGetModelBoundingBox\b", "E004",
     "IModelDoc2.GetModelBoundingBox does not exist (invented API); read the box "
     "from the solid body via IBody2.GetBodyBox."),
    (r'SelectByID2\([^,\n]*Name[^,\n]*,\s*"SKETCH"', "E006",
     "re-selecting a closed sketch by name is unreliable; consume the active sketch."),
    (r"\bVARIANT\(", "E-CENTRALIZE",
     "inline VARIANT construction bypasses pipeline.com_marshal; build every "
     "VARIANT through com_marshal (null_dispatch/point_variant/double_array_variant)."),
)


def _noncomment_lines(text: str) -> list[tuple[int, str]]:
    """(1-indexed line no, line) for lines that are not pure ``#`` comments."""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        out.append((i, line))
    return out


def audit_source(text: str, filename: str = "solidworks_builder.py") -> AuditReport:
    """Audit builder source text. Errors mean a known failure mode was reintroduced."""
    report = AuditReport()
    lines = _noncomment_lines(text)
    for pat, rule_id, reason in _BANNED_SOURCE_PATTERNS:
        rx = re.compile(pat)
        hit_lines = [ln for ln, line in lines if rx.search(line)]
        for ln in hit_lines:
            report.findings.append(Finding(
                "error", rule_id, filename,
                f"line {ln}: banned pattern /{pat}/ — {reason}"))
    return report


def audit_builder() -> AuditReport:
    """Audit the installed ``pipeline/solidworks_builder.py`` source."""
    return audit_source(BUILDER_PATH.read_text(encoding="utf-8"), BUILDER_PATH.name)
