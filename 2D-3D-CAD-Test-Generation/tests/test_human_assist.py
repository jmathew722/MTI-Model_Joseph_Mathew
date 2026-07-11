"""Tests for the human-assist escalation layer (pipeline.human_assist) and
pattern learning (pipeline.learned_patterns)."""
import json
from pathlib import Path

import pytest

from pipeline.human_assist import (
    AssistQueue,
    Candidate,
    Question,
    apply_answers,
    escalation_eligible,
    generate_assist_queue,
    prioritize_and_cap,
    KIND_AMBIGUOUS_DIM,
    KIND_UNRESOLVED_POS,
    PENDING,
    ANSWERED,
)


# --------------------------------------------------------------------------- #
# Escalation ladder ordering — escalate ONLY after the full ladder
# --------------------------------------------------------------------------- #
class TestEscalationLadder:
    def test_dim_needs_three_stages(self):
        assert not escalation_eligible({"resolver_plausibility"}, KIND_AMBIGUOUS_DIM)
        assert not escalation_eligible({"resolver_plausibility", "typ_derivation"}, KIND_AMBIGUOUS_DIM)
        assert escalation_eligible(
            {"resolver_plausibility", "typ_derivation", "correction_loop"}, KIND_AMBIGUOUS_DIM)

    def test_chronic_kind_also_needs_method_experiments(self):
        three = {"resolver_plausibility", "typ_derivation", "correction_loop"}
        assert not escalation_eligible(three, KIND_UNRESOLVED_POS)
        assert escalation_eligible(three | {"method_experiments"}, KIND_UNRESOLVED_POS)

    def test_ladder_order_enforced_via_fake_resolver(self):
        # A question must never be generated before all stages are recorded.
        # Simulate the pipeline recording stages incrementally.
        recorded = set()
        for stage in ("resolver_plausibility", "typ_derivation"):
            recorded.add(stage)
            assert not escalation_eligible(recorded, KIND_AMBIGUOUS_DIM)
        recorded.add("correction_loop")
        assert escalation_eligible(recorded, KIND_AMBIGUOUS_DIM)


# --------------------------------------------------------------------------- #
# Prioritization + cap
# --------------------------------------------------------------------------- #
class TestPrioritizeAndCap:
    def _q(self, fid, pri):
        return Question(question_id=f"Q-{fid}", part="P", feature_id=fid,
                        kind=KIND_AMBIGUOUS_DIM, question_text="?",
                        default_if_unanswered=1.0, priority=pri)

    def test_cap_keeps_highest_priority(self):
        qs = [self._q("F1", 10), self._q("F2", 200), self._q("F3", 50), self._q("F4", 5)]
        kept = prioritize_and_cap(qs, 2)
        assert [q.feature_id for q in kept] == ["F2", "F3"]

    def test_cap_zero(self):
        assert prioritize_and_cap([self._q("F1", 1)], 0) == []


# --------------------------------------------------------------------------- #
# Question schema — default_if_unanswered always populated; round-trip
# --------------------------------------------------------------------------- #
class TestQuestionSchema:
    def test_default_always_populated(self):
        q = Question(question_id="Q", part="P", feature_id="F", kind=KIND_AMBIGUOUS_DIM,
                     question_text="Is D008 0.44 or 0.94?", default_if_unanswered=0.94,
                     candidates=[Candidate(0.44, "chain"), Candidate(0.94, "geometry")])
        assert q.default_if_unanswered is not None
        d = q.as_dict()
        assert d["default_if_unanswered"] == 0.94
        assert Question.from_dict(d).default_if_unanswered == 0.94

    def test_queue_roundtrip(self, tmp_path):
        q = Question("Q", "P", "F", KIND_AMBIGUOUS_DIM, "?", 1.0)
        queue = AssistQueue("P", [q])
        p = queue.write(tmp_path, "P")
        loaded = AssistQueue.load(p)
        assert loaded.part == "P" and len(loaded.questions) == 1
        assert loaded.pending()[0].question_id == "Q"


# --------------------------------------------------------------------------- #
# generate_assist_queue from a resolution-like stub
# --------------------------------------------------------------------------- #
class _DR:
    def __init__(self, value, basis, tier="CRITICAL", note=""):
        self.resolved_value = value
        self.assumption_basis = basis
        self.flag_tier = tier
        self.human_note = note


class _Resolution:
    def __init__(self, dims_raw, dim_res, flags=None):
        self.resolved_extraction = {"dimensions": dims_raw}
        self.dim_resolutions = dim_res
        self.flags = flags or []


