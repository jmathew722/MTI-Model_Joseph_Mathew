"""Tab-3 visual summary view-model builder.

A PURE presentation layer over artifacts the pipeline already wrote — it runs no
pipeline stage, does no geometry, and adds no new computation. Given one part's
``output/`` directory it assembles the single JSON view-model the webapp's two
summary tables (Extracted Features & Dimensions; Build Plan) and the part-header
strip consume.

All formatting rules live here, in ONE place, so the browser never sees raw
JSON numbers or meters:

* numbers are drawing-style — trailing zeros trimmed, leading zero dropped for
  magnitudes below 1 (``.105`` not ``0.1050``); meters never surface;
* diameters carry the ``⌀`` convention, positions are ``(x, y)`` pairs;
* an absent value is the em dash ``—``, never ``null``/``""`` leaking to the UI.

Degrade gracefully: a missing artifact (e.g. no ``*_feature_verification.json``
yet — that stage's JSON is not always produced) renders its columns ``pending``
rather than raising. The artifact filename PREFIX is discovered from disk (a part
folder ``A001581E`` holds ``158-C_*`` files), never derived from the folder name.

The single entry point is :func:`build_summary`; it is import-safe with no
FastAPI/webapp dependency so it is unit-tested directly against real golden-part
output folders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# ── Basis vocabulary → the human word shown in the "Basis" column ────────────
# Maps a resolver ``assumption_basis`` / disposition ``derivation_source`` to
# the one-word label the design plan specifies (Extracted / Derived-chain /
# Derived-profile / TYP / Committed / …).
_BASIS_LABELS = {
    "": "Extracted",
    "explicit_callout": "Extracted",
    "explicit": "Extracted",
    "as_read": "Extracted",
    "direct_reading": "Extracted",
    "direct": "Extracted",
    "read": "Extracted",
    "measured": "Extracted",
    "extracted_verbatim": "Extracted",
    "arithmetic_chain": "Derived-chain",
    "chain": "Derived-chain",
    "profile_delta": "Derived-profile",
    "typ": "TYP",
    "typ_sibling": "TYP",
    "committed_conservative": "Committed",
    "committed": "Committed",
    "conservative_geometry": "Committed",
    "sibling_hole": "Sibling",
    "sibling": "Sibling",
    "standard_thread_size": "Standard",
    "standard_size": "Standard",
    "spec_driven": "Spec",
    "human_provided": "Answered",
    "last_resort_default": "Default",
    "default": "Default",
}

# Disposition state → (label, badge kind). Badge kind is one of
# ok/warn/err/neutral, mapped to .badge-c classes on the frontend.
_STATE_STATUS = {
    "BUILT": ("Built", "ok"),
    "BUILT_WITH_DERIVED_VALUE": ("Derived", "warn"),
    "EXCLUDED_INCOMPLETE": ("Excluded", "err"),
    "PHANTOM_RECLASSIFIED": ("Phantom", "neutral"),
    "NEEDS_HUMAN_INPUT": ("Needs input", "warn"),
}

# construction_method / step type → operation label (Table 2 "Operation").
_OPERATION_LABELS = {
    "sketch_rect_cut": "Sketch rect cut",
    "sketch_circle_cut": "Sketch circle cut",
    "slot2d": "Sketch slot",
    "create_sketch_slot": "Sketch slot",
    "capsule_profile": "Capsule slot",
    "hole_wizard5": "HoleWizard",
}
_TYPE_LABELS = {
    "extrude_boss": "Extrude boss",
    "extrude_cut": "Extrude cut",
    "hole": "Drill hole",
    "thread": "Tapped hole",
    "fillet": "Fillet",
    "chamfer": "Chamfer",
    "pattern": "Pattern",
    "linear_pattern": "Linear pattern",
    "circular_pattern": "Circular pattern",
    "mirror": "Mirror",
    "revolve": "Revolve",
    "slot_rect_cut": "Slot rectangle cut",
    "slot_corner_fillet": "Corner fillet",
    "reference_axis": "Reference axis",
    "reference_geometry": "Reference geometry",
}

# Build-plan step types that are scaffolding, not a drawing feature — kept out
# of the Build-Plan table (setup, export, whole-run driver, final verify, and
# the datum skeleton, which is prep, not one of the design plan's build stages).
_SCAFFOLD_TYPES = {"setup", "export_stl", "final_verify", "run_all", "export",
                   "reference_geometry"}
_SCAFFOLD_SEQS = {0, 999, 1000, 1001}
_SCAFFOLD_MACROS = {"00_setup.vba", "01a_reference_geometry.vba", "RUN_ALL.vba",
                    "ZZ_final_verify.vba", "ZZZ_export_stl.vba"}

# Stage fallback for a build step whose feature has no disposition of its own
# (slot rectangle/fillet halves, the circular-pattern trio, etc.).
_STAGE_BY_TYPE = {
    "extrude_boss": "Base Solid",
    "revolve": "Base Solid",
    "extrude_cut": "Profile Subtractions",
    "slot_rect_cut": "Profile Subtractions",
    "hole": "Holes",
    "thread": "Holes",
    "pattern": "Patterns",
    "linear_pattern": "Patterns",
    "circular_pattern": "Patterns",
    "reference_axis": "Patterns",
    "mirror": "Patterns",
    "fillet": "Edge Treatments",
    "chamfer": "Edge Treatments",
    "slot_corner_fillet": "Edge Treatments",
}

# The geometric feature types that count toward the header's feature tally.
_GEOMETRIC_TYPES = {
    "extrude_boss", "extrude_cut", "hole", "thread", "fillet", "chamfer",
    "pattern", "linear_pattern", "circular_pattern", "mirror", "revolve",
}

_EM_DASH = "—"
_DIAM = "⌀"


# ── number / string formatting (the one place meters/JSON never escape) ──────
def fmt_num(value: Any) -> Optional[str]:
    """Drawing-style number: trailing zeros trimmed, leading zero dropped for
    magnitudes < 1. Returns None for a non-number so callers can dash it."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    s = f"{f:.4f}".rstrip("0").rstrip(".")
    if s in ("", "-", "-0"):
        return "0"
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def _dash(s: Optional[str]) -> str:
    return s if s else _EM_DASH


