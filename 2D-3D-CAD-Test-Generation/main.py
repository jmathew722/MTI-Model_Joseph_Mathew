"""2D -> 3D SolidWorks Pipeline — entry point.

Phase 1 (any OS): prepare image -> extract with Claude Vision -> verify.
Phase 2: generate SolidWorks VBA macros (default, any OS — run the macros on any
SolidWorks machine, no Python needed there), or drive SolidWorks directly over
COM (--engine com, Windows + SolidWorks required).

Usage:
    # Extract + verify only (no SolidWorks needed — runs anywhere):
    python main.py --drawing path/to/drawing.pdf --validate-only --debug

    # Full Phase 1 + VBA macro package (runs anywhere):
    python main.py --drawing path/to/drawing.pdf --output ./output

    # Regenerate macros from a saved extraction (no API call):
    python main.py --from-json debug_extraction.json --output ./output

    # Direct COM build (Windows + SolidWorks 2024):
    python main.py --drawing path/to/drawing.pdf --engine com
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()


def _extract_cache_dir(args) -> Path | None:
    """The on-disk extraction cache dir, unless disabled with --no-extract-cache."""
    if getattr(args, "no_extract_cache", False):
        return None
    return Path(args.output) / ".extraction_cache"


def _prepare_and_extract(args) -> dict | None:
    """Stages 1-2: image prep + Claude extraction. Returns the extraction dict."""
    console.print("[1/4] Preparing drawing image...")
    from utils.image_prep import ImagePrepError, prepare_image

    try:
        prepared = prepare_image(args.drawing, page=args.page, return_details=True)
    except ImagePrepError as e:
        console.print(f"[red]Image preparation failed:[/red] {e}")
        return None
    for w in prepared.warnings:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    console.print(f"  Prepared {prepared.width}x{prepared.height} PNG (page {prepared.page}).")

    console.print("[2/4] Extracting drawing data with Claude Vision...")
    from pipeline.extractor import ExtractionError, extract_drawing_data

    usage: dict[str, int] = {}
    try:
        return extract_drawing_data(
            prepared.base64,
            media_type=prepared.media_type,
            prep_warnings=prepared.warnings,
            cache_dir=_extract_cache_dir(args),
            usage_out=usage,
        )
    except EnvironmentError as e:
        console.print(f"[red]Extraction failed (configuration):[/red] {e}")
    except ExtractionError as e:
        console.print(f"[red]Extraction failed:[/red] {e}")
    except Exception as e:
        # The extractor wraps an external API: auth, rate-limit, and network
        # failures surface as the SDK's own exception types. Present cleanly.
        console.print(f"[red]Extraction failed (API error):[/red] {type(e).__name__}: {e}")
    return None


def _force_utf8_console() -> None:
    """Avoid UnicodeEncodeError on Windows cp1252 consoles (failure E008).

    The verification report and rich panels emit non-ASCII ('->', box glyphs).
    On a legacy code-page console that crashes the run AFTER a paid extraction.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _save_extraction(output_dir: Path, folder_name: str, drawing_data: dict) -> Path:
    """Persist the extraction into the output folder so a paid run is never lost,
    READY or BLOCKED (failure E008). Returns the path written."""
    part_dir = output_dir / (folder_name.replace(" ", "_") or "part")
    part_dir.mkdir(parents=True, exist_ok=True)
    path = part_dir / f"{folder_name.replace(' ', '_') or 'part'}_extraction.json"
    path.write_text(json.dumps(drawing_data, indent=2), encoding="utf-8")
    return path


def _extract_one_drawing(path: Path, page: int, cache_dir: Path | None) -> dict:
    """Image-prep + Claude extraction for a single drawing file (used by batch)."""
    from utils.image_prep import prepare_image
    from pipeline.extractor import extract_drawing_data

    prepared = prepare_image(str(path), page=page, return_details=True)
    return extract_drawing_data(
        prepared.base64, media_type=prepared.media_type, prep_warnings=prepared.warnings,
        cache_dir=cache_dir,
    )


