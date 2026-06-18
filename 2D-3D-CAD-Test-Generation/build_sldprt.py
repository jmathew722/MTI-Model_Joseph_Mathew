"""Batch COM builder: turn each part's saved extraction JSON into a real .sldprt.

For every ``<part>/<part>_extraction.json`` under the given output root, re-run
verification and (if READY) build the 3D model directly in SolidWorks 2024 over
COM, saving ``<part>.sldprt`` and a ``<part>_model_check.txt`` INTO that part's
own folder. One SolidWorks session is shared across all parts.

Usage:
    python build_sldprt.py <output_root>

BLOCKED parts are skipped (their verification gate failed) and reported; nothing
is guessed. Non-strict build mode is used so a single fragile feature is skipped
(and documented) rather than aborting an otherwise-good part.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Force UTF-8 console so '->'/'—' in reports never crash on a cp1252 terminal.
for _stream in (sys.stdout, sys.stderr):
    _reconfig = getattr(_stream, "reconfigure", None)
    if _reconfig is not None:
        try:
            _reconfig(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

# Load .env from this script's directory so SOLIDWORKS_TEMPLATE_PATH is available.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

from pipeline.model_validator import validate_model
from pipeline.solidworks_builder import (
    SolidWorksError,
    build_model,
    connect_to_solidworks,
    save_model,
)
from pipeline.validator import run_verification


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python build_sldprt.py <output_root>")
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 2

    extraction_files = sorted(root.glob("*/*_extraction.json"))
    if not extraction_files:
        print(f"No *_extraction.json found under {root}")
        return 2

    import os
    template = os.getenv("SOLIDWORKS_TEMPLATE_PATH") or None
    if not template or not Path(template).exists():
        for cand in (
            r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\Part.PRTDOT",
            r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\Part.PRTDOT",
        ):
            if Path(cand).exists():
                template = cand
                break
    print(f"Using part template: {template}")

    print(f"Connecting to SolidWorks for {len(extraction_files)} candidate part(s)...")
    sw_app = connect_to_solidworks()

    built, blocked, errored = [], [], []
    for ef in extraction_files:
        part_dir = ef.parent
        name = part_dir.name
        try:
            data = json.loads(ef.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[{name}] SKIP — cannot read extraction: {e}")
            errored.append((name, f"read: {e}"))
            continue

        model, report = run_verification(data)
        if model is None or not report.ok:
            reason = "; ".join(report.errors)[:200] if report.errors else "verification failed"
            print(f"[{name}] BLOCKED — {reason}")
            blocked.append((name, reason))
            continue

        # Name the saved file after the part folder, regardless of title-block reads.
        if not model.part_name:
            model.part_name = name

        print(f"[{name}] building .sldprt ...")
        skipped: list[tuple[str, str, str]] = []
        try:
            # Build non-strict so a fragile feature is skipped (documented) not fatal.
            _, sw_doc = build_model(
                sw_app, model, output_dir=part_dir, template_path=template,
                strict=False, skipped_out=skipped,
            )
            # Re-save with the deterministic part-folder name in the part's folder.
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

            print(f"[{name}] OK -> {sldprt}  (skipped {len(skipped)} feature(s))")
            built.append((name, sldprt, len(skipped), vreport.get("ok")))
        except SolidWorksError as e:
            print(f"[{name}] BUILD ERROR — {e}")
            errored.append((name, str(e)[:200]))
            try:
                sw_app.CloseAllDocuments(True)
            except Exception:
                pass

    print("\n" + "=" * 60)
    print(f"Built {len(built)} .sldprt | Blocked {len(blocked)} | Errors {len(errored)}")
    for name, path, nskip, ok in built:
        flag = "OK" if ok else "issues"
        print(f"  BUILT   {name}: {path} ({flag}, {nskip} skipped)")
    for name, reason in blocked:
        print(f"  BLOCKED {name}: {reason}")
    for name, reason in errored:
        print(f"  ERROR   {name}: {reason}")
    return 0 if not errored else 8


if __name__ == "__main__":
    sys.exit(main())
