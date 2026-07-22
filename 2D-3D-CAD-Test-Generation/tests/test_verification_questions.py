"""Human-verification question generation (pipeline/verification_questions.py).

Turns a run's engineering flags into concise confirmation questions + compiles
the operator's answers into a correction block. Tests run with use_llm=False
(deterministic fallback) so no network/provider is needed.
"""
import json

import pytest

from pipeline.verification_questions import (
    CACHE_SUFFIX,
    build_verification_items,
    compile_corrections,
    _fallback_question,
)


def _write_plan(part_dir, prefix, review):
    (part_dir / f"{prefix}_build_plan.json").write_text(
        json.dumps({"part": prefix, "engineering_review": review}), encoding="utf-8")


def _review():
    return [
        {"severity": "CRITICAL", "source": "dim", "id": "D001",
         "what": "D001 reading (1.12) is illegible; kept as best guess for outside_diameter.",
         "why": "unverifiable reading", "affects": "dimension D001"},
        {"severity": "HIGH", "source": "overview", "id": "OV-ENV",
         "what": "Overview shows an overall height 1.42 but no extracted dimension matches it.",
         "why": "overview vs extraction", "affects": "overall envelope"},
    ]


def test_no_plan_returns_empty(tmp_path):
    d = build_verification_items(tmp_path, use_llm=False)
    assert d["count"] == 0 and d["items"] == []


def test_builds_items_with_fallback_questions(tmp_path):
    _write_plan(tmp_path, "158-C", _review())
    d = build_verification_items(tmp_path, use_llm=False)
    assert d["part"] == "158-C"
    assert d["count"] == 2
    assert d["counts"] == {"CRITICAL": 1, "HIGH": 1}
    ids = [it["id"] for it in d["items"]]
    assert len(set(ids)) == 2  # unique ids
    for it in d["items"]:
        assert it["question"]                     # every flag gets a question
        assert it["severity"] in ("CRITICAL", "HIGH")
        assert it["flag_id"] in ("D001", "OV-ENV")


def test_questions_are_cached(tmp_path):
    _write_plan(tmp_path, "P", _review())
    build_verification_items(tmp_path, use_llm=False)
    cache = tmp_path / f"P{CACHE_SUFFIX}"
    assert cache.is_file()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert len(payload["questions"]) == 2 and payload["flags_hash"]
    # A second call reuses the cache (same questions).
    d2 = build_verification_items(tmp_path, use_llm=False)
    assert [it["question"] for it in d2["items"]] == payload["questions"]


def test_cache_invalidated_when_flags_change(tmp_path):
    _write_plan(tmp_path, "P", _review())
    build_verification_items(tmp_path, use_llm=False)
    # Change the flag set → cache must regenerate (different hash, 1 item).
    _write_plan(tmp_path, "P", _review()[:1])
    d = build_verification_items(tmp_path, use_llm=False)
    assert d["count"] == 1


def test_prefix_discovered_from_disk_not_folder_name(tmp_path):
    # Folder named A001581E but artifacts prefixed 158-C (the real convention).
    _write_plan(tmp_path, "158-C", _review())
    d = build_verification_items(tmp_path, use_llm=False)
    assert d["part"] == "158-C"


def test_fallback_question_is_concise_and_references_feature():
    q = _fallback_question({"id": "D001", "what": "D001 reading (1.12) is illegible. Extra detail."})
    assert "D001" in q and q.endswith("?")
    assert "Extra detail" not in q  # only the first clause is used


def test_compile_corrections_skips_empty_and_tags_feature():
    items = [{"id": "i0", "flag_id": "D001"}, {"id": "i1", "flag_id": "OV-ENV"}]
    out = compile_corrections(items, {"i0": "diameter is 1.125", "i1": "   "})
    assert "[D001] diameter is 1.125" in out
    assert "OV-ENV" not in out           # empty answer skipped
    assert compile_corrections(items, {"i0": ""}) == ""   # nothing → empty block


def test_max_flags_cap(tmp_path):
    from pipeline.verification_questions import _MAX_FLAGS
    big = [{"severity": "LOW", "id": f"F{i}", "what": f"flag {i}"} for i in range(_MAX_FLAGS + 10)]
    _write_plan(tmp_path, "P", big)
    d = build_verification_items(tmp_path, use_llm=False)
    assert d["count"] == _MAX_FLAGS
