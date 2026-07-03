"""Tests for the severity-ranked engineering review (pipeline.engineering_review)."""
import json

from pipeline.engineering_review import (
    SEVERITY_ORDER,
    build_review_items,
    format_review,
    severity_counts,
    write_review,
)
from pipeline.macro_generator import generate_macro_package
from pipeline.resolver import resolve_extraction
from pipeline.validator import format_verification_report, run_verification

from tests.test_macro_generator import bracket_drawing


def _ambiguous_bracket():
    """The bracket drawing with one dimension made ambiguous, so the resolver
    produces at least one non-HIGH flag."""
    data = bracket_drawing()
    d = data["dimensions"][0]
    d["value_unclear"] = True
    d["ambiguity_reason"] = "smudged print"
    d["possible_values"] = [d["value"], d["value"] + 0.5]
    return data


def _run(tmp_path, data):
    resolution = resolve_extraction(data)
    model, report = run_verification(resolution.clean_extraction)
    pkg = generate_macro_package(
        model, data, format_verification_report(model, report), tmp_path,
        resolution=resolution,
    )
    return resolution, pkg


class TestBuildReviewItems:
    def test_sorted_most_urgent_first(self, tmp_path):
        resolution, pkg = _run(tmp_path, _ambiguous_bracket())
        items = build_review_items(
            resolution=resolution, pkg=pkg,
            build_skipped=[("F009", "fillet", "no edges found")],
        )
        ranks = [SEVERITY_ORDER.index(i["severity"]) for i in items]
        assert ranks == sorted(ranks)

    def test_prohibited_feature_is_critical(self, tmp_path):
        resolution, pkg = _run(tmp_path, bracket_drawing())
        items = build_review_items(resolution=resolution, pkg=pkg)
        f004 = [i for i in items if i["id"] == "F004"]
        assert f004 and f004[0]["severity"] == "CRITICAL"
        assert "MANUAL" in f004[0]["decision"].upper()

    def test_com_skipped_feature_is_critical(self):
        items = build_review_items(build_skipped=[("F002", "hole", "degenerate data")])
        assert items[0]["severity"] == "CRITICAL"
        assert "F002" in items[0]["what"]

    def test_build_caveat_is_medium(self):
        items = build_review_items(build_caveats=["F003: fillet applied to 12 edges (all)"])
        assert items[0]["severity"] == "MEDIUM"

    def test_confirmed_dimensions_not_listed(self, tmp_path):
        # A fully clear drawing: no dimension items should appear (only the
        # prohibited-shell macro items from the bracket fixture).
        resolution, pkg = _run(tmp_path, bracket_drawing())
        items = build_review_items(resolution=resolution)
        assert all(i["source"] != "dimension" or i["severity"] != "LOW"
                   or "confirmed" not in i["what"].lower() for i in items)

    def test_every_item_has_required_fields(self, tmp_path):
        resolution, pkg = _run(tmp_path, _ambiguous_bracket())
        for it in build_review_items(resolution=resolution, pkg=pkg):
            for key in ("severity", "source", "what", "decision", "why", "affects"):
                assert it[key] is not None
            assert it["severity"] in SEVERITY_ORDER


class TestReportFile:
    def test_written_by_macro_package(self, tmp_path):
        _, pkg = _run(tmp_path, _ambiguous_bracket())
        review = pkg.root / f"{pkg.root.name}_engineering_review.txt"
        assert review.exists()
        body = review.read_text(encoding="utf-8")
        assert "ENGINEERING REVIEW" in body
        assert "CRITICAL" in body  # severity guide is always present

    def test_in_build_plan(self, tmp_path):
        _, pkg = _run(tmp_path, _ambiguous_bracket())
        plan = json.loads(pkg.build_plan_json.read_text(encoding="utf-8"))
        assert "engineering_review" in plan
        assert isinstance(plan["engineering_review"], list)

    def test_write_review_roundtrip(self, tmp_path):
        items = build_review_items(build_skipped=[("F001", "hole", "reason")])
        path = write_review(tmp_path, "TESTPART", items)
        assert path.name == "TESTPART_engineering_review.txt"
        body = path.read_text(encoding="utf-8")
        assert "1. [CRITICAL] F001 (build)" in body

    def test_counts(self):
        items = build_review_items(
            build_skipped=[("F1", "hole", "r")],
            build_caveats=["c1", "c2"],
        )
        counts = severity_counts(items)
        assert counts["CRITICAL"] == 1 and counts["MEDIUM"] == 2

    def test_clean_run_reads_clean(self):
        body = format_review("P", [])
        assert "No assumptions or manual steps" in body