def _run_batch(args) -> int:
    """Process every input in a directory, print a summary table, write CSV."""
    from rich.table import Table

    from pipeline.batch import run_batch

    directory = Path(args.batch)
    if not directory.is_dir():
        console.print(f"[red]--batch path is not a directory:[/red] {directory}")
        return 2

    output_dir = Path(args.output)
    cache_dir = _extract_cache_dir(args)
    rows, csv_path = run_batch(
        directory, output_dir,
        extract_fn=lambda p: _extract_one_drawing(p, args.page, cache_dir),
    )
    if not rows:
        console.print(f"[yellow]No drawings or *_extraction.json files found in[/yellow] {directory}")
        return 0

    table = Table(title=f"Batch summary ({len(rows)} inputs)")
    for col in ("Part", "Status", "Readiness", "Macros", "Review", "Skipped", "Detail"):
        table.add_column(col, overflow="fold")
    status_color = {"READY": "green", "BLOCKED": "red", "ERROR": "yellow"}
    for r in rows:
        color = status_color.get(r.status, "white")
        table.add_row(
            r.part, f"[{color}]{r.status}[/{color}]", f"{r.macro_readiness:.0%}",
            str(r.n_macros), str(r.n_needs_review), str(r.n_skipped), r.detail[:60],
        )
    console.print(table)
    n_ready = sum(1 for r in rows if r.status == "READY")
    console.print(f"  Summary CSV: {csv_path}  ({n_ready}/{len(rows)} READY)")
    return 0 if n_ready == len(rows) else 8


