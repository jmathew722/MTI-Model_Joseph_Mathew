"""Golden-file snapshot of the generated macro package.

Generation is deterministic (no timestamps or absolute paths are baked into the
VBA — those are resolved at run time), so the full text of every generated file
can be snapshotted. This catches any unintended change to macro output: a drift
here is a red flag that a generator edit changed what ships to SolidWorks.

Regenerate the golden after an INTENTIONAL change:

    UPDATE_GOLDEN=1 python -m pytest tests/test_golden_macros.py -q

Review the resulting diff in the PR exactly as you would review code.
"""
import os
from pathlib import Path

import pytest

from pipeline.macro_generator import generate_macro_package
from pipeline.validator import format_verification_report, run_verification

GOLDEN_DIR = Path(__file__).parent / "golden" / "bracket" / "macros"


def _golden_drawing() -> dict:
    """A fixed, representative part: base plate + patterned holes + counterbore +
    fillet + a prohibited shell — exercises most generator paths. Frozen on
    purpose; do not edit without regenerating the golden files."""
    return {
        "part_number": "GOLDEN-1",
        "revision": "A",
        "units": "inch",
        "confidence": 0.91,
        "dimensions": [
            {"id": "D001", "type": "linear", "value": 4.0, "unit": "inch", "applies_to": "length"},
            {"id": "D002", "type": "linear", "value": 2.0, "unit": "inch", "applies_to": "width"},
            {"id": "D003", "type": "depth", "value": 0.5, "unit": "inch", "applies_to": "height"},
            {"id": "D004", "type": "diameter", "value": 0.25, "unit": "inch",
             "applies_to": "hole_diameter", "feature_ref": "F002"},
            {"id": "D005", "type": "radial", "value": 0.125, "unit": "inch",
             "applies_to": "fillet_radius", "feature_ref": "F003"},
        ],
        "hole_callouts": [
            {"id": "H001", "type": "thru", "diameter": 0.25, "qty": 4,
             "pattern": "linear", "pattern_spacing": 1.0, "feature_ref": "F002"},
        ],
        "features": [
            {"id": "F001", "type": "extrude_boss", "description": "Base plate",
             "related_dimensions": ["D001", "D002"], "depth_dimension_id": "D003", "sketch_plane": "Top"},
            {"id": "F002", "type": "hole", "description": "Mounting holes", "related_dimensions": ["D004"]},
            {"id": "F003", "type": "fillet", "description": "Corner fillet", "related_dimensions": ["D005"]},
            {"id": "F004", "type": "shell", "description": "Shell body", "related_dimensions": ["D003"]},
        ],
        "build_order": ["F001", "F002", "F003", "F004"],
    }


def _generate(tmp_path) -> Path:
    data = _golden_drawing()
    model, report = run_verification(data)
    assert report.ok, str(report)
    pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
    return pkg.macros_dir


def test_generated_macros_match_golden(tmp_path):
    macros = _generate(tmp_path)

    if os.getenv("UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for old in GOLDEN_DIR.glob("*"):
            old.unlink()
        for f in sorted(macros.glob("*")):
            (GOLDEN_DIR / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        pytest.skip(f"Golden files regenerated in {GOLDEN_DIR}")

    assert GOLDEN_DIR.is_dir(), "No golden files; run with UPDATE_GOLDEN=1 to create them."
    golden_names = sorted(p.name for p in GOLDEN_DIR.glob("*"))
    generated_names = sorted(p.name for p in macros.glob("*"))
    assert generated_names == golden_names, (
        f"Macro file set changed (symmetric diff: {set(generated_names) ^ set(golden_names)})"
    )
    for name in golden_names:
        expected = (GOLDEN_DIR / name).read_text(encoding="utf-8")
        actual = (macros / name).read_text(encoding="utf-8")
        assert actual == expected, f"{name} drifted from golden — review intended? UPDATE_GOLDEN=1 to accept."
