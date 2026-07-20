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


def _export_to_downloads(args, output_dir: Path, folder_name: str = "SolidWorksModel_Parts"):
    """Final pipeline step: copy every part's outputs (models + text files) into
    ``~/Downloads/<folder_name>`` so the deliverables land in one well-known place.

    Skips the internal extraction cache. Merges into any existing folder (updates
    files in place) rather than wiping it. Disabled with --no-export.
    """
    if getattr(args, "no_export", False):
        return None
    import shutil

    try:
        dest = Path.home() / "Downloads" / folder_name
        dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        for item in Path(output_dir).iterdir():
            if item.name == ".extraction_cache":
                continue  # internal cache, not a deliverable
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
            copied += 1
        console.print(f"  [green]Final step:[/green] {copied} item(s) copied to {dest}")
        return dest
    except Exception as e:
        console.print(f"  [yellow]Could not export to Downloads:[/yellow] {type(e).__name__}: {e}")
        return None


def _connect_solidworks_optional(args):
    """Connect to SolidWorks so .sldprt models are produced, unless --no-sldprt.

    Returns the SolidWorks app, or None when disabled or unavailable (off Windows,
    not installed, no license). A None result is non-fatal: the text reports and
    VBA macros are still produced; only the .sldprt is skipped (with a reason).
    """
    if getattr(args, "no_sldprt", False):
        return None
    from pipeline.solidworks_builder import (
        PlatformError,
        SolidWorksError,
        connect_to_solidworks,
    )

    try:
        app = connect_to_solidworks()
        console.print("  [green]SolidWorks connected[/green] — .sldprt models will be built each run.")
        return app
    except PlatformError as e:
        console.print(f"  [yellow]No .sldprt this run:[/yellow] {e}")
    except SolidWorksError as e:
        console.print(f"  [yellow]No .sldprt this run (SolidWorks unavailable):[/yellow] {e}")
    except Exception as e:
        console.print(f"  [yellow]No .sldprt this run:[/yellow] {type(e).__name__}: {e}")
    return None


def _extract_cache_dir(args) -> Path | None:
    """The on-disk extraction cache dir, unless disabled with --no-extract-cache."""
    if getattr(args, "no_extract_cache", False):
        return None
    return Path(args.output) / ".extraction_cache"


def _spec_lines_from_args(args) -> list[str]:
    """Operator must-meet specification lines from --requirements, if given.
    Read up-front (specs-first) so they shape extraction and Stage 2.5, not
    just the final compliance check."""
    req_path = getattr(args, "requirements", None)
    if not req_path or getattr(args, "skip_requirements_check", False):
        return []
    try:
        from pipeline.requirements_check import parse_requirements

        text = Path(req_path).read_text(encoding="utf-8", errors="replace")
        return [r["text"] for r in parse_requirements(text)]
    except OSError as e:
        console.print(f"  [yellow]Could not read --requirements file:[/yellow] {e}")
        return []


def _resolve_stage(args, drawing_data: dict, overview_analysis: dict | None = None):
    """Stage 2.5: resolve every ambiguity to a numeric value (chief-engineer pass).

    Returns ``(resolved_data, resolution)``. When --no-resolve is set, returns the
    data unchanged and ``None`` (the legacy v2 BLOCKED-gate behavior). Otherwise the
    returned ``resolved_data`` has every dimension carrying a ``resolved_value`` and
    every feature marked ``build_status='build'``; the resolution summary is printed.
    Operator must-meet specifications (--requirements) are a first-class input:
    a spec value clarifying an ambiguous dimension takes precedence (spec-driven).
    The Stage 1.5 overview analysis (when available) feeds resolution as tier 2.
    """
    if getattr(args, "no_resolve", False):
        return drawing_data, None
    from pipeline.resolver import resolve_extraction

    console.print("[2.5/4] Resolving ambiguities (chief-engineer pass)...")
    resolution = resolve_extraction(drawing_data,
                                    requirements=_spec_lines_from_args(args),
                                    overview_analysis=overview_analysis)
    _print_resolution_summary(resolution)
    # clean_extraction (resolved values, schema-valid) drives verification + build;
    # resolution.resolved_extraction (rich, annotated) is written to disk.
    return resolution.clean_extraction, resolution


