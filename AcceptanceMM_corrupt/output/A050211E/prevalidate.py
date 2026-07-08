#!/usr/bin/env python
"""Auto-generated per-run pre-validation (single source of truth =
A050211E_build_plan.json in this folder). Re-run any time:

    python prevalidate.py

Exit 0 = all checks pass; exit 1 = a check failed (see prevalidation_report.json).
Requires: pip install cadquery
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, 'C:\\Users\\joeka\\MTI-Model_New\\2D-3D-CAD-Test-Generation')
from pipeline.cq_prevalidate import run_prevalidation

HERE = Path(__file__).resolve().parent
report = run_prevalidation(HERE / 'A050211E_build_plan.json', HERE / "must_meet_constraints.json", HERE)
print(json.dumps(report, indent=2))
sys.exit(0 if report.get("ok") else 1)
