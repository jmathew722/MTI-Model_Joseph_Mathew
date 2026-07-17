"""Static self-validation of generated VBA macros (Phase 7 + Phase 10).

Every error logged in ``docs/solidworks-macro-error-log.md`` (E001-E010) taught a
rule. Unit tests guard some of them, but only on hand-written fixtures. This
module turns those rules into a **static auditor that runs over EVERY generated
package** — so a known failure mode can never silently ship again, regardless of
what the extractor produced.

The auditor is intentionally conservative: it flags only patterns we are certain
are wrong (banned/nonexistent APIs) or structurally required (balanced
``Sub``/``End Sub``, ``Option Explicit``, a PASS/FAIL log per feature macro). It
does NOT try to type-check VBA.

Severities:
  * ``error`` — a generator defect that must never ship (e.g. a nonexistent API).
    :func:`pipeline.macro_generator.generate_macro_package` raises on these.
  * ``warn``  — worth a human glance; recorded in the audit report, non-fatal.

Public entry points: :func:`audit_text`, :func:`audit_package`.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Banned / nonexistent API calls — each ties back to a logged failure (E0xx).
# A match anywhere in any generated macro is an ERROR: it means a generator
# regression reintroduced a call we proved does not exist or is wrong.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BannedApi:
    pattern: str            # regex, matched case-insensitively
    rule_id: str            # error-log id, e.g. "E004"
    reason: str
    replacement: str


BANNED_APIS: tuple[BannedApi, ...] = (
    BannedApi(
        r"\bGetModelBoundingBox\b",
        "E004",
        "IModelDoc2.GetModelBoundingBox does not exist (was an invented API).",
        "Read the box from the solid body: IBody2.GetBodyBox.",
    ),
    BannedApi(
        # The E006 anti-pattern: re-finding a closed sketch by name. Feature
        # calls must consume the ACTIVE sketch (recorder pattern), never look it
        # up by name after closing.
        r'SelectByID2\(\s*[A-Za-z_]\w*Name\b[^,]*,\s*"SKETCH"',
        "E006",
        "Re-selecting a closed sketch by name is unreliable (E006).",
        "Consume the active sketch directly, or re-find by ProfileFeature type/object.",
    ),
)

# Feature macros (NN_*.vba) must report their outcome; absence means a step can
# silently pass through with no PASS/FAIL trail — defeats stop-on-first-failure.
_FEATURE_MACRO_RE = re.compile(r"^\d\d_.*\.vba$")
# Macros that legitimately need no per-step body log (handled by their own logic).
_LOG_EXEMPT = {"README.md"}


@dataclass
class Finding:
    severity: str           # "error" | "warn"
    rule_id: str
    file: str
    message: str


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [asdict(f) for f in self.findings],
        }


def audit_text(filename: str, text: str) -> list[Finding]:
    """Audit a single macro's source text. Returns all findings for it."""
    findings: list[Finding] = []
    lowered = text  # regexes use IGNORECASE; keep original for line context

    # 1) Banned / nonexistent APIs — hard errors.
    for api in BANNED_APIS:
        if re.search(api.pattern, lowered, re.IGNORECASE):
            findings.append(Finding(
                "error", api.rule_id, filename,
                f"Banned API match /{api.pattern}/: {api.reason} Use: {api.replacement}",
            ))

    if filename.endswith(".vba"):
        # 2) Option Explicit (catches undeclared-variable typos at compile time).
        if "Option Explicit" not in text:
            findings.append(Finding(
                "warn", "STRUCT", filename, "Missing 'Option Explicit'.",
            ))
        # 3) Balanced Sub / End Sub and Function / End Function.
        n_sub = len(re.findall(r"^\s*Sub\b", text, re.MULTILINE))
        n_endsub = len(re.findall(r"^\s*End Sub\b", text, re.MULTILINE))
        if n_sub != n_endsub:
            findings.append(Finding(
                "error", "STRUCT", filename,
                f"Unbalanced Sub/End Sub ({n_sub} Sub, {n_endsub} End Sub).",
            ))
        n_fn = len(re.findall(r"^\s*(?:Public |Private )?Function\b", text, re.MULTILINE))
        n_endfn = len(re.findall(r"^\s*End Function\b", text, re.MULTILINE))
        if n_fn != n_endfn:
            findings.append(Finding(
                "error", "STRUCT", filename,
                f"Unbalanced Function/End Function ({n_fn} Function, {n_endfn} End Function).",
            ))
        # 4) Feature macros must leave a PASS/FAIL trail.
        if _FEATURE_MACRO_RE.match(filename) and "LogResult" not in text:
            findings.append(Finding(
                "warn", "STRUCT", filename,
                "Feature macro never calls LogResult — no PASS/FAIL trail.",
            ))
    return findings


def audit_package(macros_dir: Path | str) -> AuditReport:
    """Audit every ``.vba`` file in a generated ``macros/`` directory."""
    macros_dir = Path(macros_dir)
    report = AuditReport()
    for path in sorted(macros_dir.glob("*.vba")):
        report.findings.extend(audit_text(path.name, path.read_text(encoding="utf-8")))
    return report


def write_audit_report(report: AuditReport, out_path: Path | str) -> Path:
    out_path = Path(out_path)
    out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# Dimensioning-architecture check (2026-07-17): anchor annotations must ship
# --------------------------------------------------------------------------- #
# Feature types emitted through the generic feature-macro loop (the paths that
# receive the DIMENSION ANCHORS block). Slot decompositions and the circular-
# pattern trio carry their anchoring in their own canonical schema blocks.
_ANCHOR_CHECKED_TYPES = frozenset({
    "extrude_boss", "extrude_cut", "hole", "thread", "revolve", "mirror", "pattern",
})


def check_anchor_annotations(model, pkg, macros_dir: Path | str) -> list[str]:
    """Every generated macro for a feature with EXPLICIT PositionAnchor records
    must contain its anchor annotations — each anchor's dimension ids and value
    — so the drawing's dimensioning scheme demonstrably survived to the macro.
    Returns error strings; the generator raises on any (fails loudly)."""
    macros_dir = Path(macros_dir)
    errors: list[str] = []
    feats = {f.id: f for f in model.features}
    for step in pkg.steps:
        feat = feats.get(step.feature_id)
        if (feat is None or not feat.anchors or step.status != "generated"
                or step.feature_type not in _ANCHOR_CHECKED_TYPES
                or not str(step.macro_file).endswith(".vba")):
            continue
        path = macros_dir / step.macro_file
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "DIMENSION ANCHORS" not in text:
            errors.append(f"{step.macro_file}: feature {feat.id} has explicit "
                          f"anchors but no DIMENSION ANCHORS block was emitted")
            continue
        for a in feat.anchors:
            val = f"{float(a.value):.6g}"
            if val not in text:
                errors.append(f"{step.macro_file}: anchor value {val} "
                              f"({a.axis} from {a.anchor_ref}) missing from macro")
            for did in a.dimension_ids:
                if did not in text:
                    errors.append(f"{step.macro_file}: anchor dimension {did} "
                                  f"missing from macro")
    return errors