def _diam(value: Any) -> Optional[str]:
    n = fmt_num(value)
    return f"{_DIAM}{n}" if n is not None else None


def _pos_str(x: Any, y: Any) -> Optional[str]:
    fx, fy = fmt_num(x), fmt_num(y)
    if fx is None and fy is None:
        return None
    return f"({_dash(fx)}, {_dash(fy)})"


def _basis_label(basis: str) -> str:
    return _BASIS_LABELS.get((basis or "").lower(), (basis or "").replace("_", " ").title() or "Extracted")


# ── disk loading ─────────────────────────────────────────────────────────────
def _first(out: Path, patterns: list[str], exclude: Optional[list[str]] = None) -> Optional[Path]:
    """First file under *out* matching any pattern (rglob), skipping any whose
    name contains an *exclude* token. Mirrors the webapp's own _first helper."""
    exclude = exclude or []
    if not out or not out.exists():
        return None
    for pat in patterns:
        for p in sorted(out.rglob(pat)):
            if any(ex in p.name for ex in exclude):
                continue
            if p.is_file():
                return p
    return None


def _load(path: Optional[Path]) -> Any:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── envelope, from individual dimensions (no single envelope object exists) ──
def _envelope(dims: list[dict], units: str, base_values: Optional[dict] = None) -> dict:
    """Overall W × H × T pulled from dimensions by applies_to. The in-plane
    second axis is 'width' on some parts and 'height' on others — accept both.
    Falls back to the base-solid step's own values_used when the dimensions
    don't carry envelope-labelled entries."""
    def pick(*keys) -> Optional[float]:
        for d in dims:
            a = str(d.get("applies_to") or "").lower()
            if a in keys:
                v = d.get("resolved_value")
                if v is None:
                    v = d.get("value")
                if v is not None:
                    return v
        return None

    length = pick("length", "overall_length")
    width = pick("width", "overall_width", "height", "overall_height")
    thick = pick("thickness", "depth")
    if thick is None:
        thick = pick("height") if width is not None else None
    bv = base_values or {}
    if length is None:
        length = bv.get("length")
    if width is None:
        width = bv.get("width", bv.get("height"))
    if thick is None:
        thick = bv.get("thickness", bv.get("depth"))
    parts = [fmt_num(length), fmt_num(width), fmt_num(thick)]
    shown = [p for p in parts if p is not None]
    display = (" × ".join(shown) + (f" {units}" if units and shown else "")) if shown else _EM_DASH
    return {"length": length, "width": width, "thickness": thick, "units": units, "display": display}


