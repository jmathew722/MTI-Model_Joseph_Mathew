"""Extraction-is-truth Task 2 — verbatim carry, determinism, and the resolution
cache (2026-07-12).

158-C evidence: F002's D002 (1.56, read straight off the drawing) carried
assumption_confidence 0.92 instead of 1.0 — a sole extracted reading must pass
through resolution unchanged and pinned, so close-candidate scoring can never
flip it between runs.
"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.resolution_cache import cache_key, resolve_with_cache
from pipeline.resolver import resolve_extraction

FIX = Path(__file__).resolve().parent / "fixtures" / "commit_mode"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Verbatim invariant — a sole clean reading is 1.0 / extracted_verbatim
# --------------------------------------------------------------------------- #
class TestVerbatimInvariant:
    def test_158c_slot_offset_is_verbatim_full_confidence(self):
        raw = _load("158-C_extraction.json")
        res = resolve_extraction(raw)
        d003 = res.dim_resolutions["D003"]  # the 1.56 slot offset ("position")
        assert d003.assumption_confidence == 1.0
        assert d003.assumption_basis == "extracted_verbatim"
        assert d003.resolved_value == pytest.approx(1.56)

    def test_every_sole_reading_dimension_is_verbatim_or_higher_tier(self):
        # Every dimension the extraction did NOT flag unclear/ambiguous must be
        # confidence 1.0 (never a bare sub-1.0 score with no candidate list).
        raw = _load("158-C_extraction.json")
        res = resolve_extraction(raw)
        dims_by_id = {d["id"]: d for d in raw["dimensions"]}
        for did, dr in res.dim_resolutions.items():
            src = dims_by_id.get(did, {})
            flagged = bool(src.get("value_unclear") or src.get("resolution_required")
                          or (src.get("ambiguity_reason") or "").strip())
            if not flagged and dr.assumption_basis in ("extracted_verbatim",):
                assert dr.assumption_confidence == 1.0, did

    def test_sub_1_confidence_always_carries_a_deciding_rule(self):
        # Any resolution below 1.0 confidence must have a human_note explaining
        # the deciding rule — never a bare score.
        raw = _load("M_121-B_extraction.json")
        res = resolve_extraction(raw)
        for did, dr in res.dim_resolutions.items():
            if dr.assumption_confidence < 1.0:
                assert dr.human_note and len(dr.human_note) > 10, did


# --------------------------------------------------------------------------- #
# Determinism — resolve_extraction is a pure function of its input
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_five_runs_are_byte_identical(self):
        raw = _load("158-C_extraction.json")
        outs = [json.dumps(resolve_extraction(json.loads(json.dumps(raw))).resolved_extraction,
                           sort_keys=True) for _ in range(5)]
        assert len(set(outs)) == 1, "resolve_extraction produced different output across identical runs"

    def test_m121b_five_runs_are_byte_identical(self):
        raw = _load("M_121-B_extraction.json")
        outs = [json.dumps(resolve_extraction(json.loads(json.dumps(raw))).resolved_extraction,
                           sort_keys=True) for _ in range(5)]
        assert len(set(outs)) == 1

    def test_candidate_order_does_not_affect_result(self):
        # Reversing possible_values must not change which candidate wins (no
        # dict/order-dependent tie-breaking) — the decision is by rule, not order.
        raw = _load("158-C_extraction.json")
        res1 = resolve_extraction(json.loads(json.dumps(raw)))
        for d in raw["dimensions"]:
            if d.get("possible_values"):
                d["possible_values"] = list(reversed(d["possible_values"]))
        res2 = resolve_extraction(raw)
        v1 = {k: v.resolved_value for k, v in res1.dim_resolutions.items()}
        v2 = {k: v.resolved_value for k, v in res2.dim_resolutions.items()}
        assert v1 == v2


# --------------------------------------------------------------------------- #
# Resolution cache — key = hash(extraction + resolver version)
# --------------------------------------------------------------------------- #
class TestResolutionCache:
    def test_cache_key_stable_across_dict_key_order(self):
        raw = _load("158-C_extraction.json")
        shuffled = json.loads(json.dumps(raw))  # same content, same structure
        assert cache_key(raw) == cache_key(shuffled)

    def test_cache_key_changes_with_resolver_version(self):
        raw = _load("158-C_extraction.json")
        assert cache_key(raw, "v1") != cache_key(raw, "v2")

    def test_cache_key_changes_with_extraction_content(self):
        raw = _load("158-C_extraction.json")
        mutated = json.loads(json.dumps(raw))
        mutated["dimensions"][0]["value"] += 0.001
        assert cache_key(raw) != cache_key(mutated)

    def test_rerun_hits_cache_byte_identical(self, tmp_path):
        raw = _load("158-C_extraction.json")
        cache_dir = tmp_path / ".resolution_cache"
        r1 = resolve_with_cache(json.loads(json.dumps(raw)), cache_dir=cache_dir)
        entries_after_first = list(cache_dir.glob("*.json"))
        assert len(entries_after_first) == 1
        stored = json.loads(entries_after_first[0].read_text())
        r2 = resolve_with_cache(json.loads(json.dumps(raw)), cache_dir=cache_dir)
        entries_after_second = list(cache_dir.glob("*.json"))
        assert len(entries_after_second) == 1  # same key -> no new entry
        assert json.dumps(r1.resolved_extraction, sort_keys=True) == \
               json.dumps(r2.resolved_extraction, sort_keys=True)
        assert stored["resolved_hash"] == json.loads(
            entries_after_second[0].read_text())["resolved_hash"]

    def test_changed_extraction_gets_a_new_cache_entry(self, tmp_path):
        raw = _load("158-C_extraction.json")
        cache_dir = tmp_path / ".resolution_cache"
        resolve_with_cache(json.loads(json.dumps(raw)), cache_dir=cache_dir)
        mutated = json.loads(json.dumps(raw))
        mutated["dimensions"][0]["value"] += 0.001
        resolve_with_cache(mutated, cache_dir=cache_dir)
        assert len(list(cache_dir.glob("*.json"))) == 2

    def test_no_cache_dir_is_pure_passthrough(self):
        raw = _load("158-C_extraction.json")
        r = resolve_with_cache(raw, cache_dir=None)
        assert r.resolved_extraction  # behaves exactly like resolve_extraction
