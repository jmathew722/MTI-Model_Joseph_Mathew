"""Batch processing for many drawings (scale-to-thousands triage).

Walks a directory, runs the full Phase-1 + macro-generation pipeline for each
input, and writes a single ``batch_summary.csv`` so a large set of drawings can
be triaged at a glance: which are READY, which are BLOCKED (and why), and each
one's drawing-completeness scores.

Two input kinds are handled:
  * drawings (``.pdf/.png/.jpg/.tif``) — extracted via the injected ``extract_fn``
    (the Claude Vision call; costs API credits).
  * ``*_extraction.json`` — loaded directly (no API call, free re-runs).

Every input always gets its extraction + verification report persisted (even
BLOCKED), mirroring the single-part path. The extractor is injected so this
module — and its tests — never import the API client.

Public entry points: :func:`iter_inputs`, :func:`run_batch`.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from pipeline.macro_generator import MacroGenerationError, generate_macro_package
from pipeline.validator import format_verification_report, run_verification
from utils.logger import get_logger

log = get_logger()

DRAWING_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
ExtractFn = Callable[[Path], dict]

# Common SolidWorks 2024 part templates, used if SOLIDWORKS_TEMPLATE_PATH is unset.
_TEMPLATE_FALLBACKS = (
    r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\Part.PRTDOT",
    r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\Part.PRTDOT",
)


def resolve_part_template(template_path: Optional[str] = None) -> Optional[str]:
    """Resolve a usable .prtdot template: explicit arg > env > known fallbacks."""
    import os

    cand = template_path or os.getenv("SOLIDWORKS_TEMPLATE_PATH")
    if cand and Path(cand).exists():
        return cand
    for fb in _TEMPLATE_FALLBACKS:
        if Path(fb).exists():
            return fb
    return cand  # may be None / nonexistent; create_new_part will report clearly


def build_sldprt_for_part(sw_app, model, part_dir: Path, name: str,
                          template_path: Optional[str] = None) -> str:
    """Build one validated part into ``<part_dir>/<name>.sldprt`` over COM and
    write a ``<name>_model_check.txt``. Non-strict: a fragile feature is skipped
    (and documented) rather than aborting the build. Returns the saved path."""
    from pipeline.model_validator import validate_model
    from pipeline.solidworks_builder import build_model, save_model

    if not model.part_name:
        model.part_name = name
    template = resolve_part_template(template_path)

    skipped: list[tuple[str, str, str]] = []
    _, sw_doc = build_model(
        sw_app, model, output_dir=part_dir, template_path=template,
        strict=False, skipped_out=skipped,
    )
    sldprt = save_model(sw_doc, name, part_dir)

    vreport = validate_model(sw_doc, model)
    lines = [f"MODEL CHECK — {name}", "=" * 40, f"Saved: {sldprt}", ""]
    for p in vreport.get("passed", []):
        lines.append(f"[PASS] {p}")
    for f in vreport.get("failed", []):
        lines.append(f"[FAIL] {f}")
    for w in vreport.get("warnings", []):
        lines.append(f"[WARN] {w}")
    if skipped:
        lines.append("")
        lines.append("Skipped features (build non-strict — verify manually):")
        for fid, ftype, reason in skipped:
            lines.append(f"  - {fid} ({ftype}): {reason}")
    lines.append("")
    lines.append(f"Overall model validation: {'PASSED' if vreport.get('ok') else 'completed with issues'}.")
    (part_dir / f"{name}_model_check.txt").write_text("\n".join(lines), encoding="utf-8")

    try:
        sw_app.CloseDoc(sw_doc.GetTitle())
    except Exception:
        pass
    return sldprt


@dataclass
class BatchRow:
    source: str
    part: str
    status: str  # READY | BLOCKED | ERROR
    macro_readiness: float
    geometry_completeness: float
    dimension_completeness: float
    consistency: float
    feature_confidence: float
    n_macros: int
    n_needs_review: int
    n_skipped: int
    detail: str


def iter_inputs(directory: Path) -> list[tuple[Path, bool]]:
    """Return ``(path, is_json)`` for every processable input in ``directory``.

    ``*_extraction.json`` files are processed as saved extractions; other
    supported drawing files are processed via extraction. Sorted for
    deterministic ordering. A drawing and its own ``*_extraction.json`` can
    coexist; both are listed (the JSON is the free path).
    """
    items: list[tuple[Path, bool]] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        if p.name.endswith("_extraction.json"):
            items.append((p, True))
        elif p.suffix.lower() in DRAWING_EXTS:
            items.append((p, False))
    return items


def _scores(readiness: dict) -> dict:
    keys = ("macro_readiness", "geometry_completeness", "dimension_completeness",
            "consistency", "feature_confidence")
    return {k: float(readiness.get(k, 0.0)) for k in keys}


def process_drawing_data(drawing_data: dict, source: str, output_dir: Path,
                         sw_app=None, template_path: Optional[str] = None) -> BatchRow:
    """Verify + (if READY) generate macros for one already-loaded extraction.

    When ``sw_app`` is provided (a connected SolidWorks application), a real
    ``.sldprt`` is also built into the part folder — making the 3D model a
    standard output of every run, not just the VBA macros.
    """
    model, report = run_verification(drawing_data)
    scores = _scores(report.readiness or {})
    part = model.display_name if model is not None else (
        drawing_data.get("part_number") or drawing_data.get("part_name") or "part"
    )

    # Always persist the extraction + report (READY or BLOCKED), like the single path.
    part_dir = output_dir / (part.replace(" ", "_") or "part")
    part_dir.mkdir(parents=True, exist_ok=True)
    (part_dir / f"{part.replace(' ', '_') or 'part'}_extraction.json").write_text(
        json.dumps(drawing_data, indent=2), encoding="utf-8"
    )
    verification_text = format_verification_report(model, report)
    (part_dir / f"{part.replace(' ', '_') or 'part'}_verification_report.txt").write_text(
        verification_text, encoding="utf-8"
    )

    if model is None or not report.ok:
        return BatchRow(source, part, "BLOCKED", **scores, n_macros=0, n_needs_review=0,
                        n_skipped=0, detail="; ".join(report.errors)[:300])
    try:
        pkg = generate_macro_package(model, drawing_data, verification_text, output_dir)
    except MacroGenerationError as e:
        return BatchRow(source, part, "ERROR", **scores, n_macros=0, n_needs_review=0,
                        n_skipped=0, detail=str(e)[:300])
    n_macros = sum(1 for s in pkg.steps if s.macro_file.endswith(".vba"))

    # Build the real .sldprt into the part folder whenever SolidWorks is available,
    # so the 3D model is a required output of every run alongside the text files.
    detail = ""
    if sw_app is not None:
        try:
            sldprt = build_sldprt_for_part(sw_app, model, part_dir, part_dir.name, template_path)
            log.info("Built .sldprt for %s: %s", part, sldprt)
        except Exception as e:  # a build failure must not lose the macros/text output
            detail = f"sldprt build failed: {type(e).__name__}: {e}"[:300]
            log.warning("%s: %s", part, detail)

    return BatchRow(source, part, "READY", **scores, n_macros=n_macros,
                    n_needs_review=len(pkg.needs_review), n_skipped=len(pkg.skipped), detail=detail)


def run_batch(
    directory: Path,
    output_dir: Path,
    extract_fn: Optional[ExtractFn] = None,
    sw_app=None,
    template_path: Optional[str] = None,
) -> tuple[list[BatchRow], Path]:
    """Process every input in ``directory`` and write ``batch_summary.csv``.

    ``extract_fn`` is required only if drawing files (not just ``*_extraction.json``)
    are present; it maps a drawing path to an extraction dict. When ``sw_app`` is
    given, each READY part is also built into a real ``.sldprt``.
    """
    directory = Path(directory)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[BatchRow] = []

    for path, is_json in iter_inputs(directory):
        log.info("batch: processing %s", path.name)
        try:
            if is_json:
                drawing_data = json.loads(path.read_text(encoding="utf-8"))
            else:
                if extract_fn is None:
                    rows.append(BatchRow(path.name, path.stem, "ERROR", 0, 0, 0, 0, 0,
                                         0, 0, 0, "No extractor provided for a drawing file."))
                    continue
                drawing_data = extract_fn(path)
            rows.append(process_drawing_data(drawing_data, path.name, output_dir,
                                             sw_app=sw_app, template_path=template_path))
        except Exception as e:  # one bad drawing must not sink the whole batch
            log.warning("batch: %s failed: %s", path.name, e)
            rows.append(BatchRow(path.name, path.stem, "ERROR", 0, 0, 0, 0, 0,
                                 0, 0, 0, f"{type(e).__name__}: {e}"[:300]))

    csv_path = write_batch_csv(rows, output_dir)
    log.info("batch: wrote %s (%d rows)", csv_path, len(rows))
    return rows, csv_path


def write_batch_csv(rows: list[BatchRow], output_dir: Path, name: str = "batch_summary.csv") -> Path:
    """Write a list of BatchRow to ``<output_dir>/<name>`` and return the path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / name
    fields = list(asdict(BatchRow("", "", "", 0, 0, 0, 0, 0, 0, 0, 0, "")).keys())
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return csv_path