# ── feature size / position, per feature type ────────────────────────────────
def _dims_by_id(dims: list[dict]) -> dict[str, dict]:
    return {str(d.get("id")): d for d in dims if d.get("id")}


def _dim_value(d: dict) -> Any:
    v = d.get("resolved_value")
    return d.get("value") if v is None else v


def _feature_size(feat: dict, dims_by_id: dict, hole: Optional[dict],
                  slot: Optional[dict]) -> Optional[str]:
    ftype = str(feat.get("type") or "").lower()
    if hole is not None:
        d = _diam(hole.get("diameter"))
        depth = hole.get("depth") or 0
        if d and not hole.get("thru") and depth:
            return f"{d} × {fmt_num(depth)} deep"
        return d
    if slot is not None:
        w, dp = fmt_num(slot.get("width")), fmt_num(slot.get("depth"))
        r = fmt_num(slot.get("corner_radius"))
        base = f"{_dash(w)} × {_dash(dp)}"
        return f"{base} (R{r})" if r else base
    related = [dims_by_id[i] for i in (feat.get("related_dimensions") or []) if i in dims_by_id]
    if ftype in ("fillet", "chamfer"):
        for d in related:
            n = fmt_num(_dim_value(d))
            if n is not None:
                return (f"R{n}" if ftype == "fillet" else f"{n} × 45°")
        return None
    # generic: length × width/height × depth from related dims
    by_axis: dict[str, str] = {}
    for d in related:
        a = str(d.get("applies_to") or "").lower()
        n = fmt_num(_dim_value(d))
        if n is None:
            continue
        if a in ("length", "overall_length") and "l" not in by_axis:
            by_axis["l"] = n
        elif a in ("width", "height", "overall_width", "overall_height") and "w" not in by_axis:
            by_axis["w"] = n
        elif a in ("thickness", "depth", "diameter") and "t" not in by_axis:
            by_axis["t"] = n
    ordered = [by_axis[k] for k in ("l", "w", "t") if k in by_axis]
    if ordered:
        return " × ".join(ordered)
    return None


def _feature_position(feat: dict, hole: Optional[dict]) -> Optional[str]:
    if feat.get("position_known") or feat.get("offset_x") or feat.get("offset_y"):
        return _pos_str(feat.get("offset_x"), feat.get("offset_y"))
    if hole is not None:
        insts = hole.get("instance_positions") or []
        if len(insts) == 1:
            return _pos_str(insts[0][0], insts[0][1])
        if len(insts) > 1:
            return f"{len(insts)} positions"
        return _pos_str(hole.get("x_position"), hole.get("y_position"))
    return None