def _print_resolution_summary(resolution) -> None:
    """Print the Stage 2.5 resolution summary + every MEDIUM/LOW/CRITICAL flag."""
    s = resolution.summary
    console.print(
        f"  Resolved {s.total_dimensions} dimension(s): "
        f"{s.total_dimensions - s.assumptions_made} confirmed, {s.assumptions_made} assumed "
        f"({s.critical_flags} critical, {s.low_flags} low, {s.medium_flags} medium). "
        f"Rebuild confidence {s.rebuild_confidence:.0%}."
    )
    surfaced = [f for f in resolution.flags if f.get("flag_tier") in ("MEDIUM", "LOW", "CRITICAL")]
    color = {"CRITICAL": "red", "LOW": "yellow", "MEDIUM": "cyan"}
    for f in surfaced:
        tier = f.get("flag_tier", "")
        by = f.get("resolved_by_tier", "")
        console.print(
            f"    [{color.get(tier, 'white')}]{tier}[/] {f.get('dimension_id', '')}"
            f"{f' [{by}]' if by else ''}: {f.get('human_note', '')}"
        )


def _prepare_and_extract(args) -> tuple[dict | None, dict | None]:
    """Stages 1-2: image prep + Stage 1.5 overview analysis + Claude extraction.

    Returns ``(extraction_dict, overview_analysis)`` — extraction is ``None`` on
    failure; overview_analysis is ``None`` whenever that stage was unavailable
    (it can only add signal, never block)."""
    console.print("[1/4] Preparing drawing image...")
    from utils.image_prep import ImagePrepError, prepare_image

    try:
        prepared = prepare_image(args.drawing, page=args.page, return_details=True)
    except ImagePrepError as e:
        console.print(f"[red]Image preparation failed:[/red] {e}")
        return None, None
    for w in prepared.warnings:
        console.print(f"  [yellow]warning:[/yellow] {w}")
    console.print(f"  Prepared {prepared.width}x{prepared.height} PNG (page {prepared.page}).")

    import os as _os
    from pipeline.extractor import DEFAULT_MODEL

    model = _os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL

    # ── Stage 1.5: Holistic Overview Analysis on the SAME prepared full-sheet
    # image (no re-rasterization) — cross-view relationships before extraction.
    console.print("[1.5/4] Holistic overview analysis (full sheet, cross-view)...")
    from pipeline.overview_analysis import STAGE_TAG, analyze_overview
    from pipeline.usage_log import record_run

    usage_ov: dict[str, int] = {}
    overview_analysis = analyze_overview(
        prepared.base64, media_type=prepared.media_type,
        cache_dir=_extract_cache_dir(args), usage_out=usage_ov,
    )
    if usage_ov.get("calls") or usage_ov.get("cache_hits"):
        part_hint = Path(args.drawing).stem
        record_run(Path(args.output), part_hint, model, usage_ov, stage=STAGE_TAG)
    if overview_analysis is not None:
        console.print(
            f"  Overview: {len(overview_analysis.get('views_detected', []) or [])} view(s), "
            f"{len(overview_analysis.get('cross_view_conflicts', []) or [])} conflict(s); "
            f"shape: {overview_analysis.get('overall_shape_summary', '')[:100]}"
        )
        if overview_analysis.get("dimension_locations"):
            console.print(f"  Dimensions: {overview_analysis['dimension_locations'][:160]}")
    else:
        console.print("  [yellow]Overview analysis unavailable — proceeding with "
                      "per-view extraction only.[/yellow]")

    console.print("[2/4] Extracting drawing data with Claude Vision...")
    from pipeline.extractor import ExtractionError, extract_drawing_data

    usage: dict[str, int] = {}
    spec_lines = _spec_lines_from_args(args)
    if spec_lines:
        console.print(f"  [cyan]Specs-first:[/cyan] {len(spec_lines)} must-meet "
                      "specification(s) injected into extraction.")
    try:
        data = extract_drawing_data(
            prepared.base64,
            media_type=prepared.media_type,
            prep_warnings=prepared.warnings,
            cache_dir=_extract_cache_dir(args),
            usage_out=usage,
            requirements=spec_lines,
        )
        # Record tokens + cost for this API run into the output-root ledger.
        part = data.get("part_number") or data.get("part_name") or "part"
        ledger = record_run(Path(args.output), part, model, usage)
        console.print(
            f"  Tokens: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)} "
            f"cache_read={usage.get('cache_read_input_tokens', 0)} -> ledger {ledger}"
        )
        return data, overview_analysis
    except EnvironmentError as e:
        console.print(f"[red]Extraction failed (configuration):[/red] {e}")
    except ExtractionError as e:
        console.print(f"[red]Extraction failed:[/red] {e}")
    except Exception as e:
        # The extractor wraps an external API: auth, rate-limit, and network
        # failures surface as the SDK's own exception types. Present cleanly.
        console.print(f"[red]Extraction failed (API error):[/red] {type(e).__name__}: {e}")
    return None, overview_analysis


