"""Template-based VBA emission for Stage 7 (2026-07-12 hardening, Task 2).

Every per-feature VBA fragment is a named template file under
``pipeline/macro_templates/`` with ``%%NAME`` placeholders, filled EXCLUSIVELY
from one feature's record dict. The fill is strict in both directions:

* a placeholder the record does not provide  → :class:`TemplateFillError`
  (never a silently-empty hole in the VBA);
* a record key the template never consumes   → :class:`TemplateFillError`
  (a leftover key means the record builder and the template disagree about
  what this feature emits — the exact drift class that produced the 158-C
  cross-feature evidence).

Because ``fill()`` receives exactly ONE feature's record, it is structurally
impossible for a template to reference another feature's values — the
cross-contamination class dies at the API boundary, and the macro echo check
(:mod:`pipeline.macro_echo`) then proves the emitted literals round-trip to the
build plan.

Delimiter is ``%%`` (not ``$``): VBA itself uses ``$`` (``Left$``, ``Format$``,
``Chr$``) and ``@`` ("Point1@Origin"), so both are unusable as placeholder
markers. ``%%`` never appears in emitted VBA.

Template header comments cite the ``METHODS.md`` recipe they implement, per the
Stage 7 documentation contract; the static macro audit
(:func:`pipeline.macro_audit.audit_text`) is run over every template once at
test time (banned-API rules), so generated output inherits the audit.
"""
from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any

TEMPLATE_DIR = Path(__file__).parent / "macro_templates"


class TemplateFillError(Exception):
    """A template/record mismatch — always a generation bug, never user data."""


class VbaTemplate(Template):
    """string.Template tuned for VBA text: ``%%NAME`` placeholders, uppercase
    identifiers only, so VBA's own ``$``/``@`` usage can never collide."""

    delimiter = "%%"
    idpattern = r"[A-Z][A-Z0-9_]*"


_cache: dict[str, VbaTemplate] = {}


def template_names() -> list[str]:
    """Every template file name (for the static template audit)."""
    return sorted(p.name for p in TEMPLATE_DIR.glob("*.vba.tmpl"))


def _load(name: str) -> VbaTemplate:
    if name not in _cache:
        path = TEMPLATE_DIR / name
        if not path.is_file():
            raise TemplateFillError(f"Unknown macro template {name!r} (looked in {TEMPLATE_DIR})")
        _cache[name] = VbaTemplate(path.read_text(encoding="utf-8"))
    return _cache[name]


def fill(name: str, record: dict[str, Any]) -> str:
    """Fill template *name* from exactly one feature's *record*.

    Strict in both directions (see module docstring). All values are stringified
    verbatim — number formatting is the record builder's job, so the emitted
    literal is the one the echo check parses back.
    """
    tmpl = _load(name)
    idents = set(tmpl.get_identifiers())
    provided = set(record)
    missing = idents - provided
    extra = provided - idents
    if missing:
        raise TemplateFillError(
            f"Template {name}: record is missing placeholder value(s) "
            f"{sorted(missing)} — refusing to emit VBA with holes.")
    if extra:
        raise TemplateFillError(
            f"Template {name}: record carries unused key(s) {sorted(extra)} — "
            f"the record builder and template disagree about what this feature emits.")
    return tmpl.substitute({k: str(v) for k, v in record.items()})