# ── main builder ─────────────────────────────────────────────────────────────
def build_summary(output_dir: Path | str) -> dict:
    """Assemble the Tab-3 view-model for one part's output directory. Never
    raises on missing/partial artifacts — those render as pending/empty."""
    out = Path(output_dir)
    resolved_p = _first(out, ["*_resolved_extraction.json", "*resolved*.json"])
    extraction_p = _first(out, ["*_extraction.json", "*extraction*.json"], exclude=["resolved"])
    plan_p = _first(out, ["*_build_plan.json", "*build_plan*.json"])
    disp_p = _first(out, ["*_build_dispositions.json", "*build_dispositions*.json"])
    fverify_p = _first(out, ["*_feature_verification.json", "*feature_verification*.json"])
    assist_p = _first(out, ["*_assist_queue.json", "*assist_queue*.json"])
    recon_p = _first(out, ["*_reconciliation_report.json", "*reconciliation*.json"])

    resolved = _load(resolved_p) or _load(extraction_p) or {}
    plan = _load(plan_p) or {}
    # Dispositions: prefer the standalone list; fall back to the copy embedded
    # in build_plan.json. Both are the same bare list of disposition dicts.
    dispositions = _load(disp_p)
    if not isinstance(dispositions, list):
        dispositions = plan.get("dispositions") if isinstance(plan, dict) else None
    if not isinstance(dispositions, list):
        dispositions = []
    fverify = _load(fverify_p) or {}
    assist = _load(assist_p) or {}
    recon = _load(recon_p) or {}

    ran = bool(resolved) or bool(plan) or bool(dispositions)

    units = str(resolved.get("units") or plan.get("units") or "")
    dims = resolved.get("dimensions") or []
    features = resolved.get("features") or []
    holes = resolved.get("hole_callouts") or []
    slots = resolved.get("slot_cuts") or []
    dims_by_id = _dims_by_id(dims)
    hole_by_feat = {str(h.get("feature_ref")): h for h in holes if h.get("feature_ref")}
    slot_by_feat = {str(s.get("id")): s for s in slots if s.get("id")}
    disp_by_feat = {str(d.get("feature_id")): d for d in dispositions if d.get("feature_id")}

    # feature verification, keyed by id, when the artifact exists
    fverify_by_feat: dict[str, dict] = {}
    for fv in (fverify.get("features") or []):
        fid = str(fv.get("feature_id") or fv.get("id") or "")
        if fid:
            fverify_by_feat[fid] = fv
    verification_available = bool(fverify_by_feat)

    # pending assist questions, keyed by feature id
    pending_by_feat: dict[str, dict] = {}
    for q in (assist.get("questions") or []):
        if q.get("status") == "pending" and q.get("feature_id"):
            pending_by_feat[str(q["feature_id"])] = q

    # ── Table 1: extracted features ──────────────────────────────────────────
    feat_rows: list[dict] = []
    notes_rows: list[dict] = []
    feature_counts: dict[str, int] = {}
    for feat in features:
        fid = str(feat.get("id") or "")
        ftype = str(feat.get("type") or "").lower()
        hole = hole_by_feat.get(fid)
        slot = slot_by_feat.get(fid)
        disp = disp_by_feat.get(fid)

        if ftype in _GEOMETRIC_TYPES:
            feature_counts[ftype] = feature_counts.get(ftype, 0) + 1

        # basis: prefer disposition derivation_source, else the driving dim basis
        basis_src = ""
        if disp and disp.get("derivation_source"):
            basis_src = disp.get("derivation_source")
        else:
            for i in (feat.get("related_dimensions") or []):
                d = dims_by_id.get(i)
                if d and d.get("assumption_basis"):
                    basis_src = d.get("assumption_basis")
                    break
        basis = _basis_label(basis_src)

        # status from disposition state (falls back to feature build_status)
        state = (disp or {}).get("state") or (
            {"build": "BUILT"}.get(str(feat.get("build_status") or "").lower(), ""))
        status_label, status_kind = _STATE_STATUS.get(
            state, ("Built" if state == "" and ran else "—", "ok" if ran else "neutral"))
        # a pending question nudges an otherwise-clean feature to amber, but
        # never softens an already-red (excluded) state.
        if (fid in pending_by_feat
                or (disp or {}).get("human_input_state") == "NEEDS_HUMAN_INPUT"):
            if status_kind == "ok":
                status_kind = "warn"

        qty = hole.get("qty") if hole else feat.get("quantity")
        if not qty:
            qty = 1

        # collect this feature's flags (from disposition + resolved warnings)
        fflags = list((disp or {}).get("flags") or [])

        # detail: full dims list w/ tolerances, candidates < 1.0, position basis
        detail_dims = []
        for i in (feat.get("related_dimensions") or []):
            d = dims_by_id.get(i)
            if not d:
                continue
            tol = None
            tp, tm = d.get("tolerance_plus"), d.get("tolerance_minus")
            if tp or tm:
                if tp == tm:
                    tol = f"±{fmt_num(tp)}"
                else:
                    tol = f"+{fmt_num(tp)}/-{fmt_num(tm)}"
            conf = d.get("assumption_confidence")
            cands = d.get("possible_values") or []
            entry = {
                "id": i,
                "value": _dash(fmt_num(_dim_value(d))),
                "applies_to": d.get("applies_to") or "",
                "tolerance": tol,
                "basis": _basis_label(d.get("assumption_basis") or ""),
            }
            # candidate readings + confidence only when confidence < 1.0
            if conf is not None and float(conf) < 1.0 and cands:
                entry["candidates"] = [fmt_num(c) for c in cands]
                entry["confidence"] = round(float(conf), 2)
            detail_dims.append(entry)

        detail = {
            "description": feat.get("description") or "",
            "dimensions": detail_dims,
            "position_basis": (disp or {}).get("position_basis") or feat.get("position_basis") or [],
            "flags": fflags,
            "region_crop": feat.get("region_crop") or "",
            "parent_feature": feat.get("parent_feature") or "",
        }

        feat_rows.append({
            "id": fid,
            "type": ftype,
            "type_label": _TYPE_LABELS.get(ftype, ftype.replace("_", " ").title() or _EM_DASH),
            "size": _dash(_feature_size(feat, dims_by_id, hole, slot)),
            "position": _dash(_feature_position(feat, hole)),
            "basis": basis,
            "qty": qty,
            "status": status_label,
            "status_kind": status_kind,
            "has_question": fid in pending_by_feat,
            "detail": detail,
        })

    # Non-geometric "Notes & references": material/finish/tolerance + reference
    # (balloon) dimensions that were correctly NOT built as geometry.
    for key, label in (("material", "Material"), ("finish", "Finish"),
                       ("general_tolerance", "General tolerance"),
                       ("drawing_standard", "Drawing standard")):
        val = resolved.get(key)
        if val:
            notes_rows.append({"label": label, "value": str(val), "kind": "meta"})
    for d in dims:
        if d.get("is_reference"):
            v = fmt_num(_dim_value(d))
            notes_rows.append({
                "label": f"{d.get('id')} (reference)",
                "value": f"{_dash(v)} {units}".strip() + (f" — {d.get('applies_to')}" if d.get("applies_to") else ""),
                "kind": "reference",
            })

    # ── Table 2: build plan, in build order ──────────────────────────────────
    step_rows: list[dict] = []
    steps = plan.get("steps") or []
    for step in steps:
        stype = str(step.get("type") or "").lower()
        seq = step.get("seq")
        mf = str(step.get("macro_file") or "")
        if stype in _SCAFFOLD_TYPES or mf in _SCAFFOLD_MACROS:
            continue
        if seq in _SCAFFOLD_SEQS and stype not in _TYPE_LABELS:
            continue

        fid = str(step.get("feature_id") or "")
        disp = disp_by_feat.get(fid)
        stage_name = (disp or {}).get("stage_name") or ""
        if stage_name:
            stage_label = stage_name.replace("_", " ").title()
        else:
            stage_label = _STAGE_BY_TYPE.get(stype, _EM_DASH)

        method = str(step.get("construction_method") or "")
        operation = _OPERATION_LABELS.get(method) or _TYPE_LABELS.get(stype) or (
            stype.replace("_", " ").title() if stype else _EM_DASH)

        # key values: up to three driving numbers
        ddu = step.get("dimensions_drawing_units") or {}
        key_vals = []
        for k, v in ddu.items():
            n = fmt_num(v)
            if n is not None:
                key_vals.append(f"{k.replace('_', ' ')} {n}")
            if len(key_vals) >= 3:
                break
        key_values = ", ".join(key_vals) if key_vals else _EM_DASH

        # placement: first position + anchor
        positions = step.get("positions_xy") or []
        anchor = step.get("positioned_from") or ""
        if positions:
            place = _pos_str(positions[0][0], positions[0][1]) or _EM_DASH
            if len(positions) > 1:
                place += f" +{len(positions) - 1}"
        else:
            place = _EM_DASH
        if anchor and anchor not in ("REF_DATUM_A", ""):
            place = f"{place} @ {anchor}"

        # result = disposition ⊕ verification verdict. A step whose feature has
        # no disposition of its own (slot halves, pattern trio) inherits BUILT
        # from its generated status so it doesn't read as perpetually Pending.
        state = (disp or {}).get("state") or ""
        if not state and str(step.get("status") or "").lower() in ("generated", "built", ""):
            state = "BUILT"
        fv = fverify_by_feat.get(fid)
        verdict = str((fv or {}).get("classification") or (fv or {}).get("status") or "").upper()
        result, result_kind = _merge_result(state, verdict, verification_available,
                                             (disp or {}).get("flags") or [])

        detail = {
            "macro_file": step.get("macro_file") or "",
            "values_used": ddu,
            "construction_method": method,
            "positioned_from": anchor,
            "verification": _verify_detail(fv) if fv else None,
            "flags": (disp or {}).get("flags") or [],
            "notes": step.get("notes") or "",
        }

        step_rows.append({
            "step": seq if seq is not None else len(step_rows) + 1,
            "feature_id": fid,
            "stage": stage_label,
            "operation": operation,
            "key_values": key_values,
            "placement": place,
            "result": result,
            "result_kind": result_kind,
            "has_question": fid in pending_by_feat,
            "detail": detail,
        })

    # ── header strip ─────────────────────────────────────────────────────────
    flag_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for item in (plan.get("engineering_review") or []):
        sev = str(item.get("severity") or "").lower()
        if sev in flag_counts:
            flag_counts[sev] += 1

    final_status = _final_status(recon, plan, dispositions)

    # base-solid values_used as an envelope fallback (some parts don't carry
    # envelope-labelled dimensions, but the base step always records L/W/T).
    base_values = {}
    for d in dispositions:
        if str(d.get("stage_name") or "") == "base_solid" and d.get("values_used"):
            base_values = d.get("values_used")
            break

    header = {
        "part_name": resolved.get("part_name") or resolved.get("part_number") or plan.get("part") or out.parent.name,
        "part_number": resolved.get("part_number") or "",
        "revision": resolved.get("revision") or "",
        "units": units,
        "envelope": _envelope(dims, units, base_values),
        "feature_counts": feature_counts,
        "flag_counts": flag_counts,
        "final_status": final_status,
        "feature_count": len(feat_rows),
        "step_count": len(step_rows),
        "pending_questions": len(pending_by_feat),
        "verification_available": verification_available,
    }

    return {
        "part": header["part_name"],
        "ran": ran,
        "header": header,
        "features": feat_rows,
        "notes": notes_rows,
        "build_steps": step_rows,
    }


