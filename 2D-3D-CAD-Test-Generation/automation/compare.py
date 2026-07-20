"""A/B parity comparison between the baseline COM build and the pywin32 executor.

Runs the SAME resolved model through both build paths against two SEPARATE
SolidWorks documents and diffs the results — feature-tree feature counts and the
solid-body bounding box (the two cheap, robust geometry fingerprints) — so parity
can be demonstrated before the pywin32 path is promoted to default.

  * baseline  — ``pipeline.solidworks_builder.build_model`` called directly
                (the existing ``--engine com`` behaviour).
  * candidate — ``automation.build_executor.run`` (the pywin32-wrapped path).

Windows + SolidWorks required at call time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

BBOX_TOL_M = 5e-4  # 0.5 mm — geometry fingerprint tolerance


def _bbox_close(a: Optional[list], b: Optional[list], tol: float = BBOX_TOL_M) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


def compare_build(
    model: Union[Any, dict],
    output_dir: Union[str, Path],
    *,
    template_path: Optional[str] = None,
    part_name: Optional[str] = None,
) -> dict:
    """Build ``model`` both ways and return a structured parity report."""
    from pipeline.schema import DrawingData
    from pipeline.solidworks_builder import build_model, connect_to_solidworks
    from automation.build_executor import run as pywin32_run

    coerced = model if isinstance(model, DrawingData) else DrawingData.model_validate(model)
    name = part_name or coerced.part_name or "part"
    out = Path(output_dir)
    baseline_dir = out / "_compare_baseline"
    candidate_dir = out / "_compare_pywin32"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir.mkdir(parents=True, exist_ok=True)

    # -- baseline: direct build_model (existing COM path) ------------------ #
    baseline: dict = {"path": None, "feature_tree_count": None, "bbox_m": None, "error": None}
    try:
        sw_app = connect_to_solidworks()
        feat_results: list[dict] = []
        path, sw_doc = build_model(
            sw_app, coerced, output_dir=baseline_dir, template_path=template_path,
            strict=False, feature_results=feat_results,
        )
        baseline["path"] = str(path)
        try:
            feat = sw_doc.FirstFeature
            n = 0
            while feat is not None:
                n += 1
                feat = feat.GetNextFeature
            baseline["feature_tree_count"] = n
        except Exception:
            pass
        try:
            bodies = sw_doc.GetBodies2(0, False)
            body = bodies[0] if isinstance(bodies, (list, tuple)) else bodies
            baseline["bbox_m"] = list(body.GetBodyBox())
        except Exception:
            pass
        baseline["feature_pass_count"] = sum(1 for r in feat_results if r.get("status") == "PASS")
    except Exception as e:
        baseline["error"] = f"{type(e).__name__}: {e}"

    # -- candidate: pywin32 executor --------------------------------------- #
    candidate: dict = {"path": None, "feature_tree_count": None, "bbox_m": None, "error": None}
    try:
        report = pywin32_run(coerced, candidate_dir, part_name=name,
                             template_path=template_path, strict=False)
        candidate["path"] = report.sldprt_path
        candidate["feature_tree_count"] = report.feature_tree_count
        candidate["bbox_m"] = report.bbox_m
        candidate["feature_pass_count"] = sum(
            1 for o in report.operations if o.status == "PASS")
        candidate["errors"] = report.errors
    except Exception as e:
        candidate["error"] = f"{type(e).__name__}: {e}"

    tree_match = (baseline["feature_tree_count"] is not None
                  and baseline["feature_tree_count"] == candidate["feature_tree_count"])
    bbox_match = _bbox_close(baseline["bbox_m"], candidate["bbox_m"])
    return {
        "part": name,
        "baseline": baseline,
        "candidate": candidate,
        "diff": {
            "feature_tree_count_match": tree_match,
            "bbox_match": bbox_match,
            "bbox_tolerance_m": BBOX_TOL_M,
            "parity": bool(tree_match and bbox_match
                           and not baseline["error"] and not candidate["error"]),
        },
    }
