"""Tests for pipeline.requirements_check — human notes as graded requirements."""
import json

from pipeline.requirements_check import (
    check_requirements,
    find_notes_file,
    parse_requirements,
    review_items,
    run_requirements_check,
    write_requirements_json,
)


def _extraction():
    return {
        "part_number": "TEST-1",
        "material": "Aluminum 6061-T6",
        "finish": "",
        "dimensions": [
            {"dimension_id": "D001", "value": 100.0, "resolved_value": 100.0},
            {"dimension_id": "D002", "value": 12.7},
        ],
        "hole_callouts": [
            {"hole_id": "H001", "type": "thru", "diameter": 6.0, "qty": 4},
        ],
        "features": [
            {"feature_id": "F001", "type": "extrude_boss", "description": "base"},
            {"feature_id": "F002", "type": "fillet", "description": "corner fillets"},
        ],
    }


class TestParsing:
    def test_lines_and_bullets(self):
        text = "- 4x mounting holes\n* material 6061\n\n2. fillet all corners\nplain line\n"
        reqs = parse_requirements(text)
        assert [r["text"] for r in reqs] == [
            "4x mounting holes", "material 6061", "fillet all corners", "plain line"]
        assert [r["id"] for r in reqs] == ["R001", "R002", "R003", "R004"]

    def test_headings_and_blanks_skipped(self):
        reqs = parse_requirements("Requirements:\n\n- one thing\n")
        assert len(reqs) == 1

    def test_empty_text(self):
        assert parse_requirements("") == []


class TestGrading:
    def _grade(self, line, extraction=None):
        reqs = parse_requirements(line)
        check_requirements(reqs, extraction or _extraction())
        return reqs[0]

    def test_feature_and_number_matched_is_met(self):
        r = self._grade("4 mounting holes of 6.0 diameter")
        assert r["status"] == "met"

    def test_feature_present_number_unmatched_is_partial(self):
        r = self._grade("mounting holes of 9.99 diameter")
        assert r["status"] == "partial"

    def test_missing_feature_is_unmet(self):
        r = self._grade("add a keyway slot on the shaft")
        assert r["status"] == "unmet"

    def test_number_only_matched_via_unit_conversion(self):
        # 0.5 inch == 12.7 mm (D002)
        r = self._grade("overall thickness 0.5")
        assert r["status"] == "met"

    def test_number_only_unmatched_is_unmet(self):
        r = self._grade("overall length must be 77.7")
        assert r["status"] == "unmet"

    def test_material_match_is_met(self):
        r = self._grade("material: aluminum 6061")
        assert r["status"] == "met"

    def test_material_conflict_is_unmet(self):
        r = self._grade("material: brass")
        assert r["status"] == "unmet"

    def test_material_unreadable_is_not_applicable(self):
        ex = _extraction()
        ex["material"] = ""
        r = self._grade("material: brass", ex)
        assert r["status"] == "not_applicable"

    def test_process_note_is_not_applicable_never_fabricated(self):
        r = self._grade("anodize black after machining")
        assert r["status"] == "not_applicable"
        assert "manually" in r["note"]

    def test_uncheckable_text_is_not_applicable(self):
        r = self._grade("ship with care")
        assert r["status"] == "not_applicable"


class TestReviewItemsAndPersistence:
    def test_severity_mapping(self):
        reqs = [
            {"id": "R001", "text": "a", "status": "unmet", "note": ""},
            {"id": "R002", "text": "b", "status": "partial", "note": ""},
            {"id": "R003", "text": "c", "status": "not_applicable", "note": ""},
            {"id": "R004", "text": "d", "status": "met", "note": ""},
        ]
        sev = [i["severity"] for i in review_items(reqs)]
        assert sev == ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        assert all(i["source"] == "requirement" for i in review_items(reqs))
        assert review_items(reqs)[0]["status"] == "unmet"

    def test_write_requirements_json(self, tmp_path):
        reqs = [{"id": "R001", "text": "x", "status": "met", "note": "ok"}]
        p = write_requirements_json(tmp_path, "PART", reqs)
        data = json.loads(p.read_text())
        assert p.name == "PART_requirements.json"
        assert data["summary"] == {"met": 1}
        assert data["requirements"][0]["id"] == "R001"


class TestDiscoveryAndWrapper:
    def test_find_notes_file_precedence(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("- a\n")
        assert find_notes_file(tmp_path).name == "requirements.txt"
        (tmp_path / "notes.txt").write_text("- b\n")
        assert find_notes_file(tmp_path).name == "notes.txt"
        (tmp_path / "P1_notes.txt").write_text("- c\n")
        assert find_notes_file(tmp_path, "P1").name == "P1_notes.txt"

    def test_find_notes_file_none(self, tmp_path):
        assert find_notes_file(tmp_path) is None

    def test_wrapper_no_file(self):
        reqs, note = run_requirements_check(None, _extraction())
        assert reqs == [] and note.startswith("skipped")

    def test_wrapper_empty_file(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("Requirements:\n\n")
        reqs, note = run_requirements_check(f, _extraction())
        assert reqs == [] and "no requirement lines" in note

    def test_wrapper_grades_and_counts_unmet(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("- 4 holes 6.0 dia\n- add a keyway slot\n")
        reqs, note = run_requirements_check(f, _extraction())
        assert [r["status"] for r in reqs] == ["met", "unmet"]
        assert "1 unmet" in note