def main() -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(
        description="Convert a 2D engineering drawing into a SolidWorks 3D model."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--drawing", help="Path to the 2D drawing (PDF, PNG, JPG, TIFF).")
    src.add_argument(
        "--from-json",
        help="Skip extraction: load a previously saved extraction JSON (e.g. debug_extraction.json).",
    )
    src.add_argument(
        "--batch",
        help="Process every drawing / *_extraction.json in a directory and write batch_summary.csv.",
    )
    parser.add_argument("--output", default="./output", help="Output directory.")
    parser.add_argument("--page", type=int, default=1, help="Page to use for multi-page PDFs (default 1).")
    parser.add_argument("--debug", action="store_true", help="Save intermediate extraction JSON.")
    parser.add_argument(
        "--no-extract-cache",
        action="store_true",
        help="Disable the on-disk extraction cache (re-extract even if an identical image was seen).",
    )
    parser.add_argument(
        "--engine",
        choices=("vba", "com"),
        default="vba",
        help="Build engine: 'vba' generates SolidWorks macros (default, any OS); "
        "'com' drives SolidWorks directly (Windows only).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Extract and verify only; do not generate macros or touch SolidWorks.",
    )
    args = parser.parse_args()

    console.print(Panel("2D -> 3D SolidWorks Pipeline", style="bold blue"))

    # --- Batch mode: process a whole directory and write a triage CSV ---
    if args.batch:
        return _run_batch(args)

    # --- [1-2/4] Source the extraction ---
    if args.from_json:
        json_path = Path(args.from_json)
        if not json_path.exists():
            console.print(f"[red]Extraction JSON not found:[/red] {json_path}")
            return 2
        console.print(f"[1-2/4] Loading extraction from {json_path} (skipping API)...")
        try:
            drawing_data = json.loads(json_path.read_text())
        except json.JSONDecodeError as e:
            console.print(f"[red]Could not parse {json_path}:[/red] {e}")
            return 2
    else:
        drawing_data = _prepare_and_extract(args)
        if drawing_data is None:
            return 3

    if args.debug and not args.from_json:
        debug_path = Path("debug_extraction.json")
        debug_path.write_text(json.dumps(drawing_data, indent=2))
        console.print(f"  Debug: extraction saved to {debug_path}")

    # --- [3/4] Verification (Phase 1 gate) ---
    console.print("[3/4] Verifying extracted data...")
    from pipeline.validator import format_verification_report, run_verification

    model, report = run_verification(drawing_data)
    verification_text = format_verification_report(model, report)
    console.print(verification_text)

    output_dir = Path(args.output)
    # Folder name: prefer the validated model's display name; fall back to the
    # raw extraction's part number/name so a BLOCKED run (model may be None) still
    # lands somewhere sensible.
    folder_name = (
        model.display_name if model is not None
        else (drawing_data.get("part_number") or drawing_data.get("part_name") or "part")
    )
    # Always persist BOTH the extraction and the verification report — READY or
    # BLOCKED — so a paid extraction is never lost and BLOCKED is re-runnable via
    # --from-json with no API cost (failure E008).
    extraction_path = _save_extraction(output_dir, folder_name, drawing_data)
    console.print(f"  Extraction saved to {extraction_path}")
    if model is not None:
        report_path = extraction_path.parent / f"{folder_name}_verification_report.txt"
        report_path.write_text(verification_text, encoding="utf-8")
        console.print(f"  Report written to {report_path}")

    if model is None or not report.ok:
        console.print(
            Panel(
                "[red]BLOCKED — resolve the issues above, then re-run.[/red]\n"
                "Nothing was sent to SolidWorks and no macros were generated.",
                style="red",
            )
        )
        return 4

    if args.validate_only:
        console.print(Panel("[green]READY TO BUILD. Exiting (--validate-only).[/green]", style="green"))
        return 0

    # --- [4/4] Build ---
    if args.engine == "vba":
        console.print("[4/4] Generating SolidWorks VBA macro package...")
        from pipeline.macro_generator import generate_macro_package

        pkg = generate_macro_package(model, drawing_data, verification_text, output_dir)
        n_macros = sum(1 for s in pkg.steps if s.macro_file.endswith(".vba"))
        lines = [
            f"[green]Macro package complete:[/green] {pkg.root}",
            f"  {n_macros} macros in {pkg.macros_dir}",
            f"  Build plan: {pkg.build_plan_json}",
        ]
        if pkg.skipped:
            lines.append(
                f"  [yellow]{len(pkg.skipped)} feature(s) skipped (prohibited):[/yellow] "
                + ", ".join(s.feature_id for s in pkg.skipped)
            )
        if pkg.needs_review:
            lines.append(
                f"  [yellow]{len(pkg.needs_review)} macro(s) need manual review:[/yellow] "
                + ", ".join(s.feature_id for s in pkg.needs_review)
            )
        lines.append("  Next: copy the package folder to a SolidWorks machine and follow macros/README.md.")
        console.print(Panel("\n".join(lines), style="green"))
        return 0

    # --- engine == "com": direct COM build (Windows + SolidWorks) ---
    console.print("[4/6] Connecting to SolidWorks...")
    from pipeline.solidworks_builder import (
        PlatformError,
        SolidWorksError,
        build_model,
        connect_to_solidworks,
    )

    try:
        sw_app = connect_to_solidworks()
    except PlatformError as e:
        console.print(f"[red]{e}[/red]")
        return 5
    except SolidWorksError as e:
        console.print(f"[red]Could not connect to SolidWorks:[/red] {e}")
        return 5

    console.print("[5/6] Building 3D model in SolidWorks...")
    import os

    template_path = os.getenv("SOLIDWORKS_TEMPLATE_PATH") or None
    try:
        output_path, sw_doc = build_model(
            sw_app, model, output_dir=args.output, template_path=template_path
        )
    except SolidWorksError as e:
        console.print(f"[red]Build failed:[/red] {e}")
        if e.partial_path:
            console.print(f"  Partial model saved to: {e.partial_path}")
        return 6

    console.print("[6/6] Validating built model...")
    from pipeline.model_validator import validate_model

    vreport = validate_model(sw_doc, model)
    for p in vreport["passed"]:
        console.print(f"  [green]PASS[/green] {p}")
    for f in vreport["failed"]:
        console.print(f"  [red]FAIL[/red] {f}")
    for w in vreport["warnings"]:
        console.print(f"  [yellow]WARN[/yellow] {w}")

    style = "green" if vreport.get("ok") else "yellow"
    console.print(
        Panel(
            f"[{style}]Pipeline complete.[/{style}]\nSaved to: {output_path}\n"
            f"Model validation: {'PASSED' if vreport.get('ok') else 'completed with issues'}.",
            style=style,
        )
    )
    return 0 if vreport.get("ok") else 7


if __name__ == "__main__":
    sys.exit(main())