def _merge_result(state: str, verdict: str, verification_available: bool,
                  flags: list) -> tuple[str, str]:
    """Merge a disposition state with a post-build feature-verification verdict
    into one result label + badge kind (ok/warn/err/neutral/pending)."""
    has_crit = any(str(f.get("flag_tier") or "").upper() == "CRITICAL" for f in flags)
    if state == "EXCLUDED_INCOMPLETE":
        return ("Excluded ✗", "err")
    if state == "PHANTOM_RECLASSIFIED":
        return ("Phantom", "neutral")
    # verification, when we actually have it
    if verdict in ("OK",):
        return ("Built ✓ verified", "ok")
    if verdict in ("MISSING", "MISPLACED", "WRONG_SIZE", "EXTRA"):
        return (f"Built ⚠ {verdict.replace('_', ' ').lower()}", "warn")
    if verdict in ("FAILED", "FAIL"):
        return ("Failed ✗", "err")
    # no per-feature verification verdict available
    if state == "BUILT_WITH_DERIVED_VALUE" or has_crit:
        return ("Built ⚠ flagged", "warn")
    if state == "BUILT":
        if verification_available:
            return ("Built ✓ verified", "ok")
        return ("Built", "ok")
    return ("Pending", "neutral")


def _verify_detail(fv: dict) -> dict:
    return {
        "classification": fv.get("classification") or fv.get("status") or "",
        "measured": fv.get("measured"),
        "expected": fv.get("expected"),
        "reason": fv.get("reason") or "",
    }


def _final_status(recon: dict, plan: dict, dispositions: list) -> str:
    """Part-level READY status. Reconciliation owns it when present; otherwise
    infer from dispositions (any EXCLUDED → not fully ready)."""
    fs = recon.get("final_status")
    if fs:
        return fs
    if not dispositions:
        return _EM_DASH
    states = [str(d.get("state") or "") for d in dispositions]
    if any(s == "EXCLUDED_INCOMPLETE" for s in states):
        return "READY_WITH_OPEN_ITEMS"
    if any((d.get("human_input_state") == "NEEDS_HUMAN_INPUT") for d in dispositions):
        return "READY_WITH_OPEN_ITEMS"
    return "READY"
