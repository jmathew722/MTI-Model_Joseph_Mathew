"""Phase D — bounded construction experiment harness.

When a feature class fails Phase A verification repeatedly, do NOT blindly retry
the same construction a third time. Instead build a SCRATCH part (base + only the
failing feature) with each candidate construction method, verify every result
with Phase A (pipeline.feature_verify), and record the winner. The winner is
written to ``pipeline/METHODS.md`` (human evidence) and can be promoted into
``methods.json`` (machine dispatch, read by methods_config) — converting a
debugging session into permanent pipeline knowledge.

Headless candidates (CadQuery) run anywhere; the SolidWorks candidates require a
live COM app passed in as ``sw_app`` and are simply skipped when absent, so this
module is import- and test-safe without SolidWorks.

Public entry: :func:`run_hole_experiment`, :func:`run_slot_experiment`,
:func:`ExperimentResult`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from utils.logger import get_logger

log = get_logger()


@dataclass
class MethodTrial:
    method: str
    backend: str                # cadquery | solidworks
    built: bool
    verified_ok: bool
    detail: str = ""
    measurements: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "backend": self.backend, "built": self.built,
                "verified_ok": self.verified_ok, "detail": self.detail,
                "measurements": self.measurements}


@dataclass
class ExperimentResult:
    feature_class: str
    trials: list[MethodTrial] = field(default_factory=list)
    winner: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"feature_class": self.feature_class, "winner": self.winner,
                "trials": [t.as_dict() for t in self.trials]}

    def decide(self) -> None:
        ok = [t for t in self.trials if t.built and t.verified_ok]
        self.winner = ok[0].method if ok else None


# --------------------------------------------------------------------------- #
# CadQuery scratch builders (headless, always available when cadquery is)
# --------------------------------------------------------------------------- #
def _cq_base(length=4.0, width=2.0, thick=0.25):
    import cadquery as cq
    k = 25.4
    return (cq.Workplane("XY").center(length / 2 * k, width / 2 * k)
            .box(length * k, width * k, thick * k, centered=(True, True, False))), k


def _cq_hole_sketch_circle(cx, cy, dia):
    wp, k = _cq_base()
    return (wp.faces(">Z").workplane(origin=(0, 0, 0))
            .pushPoints([(cx * k, cy * k)]).hole(dia * k))


def _cq_slot_slot2d(cx, cy, length, width):
    wp, k = _cq_base()
    return (wp.faces(">Z").workplane(origin=(0, 0, 0)).center(cx * k, cy * k)
            .slot2D(length * k, width * k, 0).cutThruAll())


def _cq_slot_capsule(cx, cy, length, width):
    """Alternative: two circles + connecting rect (the historical hand-rolled
    obround) — kept as an experiment comparand, not a recommended method."""
    import cadquery as cq
    wp, k = _cq_base()
    r = width / 2.0
    half = (length - width) / 2.0
    face = wp.faces(">Z").workplane(origin=(0, 0, 0)).center(cx * k, cy * k)
    face = (face.pushPoints([(-half * k, 0), (half * k, 0)]).circle(r * k)
            .cutThruAll())
    face = (face.faces(">Z").workplane(origin=(0, 0, 0)).center(cx * k, cy * k)
            .rect(max(length - width, 1e-3) * k, width * k).cutThruAll())
    return face


def _verify_cq(solid, plan: dict, tmp: Path, label: str) -> MethodTrial:
    from pipeline.feature_verify import verify_features
    import cadquery as cq

    stl = tmp / f"{label}.stl"
    try:
        cq.exporters.export(solid, str(stl))
    except Exception as e:
        return MethodTrial(label, "cadquery", built=False, verified_ok=False,
                           detail=f"export failed: {e}")
    rep = verify_features(stl, plan, tmp, write=False)
    non_base = [f for f in rep.get("features", []) if f.get("kind") != "base"]
    ok = bool(non_base) and all(f["classification"] == "OK" for f in non_base) and not rep.get("extras")
    return MethodTrial(label, "cadquery", built=True, verified_ok=ok,
                       detail=rep.get("summary", {}).get("by_classification", {}),
                       measurements={"mismatches": rep.get("mismatches", [])})


# --------------------------------------------------------------------------- #
# Experiments
# --------------------------------------------------------------------------- #
def _hole_plan(cx, cy, dia):
    return {"units": "inch", "unit_factor_to_meters": 0.0254, "steps": [
        {"seq": 1, "type": "extrude_boss", "feature_id": "F001",
         "dimensions_drawing_units": {"length": 4.0, "width": 2.0, "depth": 0.25},
         "positions_xy": [[0.0, 0.0]]},
        {"seq": 2, "type": "hole", "feature_id": "F002",
         "dimensions_drawing_units": {"diameter": dia}, "positions_xy": [[cx, cy]],
         "depth_type": "through_all"}]}


def _slot_plan(cx, cy, length, width):
    return {"units": "inch", "unit_factor_to_meters": 0.0254, "steps": [
        {"seq": 1, "type": "extrude_boss", "feature_id": "F001",
         "dimensions_drawing_units": {"length": 4.0, "width": 2.0, "depth": 0.25},
         "positions_xy": [[0.0, 0.0]]},
        {"seq": 2, "type": "extrude_cut", "feature_id": "F002", "profile": "slot",
         "dimensions_drawing_units": {"length": length, "width": width},
         "positions_xy": [[cx - length / 2.0, cy - width / 2.0]],
         "depth_type": "through_all"}]}


def run_hole_experiment(tmp_dir: Path, *, cx=2.0, cy=1.0, dia=0.5,
                        sw_app=None) -> ExperimentResult:
    """Compare hole construction methods on a scratch plate."""
    tmp = Path(tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    res = ExperimentResult("hole")
    plan = _hole_plan(cx, cy, dia)
    try:
        res.trials.append(_verify_cq(_cq_hole_sketch_circle(cx, cy, dia), plan, tmp,
                                     "sketch_circle_cut"))
    except Exception as e:
        res.trials.append(MethodTrial("sketch_circle_cut", "cadquery", False, False, str(e)))
    # SolidWorks candidates (HoleWizard5 vs sketch cut) run only with a live app.
    if sw_app is not None:
        res.trials.extend(_sw_hole_trials(sw_app, plan, cx, cy, dia))
    res.decide()
    return res


def run_slot_experiment(tmp_dir: Path, *, cx=2.0, cy=1.0, length=2.0, width=0.5,
                        sw_app=None) -> ExperimentResult:
    tmp = Path(tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    res = ExperimentResult("slot")
    plan = _slot_plan(cx, cy, length, width)
    for label, builder in (("slot2d", _cq_slot_slot2d), ("capsule_profile", _cq_slot_capsule)):
        try:
            res.trials.append(_verify_cq(builder(cx, cy, length, width), plan, tmp, label))
        except Exception as e:
            res.trials.append(MethodTrial(label, "cadquery", False, False, str(e)))
    if sw_app is not None:
        res.trials.extend(_sw_slot_trials(sw_app, plan, cx, cy, length, width))
    res.decide()
    return res


def _sw_hole_trials(sw_app, plan, cx, cy, dia) -> list[MethodTrial]:
    """Live-SolidWorks hole trials — populated during a Phase-D live session."""
    # Intentionally minimal: the live session drives solidworks_builder directly
    # (build_sldprt harness) and records results here. Absent SW, never reached.
    return []


def _sw_slot_trials(sw_app, plan, cx, cy, length, width) -> list[MethodTrial]:
    return []


def record_to_methods_json(result: ExperimentResult, config_dir: Optional[Path] = None) -> Optional[Path]:
    """Promote a winning method into methods.json so the pipeline dispatches it.
    No-op (returns None) when there is no winner — never guesses."""
    if not result.winner:
        return None
    path = (Path(config_dir) if config_dir else Path(__file__).parent) / "methods.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("methods", {})[result.feature_class] = result.winner
    data.setdefault("evidence", {})[result.feature_class] = result.as_dict()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path