def _augment_holes(raw: dict, source_path: Path, page: int) -> dict:
    """Vector-augment hole positions in a raw extraction dict (exception-safe).

    Validates the dict into the schema model, lets the vector/consensus stage
    write exact positions into the hole callouts, and returns the updated dict.
    On any failure the ORIGINAL dict is returned untouched — this stage can
    only improve the extraction, never break it.
    """
    try:
        from pipeline.schema import DrawingData
        from pipeline.vector_extract import augment_hole_positions

        model = DrawingData.model_validate(raw)
        report = augment_hole_positions(model, source_path, page=page)
        if report is None:
            return raw
        n_exact = sum(1 for h in report.holes if h.outcome == "vector_exact")
        n_flag = sum(len(h.flags) for h in report.holes)
        console.print(
            f"  [2.2/4] Vector hole extraction: {n_exact}/{len(report.holes)} callout(s) "
            f"placed exactly from {source_path.suffix.lower().lstrip('.')} geometry "
            f"({n_flag} flag(s); scale from {report.scale_anchors} anchor(s))."
        )
        return model.model_dump(mode="json")
    except Exception as e:
        console.print(f"  [yellow]Vector hole extraction skipped:[/yellow] {type(e).__name__}: {e}")
        return raw


def _views_part_source_file(part, args) -> Path | None:
    """The vector source (PDF/DXF/DWG) for a views-folder part: an explicit
    --source-file wins; otherwise any vector file sitting in the part's folder."""
    explicit = getattr(args, "source_file", None)
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    try:
        first_view = next(iter(part.views.values()))
        folder = Path(first_view).parent
        for pattern in ("*.pdf", "*.dxf", "*.dwg", "*.PDF", "*.DXF", "*.DWG"):
            for cand in sorted(folder.glob(pattern)):
                return cand
    except (StopIteration, OSError):
        pass
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
    from pipeline.macro_generator import _safe_name

    safe = _safe_name(folder_name)
    part_dir = output_dir / safe
    part_dir.mkdir(parents=True, exist_ok=True)
    path = part_dir / f"{safe}_extraction.json"
    path.write_text(json.dumps(drawing_data, indent=2), encoding="utf-8")
    return path


