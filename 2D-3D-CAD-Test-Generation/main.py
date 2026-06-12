"""2D -> 3D SolidWorks Pipeline — entry point.

Usage:
    # Extract + validate only (no SolidWorks needed — runs anywhere):
    python main.py --drawing path/to/drawing.pdf --validate-only --debug

    # Full pipeline (Windows + SolidWorks 2024 required):
    python main.py --drawing path/to/drawing.pdf --output ./output
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a 2D engineering drawing into a SolidWorks 3D model."
    )
    parser.add_argument("--drawing", required=True, help="Path to the 2D drawing (PDF, PNG, JPG, TIFF).")
    parser.add_argument("--output", default="./output", help="Output directory for the .sldprt file.")
    parser.add_argument("--page", type=int, default=1, help="Page to use for multi-page PDFs (default 1).")
    parser.add_argument("--debug", action="store_true", help="Save intermediate extraction JSON.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Extract and validate only; do not connect to SolidWorks.",
    )
    args = parser.parse_args()

    console.print(Panel("2D -> 3D SolidWorks Pipeline", style="bold blue"))

    # --- [1/6] Prepare image ---
    console.print("[1/6] Preparing drawing image...")
    from utils.image_prep import ImagePrepError, prepare_image

    try:
        prepared = prepare_image(args.drawing, page=args.page, return_details=True)
    except ImagePrepError as e:
        console.print(f"[red]Image preparation failed:[/red] {e}")
        return 2
    for w in prepared.warnings:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    console.print(f"  Prepared {prepared.width}x{prepared.height} PNG (page {prepared.page}).")

    # --- [2/6] Extract dimensions ---
    console.print("[2/6] Extracting dimensions with Claude Vision...")
    from pipeline.extractor import ExtractionError, extract_drawing_data

    try:
        drawing_data = extract_drawing_data(
            prepared.base64,
            media_type=prepared.media_type,
            prep_warnings=prepared.warnings,
        )
    except EnvironmentError as e:
        console.print(f"[red]Extraction failed (configuration):[/red] {e}")
        return 3
    except ExtractionError as e:
        console.print(f"[red]Extraction failed:[/red] {e}")
        return 3
    except Exception as e:
        # The extractor wraps an external API: auth, rate-limit, and network
        # failures surface as the SDK's own exception types. Present cleanly
        # rather than dumping a traceback.
        console.print(f"[red]Extraction failed (API error):[/red] {type(e).__name__}: {e}")
        return 3

    if args.debug:
        debug_path = Path("debug_extraction.json")
        debug_path.write_text(json.dumps(drawing_data, indent=2))
        console.print(f"  Debug: extraction saved to {debug_path}")

    # --- [3/6] Validate extracted data ---
    console.print("[3/6] Validating extracted data...")
    from pipeline.validator import DrawingValidationError, validate_drawing_data

    try:
        validated = validate_drawing_data(drawing_data)
    except DrawingValidationError as e:
        console.print("[red]Validation failed — not proceeding to SolidWorks.[/red]")
        console.print(str(e.report))
        return 4
    console.print(
        f"  Validated: {len(validated.dimensions)} dimensions, "
        f"{len(validated.features)} features, confidence {validated.confidence:.2f}."
    )

    if args.validate_only:
        console.print(
            Panel("[green]Validation complete. Exiting (--validate-only).[/green]", style="green")
        )
        return 0

    # --- [4/6] Connect to SolidWorks ---
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

    # --- [5/6] Build the model ---
    console.print("[5/6] Building 3D model in SolidWorks...")
    import os

    template_path = os.getenv("SOLIDWORKS_TEMPLATE_PATH") or None
    try:
        output_path, sw_doc = build_model(
            sw_app, validated, output_dir=args.output, template_path=template_path
        )
    except SolidWorksError as e:
        console.print(f"[red]Build failed:[/red] {e}")
        if e.partial_path:
            console.print(f"  Partial model saved to: {e.partial_path}")
        return 6

    # --- [6/6] Validate the built model ---
    console.print("[6/6] Validating built model...")
    from pipeline.model_validator import validate_model

    report = validate_model(sw_doc, validated)
    for p in report["passed"]:
        console.print(f"  [green]PASS[/green] {p}")
    for f in report["failed"]:
        console.print(f"  [red]FAIL[/red] {f}")
    for w in report["warnings"]:
        console.print(f"  [yellow]WARN[/yellow] {w}")

    style = "green" if report.get("ok") else "yellow"
    console.print(
        Panel(
            f"[{style}]Pipeline complete.[/{style}]\nSaved to: {output_path}\n"
            f"Model validation: {'PASSED' if report.get('ok') else 'completed with issues'}.",
            style=style,
        )
    )
    return 0 if report.get("ok") else 7


if __name__ == "__main__":
    sys.exit(main())