class TestGenerateQueue:
    def test_ambiguous_dimension_becomes_question(self, tmp_path):
        res = _Resolution(
            dims_raw=[{"id": "D008", "applies_to": "length", "value_unclear": True,
                       "possible_values": [0.44, 0.94]}],
            dim_res={"D008": _DR(0.94, "geometric_reasonableness", "CRITICAL",
                                 "guessed 0.94 as the geometrically valid reading")})
        q = generate_assist_queue(resolution=res, part="P", part_dir=tmp_path, safe_name="P")
        assert len(q.pending()) == 1
        question = q.pending()[0]
        assert question.kind == KIND_AMBIGUOUS_DIM
        assert question.target_dimension_id == "D008"
        assert question.default_if_unanswered == 0.94  # ships the current best value
        assert {c.value for c in question.candidates} == {0.44, 0.94}
        assert (tmp_path / "P_assist_queue.json").is_file()

    def test_confident_dimension_is_not_escalated(self, tmp_path):
        res = _Resolution(
            dims_raw=[{"id": "D001", "applies_to": "length", "value_unclear": False,
                       "possible_values": []}],
            dim_res={"D001": _DR(4.0, "explicit_callout", "HIGH")})
        q = generate_assist_queue(resolution=res, part="P", part_dir=tmp_path, safe_name="P")
        assert q.pending() == []

    def test_cap_enforced(self, tmp_path):
        dims_raw = [{"id": f"D{i:03d}", "applies_to": "hole_diameter", "value_unclear": True,
                     "possible_values": [0.1 * i, 0.2 * i]} for i in range(1, 8)]
        dim_res = {f"D{i:03d}": _DR(0.1 * i, "last_resort", "CRITICAL") for i in range(1, 8)}
        q = generate_assist_queue(resolution=_Resolution(dims_raw, dim_res),
                                  part="P", part_dir=tmp_path, safe_name="P", cap=3)
        assert len(q.pending()) == 3  # capped


# --------------------------------------------------------------------------- #
# apply_answers — numeric dim answers become the resolver human_answers map
# --------------------------------------------------------------------------- #
class TestApplyAnswers:
    def test_numeric_answer_maps_to_dimension(self, tmp_path):
        q = Question("Q1", "P", "F002", KIND_AMBIGUOUS_DIM, "?", 0.94,
                     target_dimension_id="D008")
        AssistQueue("P", [q]).write(tmp_path, "P")
        human = apply_answers(tmp_path, "P", {"Q1": "0.44"})
        assert human == {"D008": 0.44}
        reloaded = AssistQueue.load(tmp_path / "P_assist_queue.json")
        assert reloaded.questions[0].status == ANSWERED
        assert reloaded.questions[0].answer == "0.44"

    def test_human_answer_outranks_spec_in_resolver(self):
        # Integration with the resolver: a human answer wins over the generic
        # ladder and is tagged human_provided.
        from pipeline.resolver import resolve_extraction
        raw = {"units": "inch", "confidence": 0.8,
               "dimensions": [{"id": "D008", "type": "linear", "value": 0.94, "unit": "inch",
                               "applies_to": "length", "value_unclear": True,
                               "possible_values": [0.44, 0.94]}],
               "features": [], "hole_callouts": []}
        res = resolve_extraction(raw, human_answers={"D008": 0.44})
        dr = res.dim_resolutions["D008"]
        assert dr.resolved_value == 0.44
        assert dr.assumption_basis == "human_provided"


# --------------------------------------------------------------------------- #
# Non-blocking invariant: disposition overlay is additive, no state replaced
# --------------------------------------------------------------------------- #
class TestDispositionOverlay:
    def test_overlay_is_additive(self, tmp_path):
        from pipeline.human_assist import overlay_dispositions
        # A disposition table with a BUILT feature.
        disp = [{"feature_id": "F002", "state": "BUILT", "stage": 4}]
        (tmp_path / "P_build_dispositions.json").write_text(json.dumps(disp))
        q = Question("Q1", "P", "F002", KIND_AMBIGUOUS_DIM, "?", 0.94)
        overlay_dispositions(tmp_path, "P", AssistQueue("P", [q]))
        out = json.loads((tmp_path / "P_build_dispositions.json").read_text())
        assert out[0]["state"] == "BUILT"                 # geometric state UNCHANGED
        assert out[0]["needs_human_input"] is True        # additive overlay
        assert out[0]["question_id"] == "Q1"


# --------------------------------------------------------------------------- #
# Pattern learning (Task 5)
# --------------------------------------------------------------------------- #
class TestLearnedPatterns:
    def _lessons(self, tmp_path, records):
        p = tmp_path / "lessons_learned.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return p

    def test_generalizes_after_two_parts(self, tmp_path):
        from pipeline.learned_patterns import scan_and_generalize
        recs = []
        for i, part in enumerate(["A", "B"]):
            qid = f"Q-{part}"
            recs.append({"kind": "human_assist_question", "question_id": qid, "part": part,
                         "question_kind": "ambiguous_dimension",
                         "question_text": "Is D008 (length) 0.44 or 0.94?",
                         "candidates": [{"value": 0.44}, {"value": 0.94}]})
            recs.append({"kind": "human_assist_answer", "question_id": qid, "part": part,
                         "answer": 0.94})  # both chose the larger
        learned = scan_and_generalize(self._lessons(tmp_path, recs), out_dir=tmp_path)
        assert learned, "expected a generalized pattern after 2 consistent parts"
        assert learned[0].choice_rule == "prefer_larger_candidate"
        assert (tmp_path / "LEARNED_PATTERNS.md").is_file()
        assert (tmp_path / "learned_patterns.json").is_file()

    def test_no_generalize_from_single_part(self, tmp_path):
        from pipeline.learned_patterns import scan_and_generalize
        recs = [{"kind": "human_assist_question", "question_id": "Q1", "part": "A",
                 "question_kind": "ambiguous_dimension",
                 "question_text": "Is D008 (length) 0.44 or 0.94?",
                 "candidates": [{"value": 0.44}, {"value": 0.94}]},
                {"kind": "human_assist_answer", "question_id": "Q1", "part": "A", "answer": 0.94}]
        assert scan_and_generalize(self._lessons(tmp_path, recs), out_dir=tmp_path) == []