def _extract_one_drawing(path: Path, page: int, cache_dir: Path | None,
                         requirements: list | None = None) -> dict:
    """Image-prep + Claude extraction for a single drawing file (used by batch)."""
    from utils.image_prep import prepare_image
    from pipeline.extractor import extract_drawing_data

    prepared = prepare_image(str(path), page=page, return_details=True)
    return extract_drawing_data(
        prepared.base64, media_type=prepared.media_type, prep_warnings=prepared.warnings,
        cache_dir=cache_dir, requirements=requirements,
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
    import os as _os

    sw_app = _connect_solidworks_optional(args)
    template = _os.getenv("SOLIDWORKS_TEMPLATE_PATH")
    batch_specs = _spec_lines_from_args(args)  # specs-first: into every extraction

    # Stage 1.5 for batch drawings: the drawing file IS the full sheet — analyze
    # it holistically before extraction and log the cost as its own stage.
    def _overview_for(p: Path):
        from pipeline.extractor import DEFAULT_MODEL
        from pipeline.overview_analysis import STAGE_TAG, analyze_overview_file
        from pipeline.usage_log import record_run

        usage_ov: dict = {}
        result = analyze_overview_file(p, page=args.page, part_number=p.stem,
                                       cache_dir=cache_dir, usage_out=usage_ov)
        if usage_ov.get("calls") or usage_ov.get("cache_hits"):
            record_run(output_dir, p.stem,
                       _os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL,
                       usage_ov, stage=STAGE_TAG)
        return result

    rows, csv_path = run_batch(
        directory, output_dir,
        extract_fn=lambda p: _extract_one_drawing(p, args.page, cache_dir,
                                                  requirements=batch_specs),
        overview_fn=_overview_for,
        sw_app=sw_app, template_path=template,
        resolve=not getattr(args, "no_resolve", False),
        strict_gate=getattr(args, "strict_gate", False),
        requirements_file=Path(args.requirements) if getattr(args, "requirements", None) else None,
        skip_overview_check=getattr(args, "skip_overview_check", False),
        skip_requirements_check=getattr(args, "skip_requirements_check", False),
    )
    if not rows:
        console.print(f"[yellow]No drawings or *_extraction.json files found in[/yellow] {directory}")
        return 0

    table = Table(title=f"Batch summary ({len(rows)} inputs)")
    for col in ("Part", "Status", "Readiness", "Macros", "Review", "Skipped", "Detail"):
        table.add_column(col, overflow="fold")
    status_color = {"READY": "green", "NOT READY": "yellow", "BLOCKED": "red", "ERROR": "yellow"}
    for r in rows:
        color = status_color.get(r.status, "white")
        table.add_row(
            r.part, f"[{color}]{r.status}[/{color}]", f"{r.macro_readiness:.0%}",
            str(r.n_macros), str(r.n_needs_review), str(r.n_skipped), r.detail[:60],
        )
    console.print(table)
    n_ready = sum(1 for r in rows if r.status == "READY")
    console.print(f"  Summary CSV: {csv_path}  ({n_ready}/{len(rows)} READY)")
    _export_to_downloads(args, output_dir)
    return 0 if n_ready == len(rows) else 8


def _views_for_extraction(part):
    """Orthographic views in canonical order, plus the full/overview drawing (as
    whole-part CONTEXT) last when present. The overview is never built as a plane."""
    from pipeline.view_ingest import OVERVIEW_VIEW

    ordered = list(part.ordered_views)  # front, top, side, ... (per VIEW_ORDER)
    if OVERVIEW_VIEW in part.views:
        ordered.append((OVERVIEW_VIEW, part.views[OVERVIEW_VIEW]))
    return ordered


def _extract_part_views(part, page: int, cache_dir, usage: dict,
                        requirements: list | None = None) -> dict:
    """Prepare each view image and run one combined multi-view extraction.

    ``requirements`` (operator must-meet spec lines) are injected into the
    extraction prompt — specs-first: the model actively looks for those
    features from the start rather than being checked only after the fact.

    The model owns interpretation: every uploaded sheet is read whole (view
    detection, origin, datums are derived by the vision model), with no human
    markup preprocessing."""
    from utils.image_prep import prepare_image
    from pipeline.extractor import extract_drawing_data_multiview

    views = []
    for view_type, path in _views_for_extraction(part):
        prepared = prepare_image(str(path), page=page, return_details=True)
        views.append((view_type, prepared.base64, prepared.media_type))

    data = extract_drawing_data_multiview(
        views, cache_dir=cache_dir, usage_out=usage, prep_warnings=part.warnings,
        requirements=requirements,
    )
    # Stamp the part number from the folder name when the model didn't read one.
    # An illegible title block can come back as quote/placeholder junk (e.g. '""')
    # rather than empty — treat anything with no alphanumerics as unread. The part
    # FOLDER name is the operator's authoritative part number.
    def _unread(v) -> bool:
        return not any(ch.isalnum() for ch in str(v or ""))

    if _unread(data.get("part_number")):
        data["part_number"] = part.name
    if _unread(data.get("revision")):
        data["revision"] = ""
    return data


def _run_views_folder(args) -> int:
    """Multi-view mode: each part is a folder of per-view images, built per plane."""
    import os as _os

    from rich.table import Table

    from pipeline.batch import process_drawing_data, write_batch_csv
    from pipeline.extractor import DEFAULT_MODEL
    from pipeline.usage_log import record_run
    from pipeline.view_ingest import VIEW_ORDER, discover_parts

    folder = Path(args.views_folder)
    if not folder.is_dir():
        console.print(f"[red]--views-folder path is not a directory:[/red] {folder}")
        return 2

    parts = discover_parts(folder)
    if not parts:
        console.print(f"[yellow]No part folders or images found in[/yellow] {folder}")
        return 0

    output_dir = Path(args.output)
    cache_dir = _extract_cache_dir(args)
    model = _os.getenv("EXTRACTION_MODEL") or DEFAULT_MODEL
    console.print(
        f"Multi-view build: {len(parts)} part(s); views processed in order "
        f"{', '.join(VIEW_ORDER)}."
    )
    # Connect to SolidWorks once so every READY part is built into a .sldprt.
    sw_app = _connect_solidworks_optional(args)
    template = _os.getenv("SOLIDWORKS_TEMPLATE_PATH")

    rows = []
    for part in parts:
        present = ", ".join(v for v, _ in _views_for_extraction(part)) or "none"
        # ── Specs-first: read the operator's must-meet notes BEFORE extraction
        # so the specifications shape the extraction prompt and Stage 2.5
        # resolution from the start (they are re-verified against the build in
        # the final gate). Also the input to the final requirements check.
        from pipeline.must_meet import find_spec_file
        from pipeline.requirements_check import parse_requirements

        notes_file = None
        if getattr(args, "requirements", None):
            notes_file = Path(args.requirements)
        else:
            try:
                part_folder = Path(next(iter(part.views.values()))).parent
                # must_meet_spec.txt wins; legacy notes.txt still works.
                notes_file = find_spec_file(part_folder, part.name)
            except StopIteration:
                pass
        spec_lines: list[str] = []
        if notes_file is not None and notes_file.is_file() \
                and not getattr(args, "skip_requirements_check", False):
            try:
                spec_lines = [r["text"] for r in parse_requirements(
                    notes_file.read_text(encoding="utf-8", errors="replace"))]
            except OSError as e:
                console.print(f"  [yellow]Could not read notes file:[/yellow] {e}")
        # ── Stage 1.5: Holistic Overview Analysis — the FULL drawing sheet is
        # analyzed relationally (cross-view correspondences, overall shape,
        # conflicts, symmetry) BEFORE per-view extraction, so Stage 2.5 gets the
        # whole-sheet context a cropped view can never provide. Reuses the
        # already-saved overview image; its cost is a separate ledger line.
        from pipeline.view_ingest import OVERVIEW_VIEW

        overview_img = part.views.get(OVERVIEW_VIEW)
        overview_analysis = None
        if overview_img is not None:
            from pipeline.overview_analysis import STAGE_TAG, analyze_overview_file

            print(f"[STAGE] Overview analysis {part.name}", flush=True)
            console.print(f"[1.5/2] {part.name}: holistic overview analysis (full sheet)...")
            usage_ov: dict = {}
            overview_analysis = analyze_overview_file(
                overview_img, page=args.page, part_number=part.name,
                cache_dir=cache_dir, usage_out=usage_ov,
            )
            if usage_ov.get("calls") or usage_ov.get("cache_hits"):
                record_run(output_dir, part.name, model, usage_ov, stage=STAGE_TAG)
            if overview_analysis is not None:
                n_conf = len(overview_analysis.get("cross_view_conflicts", []) or [])
                console.print(
                    f"  Overview: {len(overview_analysis.get('views_detected', []) or [])} "
                    f"view(s) detected, {n_conf} cross-view conflict(s); "
                    f"shape: {overview_analysis.get('overall_shape_summary', '')[:100]}"
                )
                if overview_analysis.get("dimension_locations"):
                    console.print(
                        f"  Dimensions: {overview_analysis['dimension_locations'][:160]}")
            else:
                console.print("  [yellow]Overview analysis unavailable — proceeding "
                              "with per-view extraction only.[/yellow]")
        print(f"[STAGE] Extracting {part.name}", flush=True)
        console.print(f"[1/2] {part.name}: extracting views ({present})...")
        if spec_lines:
            console.print(f"  [cyan]Specs-first:[/cyan] {len(spec_lines)} must-meet "
                          "specification(s) injected into extraction + resolution.")
        for w in part.warnings:
            console.print(f"  [yellow]warning:[/yellow] {w}")
        usage: dict = {}
        try:
            data = _extract_part_views(part, args.page, cache_dir, usage,
                                       requirements=spec_lines)
        except Exception as e:
            console.print(f"  [red]Extraction failed:[/red] {type(e).__name__}: {e}")
            from pipeline.batch import BatchRow

            rows.append(BatchRow(part.name, part.name, "ERROR", 0, 0, 0, 0, 0, 0, 0, 0,
                                 f"{type(e).__name__}: {e}"[:300]))
            continue
        record_run(output_dir, data.get("part_number") or part.name, model, usage)
        # Vector hole extraction: exact positions from an original PDF/DXF/DWG
        # delivered with the part folder (or --source-file), merged before resolve.
        vsrc = _views_part_source_file(part, args)
        if vsrc is not None:
            data = _augment_holes(data, vsrc, args.page)
        console.print("[2.5/2] resolving ambiguities + verifying + per-plane macros + .sldprt...")
        # Final-check input: the part's overview/full drawing (re-examined after
        # the build); the notes file was already discovered above (specs-first),
        # and the Stage 1.5 overview analysis feeds resolution as tier 2.
        try:
            rows.append(process_drawing_data(data, part.name, output_dir,
                                             sw_app=sw_app, template_path=template,
                                             resolve=not getattr(args, "no_resolve", False),
                                             strict_gate=getattr(args, "strict_gate", False),
                                             overview_image=overview_img,
                                             overview_analysis=overview_analysis,
                                             requirements_file=notes_file,
                                             skip_overview_check=getattr(args, "skip_overview_check", False),
                                             skip_requirements_check=getattr(args, "skip_requirements_check", False)))
        except Exception as e:  # one bad part must never sink the rest of the batch
            console.print(f"  [red]Processing failed:[/red] {type(e).__name__}: {e}")
            from pipeline.batch import BatchRow

            rows.append(BatchRow(part.name, part.name, "ERROR", 0, 0, 0, 0, 0, 0, 0, 0,
                                 f"{type(e).__name__}: {e}"[:300]))

    table = Table(title=f"Multi-view summary ({len(rows)} part(s))")
    for col in ("Part", "Status", "Readiness", "Macros", "Review", "Skipped", "Detail"):
        table.add_column(col, overflow="fold")
    status_color = {"READY": "green", "NOT READY": "yellow", "BLOCKED": "red", "ERROR": "yellow"}
    for r in rows:
        color = status_color.get(r.status, "white")
        table.add_row(r.part, f"[{color}]{r.status}[/{color}]", f"{r.macro_readiness:.0%}",
                      str(r.n_macros), str(r.n_needs_review), str(r.n_skipped), r.detail[:60])
    console.print(table)
    csv_path = write_batch_csv(rows, output_dir, name="multiview_summary.csv")
    n_ready = sum(1 for r in rows if r.status == "READY")
    console.print(f"  Summary CSV: {csv_path}  ({n_ready}/{len(rows)} READY)")
    print("[STAGE] Exporting", flush=True)
    _export_to_downloads(args, output_dir)
    print("[STAGE] Done", flush=True)
    return 0 if n_ready == len(rows) else 8


def main() -> int:
    _force_utf8_console()
    # Load .env from this script's directory so ANTHROPIC_API_KEY and
    # SOLIDWORKS_TEMPLATE_PATH resolve regardless of the current working dir.
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent / ".env")
    except Exception:
        pass
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
    src.add_argument(
        "--views-folder",
        help="Multi-view mode: a folder of parts, each a subfolder of SEPARATE per-view "
        "images (front/top/side/second_side/bottom). Each view is built on its own plane.",
    )
    parser.add_argument("--output", default="./output", help="Output directory.")
    parser.add_argument("--page", type=int, default=1, help="Page to use for multi-page PDFs (default 1).")
    parser.add_argument(
        "--source-file",
        help="Original vector drawing (PDF/DXF/DWG) for EXACT hole positions. "
        "Defaults to --drawing when that is already a PDF/DXF/DWG; for "
        "--views-folder runs, any PDF/DXF/DWG inside the part folder is used.",
    )
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
    parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="Skip the Stage 2.5 ambiguity resolver. By default every ambiguous/under-"
        "dimensioned value is resolved to a defensible number and annotated, so the "
        "build never blocks on ambiguity (chief-engineer mode).",
    )
    parser.add_argument(
        "--strict-gate",
        action="store_true",
        help="Restore the v2 hard gate: a BLOCKED verification stops the run with no "
        "macros. By default (resolver on) verification is advisory and the build "
        "proceeds with annotated assumptions.",
    )
    parser.add_argument(
        "--requirements",
        help="Human-authored must-meet notes file (one requirement per line). "
        "Each line is tracked, graded (met/partial/unmet/not_applicable) against "
        "the build, and reported; an unmet requirement gates READY. For "
        "--views-folder runs a notes.txt / requirements.txt / <part>_notes.txt "
        "inside the part folder is discovered automatically.",
    )
    parser.add_argument(
        "--skip-overview-check",
        action="store_true",
        help="Skip the final overview cross-check (the pass that re-examines the "
        "part's overview/full drawing and flags features missing from the build). "
        "The check auto-skips with a note when no overview image exists.",
    )
    parser.add_argument(
        "--skip-requirements-check",
        action="store_true",
        help="Skip grading the human-authored requirements notes against the build.",
    )
    parser.add_argument(
        "--no-sldprt",
        action="store_true",
        help="Do NOT build .sldprt files. By default every READY part is built into "
        "a real SolidWorks .sldprt (requires Windows + SolidWorks) alongside the "
        "text reports and VBA macros.",
    )
    parser.add_argument(
        "--no-export",
        action="store_true",
        help="Do NOT copy the outputs to ~/Downloads/SolidWorksModel_Parts. By "
        "default the final step gathers all part outputs there.",
    )
    args = parser.parse_args()

    console.print(Panel("2D -> 3D SolidWorks Pipeline", style="bold blue"))

    # --- Batch mode: process a whole directory and write a triage CSV ---
    if args.batch:
        return _run_batch(args)

    if args.views_folder:
        return _run_views_folder(args)

    # --- [1-2/4] Source the extraction ---
    overview_analysis = None
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
        # A saved overview_analysis.json next to the extraction is reused, so
        # --from-json re-runs keep the Stage 1.5 signal at zero API cost.
        saved_ov = json_path.parent / "overview_analysis.json"
        if saved_ov.is_file():
            try:
                overview_analysis = json.loads(saved_ov.read_text(encoding="utf-8"))
                console.print(f"  Reusing saved overview analysis: {saved_ov}")
            except json.JSONDecodeError:
                pass
    else:
        drawing_data, overview_analysis = _prepare_and_extract(args)
        if drawing_data is None:
            return 3

    if args.debug and not args.from_json:
        debug_path = Path("debug_extraction.json")
        debug_path.write_text(json.dumps(drawing_data, indent=2))
        console.print(f"  Debug: extraction saved to {debug_path}")

    # Vector hole extraction (additive): exact positions from the original
    # PDF/DXF/DWG geometry, merged into the extraction BEFORE Stage 2.5 so the
    # resolver never has to assume a position that the vector data pins exactly.
    vector_src = None
    if getattr(args, "source_file", None):
        vector_src = Path(args.source_file)
    elif args.drawing and Path(args.drawing).suffix.lower() in (".pdf", ".dxf", ".dwg"):
        vector_src = Path(args.drawing)
    if vector_src is not None and vector_src.is_file():
        drawing_data = _augment_holes(drawing_data, vector_src, args.page)

    # The raw extraction is kept verbatim for traceability (saved as _extraction.json);
    # Stage 2.5 produces the resolved copy that actually drives verification + build.
    raw_extraction = drawing_data
    drawing_data, resolution = _resolve_stage(args, raw_extraction, overview_analysis)

    # --- [3/4] Verification (advisory by default; hard gate only with --strict-gate) ---
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
    # Always persist the RAW extraction + the verification report — READY or
    # BLOCKED — so a paid extraction is never lost and a run is re-runnable via
    # --from-json with no API cost (failure E008).
    extraction_path = _save_extraction(output_dir, folder_name, raw_extraction)
    console.print(f"  Extraction saved to {extraction_path}")
    safe_base = extraction_path.name.removesuffix("_extraction.json")
    if overview_analysis:
        from pipeline.overview_analysis import save_overview_analysis

        ov_path = save_overview_analysis(extraction_path.parent, overview_analysis)
        console.print(f"  Overview analysis saved to {ov_path}")
    if resolution is not None:
        resolved_path = extraction_path.parent / f"{safe_base}_resolved_extraction.json"
        resolved_path.write_text(json.dumps(resolution.resolved_extraction, indent=2), encoding="utf-8")
        console.print(f"  Resolved extraction saved to {resolved_path}")
    if model is not None:
        report_path = extraction_path.parent / f"{safe_base}_verification_report.txt"
        report_path.write_text(verification_text, encoding="utf-8")
        console.print(f"  Report written to {report_path}")

    # Model can't be coerced at all → genuinely unbuildable, always stop.
    # Otherwise: --strict-gate restores the v2 hard block; default mode proceeds
    # with annotated assumptions ("an incomplete model is always the wrong outcome").
    if model is None or (not report.ok and args.strict_gate):
        console.print(
            Panel(
                "[red]BLOCKED — resolve the issues above, then re-run.[/red]\n"
                "Nothing was sent to SolidWorks and no macros were generated."
                + ("" if model is None else "\n(Re-run without --strict-gate to build with assumptions.)"),
                style="red",
            )
        )
        return 4
    if not report.ok:
        console.print(
            "[yellow]Verification raised issues; building anyway with annotated "
            "assumptions (chief-engineer mode). Use --strict-gate to block instead.[/yellow]"
        )

    if args.validate_only:
        console.print(Panel("[green]READY TO BUILD. Exiting (--validate-only).[/green]", style="green"))
        return 0

    # --- [4/4] Build ---
    if args.engine == "vba":
        console.print("[4/4] Generating SolidWorks VBA macro package...")
        from pipeline.macro_generator import generate_macro_package

        pkg = generate_macro_package(model, raw_extraction, verification_text, output_dir,
                                     resolution=resolution)
        n_macros = sum(1 for s in pkg.steps if s.macro_file.endswith(".vba"))
        lines = [
            f"[green]Macro package complete:[/green] {pkg.root}",
            f"  {n_macros} macros in {pkg.macros_dir}",
            f"  Build plan: {pkg.build_plan_json}",
        ]
        # Build the real .sldprt into the part folder (required output unless --no-sldprt).
        import os as _os

        sw_app = _connect_solidworks_optional(args)
        build_skipped: list = []
        build_caveats: list = []
        # Feature-flag A/B: BUILD_EXECUTOR_MODE=pywin32 routes the .sldprt COM build
        # through automation.build_executor (the experimental pywin32 path) instead
        # of the default build_sldprt_for_part. VBA macros are generated either way;
        # only the COM-build envelope changes. Default (vba) is fully unchanged.
        from automation.config import is_pywin32_mode

        if sw_app is not None and is_pywin32_mode():
            from automation.build_executor import run as _pywin32_run

            try:
                _report = _pywin32_run(
                    model, pkg.root, part_name=pkg.root.name,
                    template_path=_os.getenv("SOLIDWORKS_TEMPLATE_PATH"),
                    strict=False,
                )
                if _report.sldprt_path:
                    lines.append(f"  [green]Model built (pywin32):[/green] {_report.sldprt_path}")
                else:
                    lines.append("  [yellow].sldprt build (pywin32) produced no part;[/yellow] "
                                 "see *_pywin32_build_report.json")
                for _err in _report.errors:
                    build_caveats.append(_err.get("message", "pywin32 build error"))
            except Exception as e:
                lines.append(f"  [yellow].sldprt build (pywin32) failed:[/yellow] {type(e).__name__}: {e}")
        elif sw_app is not None:
            from pipeline.batch import build_sldprt_for_part

            try:
                sldprt = build_sldprt_for_part(
                    sw_app, model, pkg.root, pkg.root.name,
                    _os.getenv("SOLIDWORKS_TEMPLATE_PATH"),
                    skipped_out=build_skipped, caveats_out=build_caveats,
                )
                lines.append(f"  [green]Model built:[/green] {sldprt}")
            except Exception as e:
                lines.append(f"  [yellow].sldprt build failed:[/yellow] {type(e).__name__}: {e}")
        # Rewrite the engineering review with the build outcome folded in, so a
        # COM-skipped feature is always flagged in the human-facing report.
        try:
            from pipeline.engineering_review import build_review_items, write_review

            review_items = build_review_items(resolution=resolution, pkg=pkg,
                                              build_skipped=build_skipped,
                                              build_caveats=build_caveats)
            review_path = write_review(pkg.root, pkg.root.name, review_items,
                                       resolution=resolution)
            n_urgent = sum(1 for i in review_items if i["severity"] in ("CRITICAL", "HIGH"))
            lines.append(f"  Engineering review: {review_path} "
                         f"({n_urgent} item(s) need attention)")
        except Exception as e:
            lines.append(f"  [yellow]Engineering review failed:[/yellow] {type(e).__name__}: {e}")

        # ── Stage 10.5: Reconciliation Pass — the self-correcting loop (single-
        # drawing path). See pipeline/batch.py's process_drawing_data for the
        # --views-folder equivalent; both call the same pipeline.reconciliation
        # module so the guarantee is the same regardless of entry point.
        reconciliation_result = None
        if resolution is not None:
            try:
                from pipeline.reconciliation import reconcile_part

                build_plan_dict = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
                reconciliation_result = reconcile_part(
                    raw_extraction=raw_extraction, resolution=resolution, model=model,
                    dispositions=pkg.dispositions, build_plan=build_plan_dict,
                    verification_text=verification_text, part_dir=pkg.root,
                    part=pkg.root.name, requirements=_spec_lines_from_args(args) or None,
                    overview_analysis=overview_analysis,
                )
                rr_path = reconciliation_result.write(pkg.root, pkg.root.name)
                lines.append(
                    f"  Reconciliation: {reconciliation_result.confirmed_built}/"
                    f"{reconciliation_result.checklist_total} checklist items confirmed built "
                    f"after {reconciliation_result.loop_passes_used} pass(es) ({rr_path.name})"
                )
                if reconciliation_result.unresolved:
                    lines.append(
                        f"  [yellow]{len(reconciliation_result.unresolved)} reconciliation "
                        "item(s) still unresolved:[/yellow] "
                        + ", ".join(u.feature_id for u in reconciliation_result.unresolved)
                    )
            except Exception as e:
                lines.append(f"  [yellow]Reconciliation pass failed:[/yellow] {type(e).__name__}: {e}")

            # Human-assist escalation (same guarantee as the --views-folder path):
            # after the full automated ladder + reconciliation, still-unresolved
            # items become at most `cap` narrow questions. Non-blocking.
            try:
                from pipeline.human_assist import generate_assist_queue, overlay_dispositions

                aq = generate_assist_queue(
                    resolution=resolution, part=pkg.root.name, part_dir=pkg.root,
                    safe_name=pkg.root.name, model=model,
                    reconciliation_result=reconciliation_result,
                    lessons_path=output_dir / "lessons_learned.jsonl")
                if aq.pending():
                    overlay_dispositions(pkg.root, pkg.root.name, aq)
                    lines.append(f"  [yellow]{len(aq.pending())} question(s) need human input[/yellow] "
                                 f"({pkg.root.name}_assist_queue.json) — best-available values shipped")
            except Exception as e:
                lines.append(f"  [yellow]Human-assist escalation failed:[/yellow] {type(e).__name__}: {e}")

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
        style = "yellow" if (reconciliation_result is not None and reconciliation_result.unresolved) else "green"
        console.print(Panel("\n".join(lines), style=style))
        _export_to_downloads(args, output_dir)
        # Exit code mirrors the batch path's READY/NOT-fully-READY distinction:
        # unresolved reconciliation items mean the part is not fully READY, even
        # though the macros/model are still produced (never blocking, always visible).
        if reconciliation_result is not None and reconciliation_result.unresolved:
            return 8
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
    _export_to_downloads(args, output_dir)
    return 0 if vreport.get("ok") else 7


if __name__ == "__main__":
    sys.exit(main())
