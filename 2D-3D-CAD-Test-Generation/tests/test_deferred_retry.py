"""Tests for Workstream 1 — deferred feature retry queue (pipeline.deferred_retry).
Orchestration is exercised with an injected retry function, so no SolidWorks is
needed; the taxonomy + playbook + clarification questions are pure."""
import json
from pathlib import Path

from pipeline.deferred_retry import (
    COM_TIMEOUT,
    GEOMETRY,
    MISSING_PARENT,
    PARAM_RANGE,
    SELECTION,
    SKETCH_DEF,
    UNKNOWN,
    DeferredItem,
    DeferredQueue,
    classify_failure,
    clarification_question,
    retry_strategies,
    run_retry_passes,
)


class TestTaxonomy:
    def test_each_class(self):
        assert classify_failure("hole F004 requires an existing solid body") == MISSING_PARENT
        assert classify_failure("Failed to enter sketch mode for hole/cut") == SELECTION
        assert classify_failure("SelectByID2 returned False for axis") == SELECTION
        assert classify_failure("Sketch is over-defined — cannot extrude") == SKETCH_DEF
        assert classify_failure("CreateCircleByRadius returned Nothing") == GEOMETRY
        assert classify_failure("Type mismatch (-2147352571)") == PARAM_RANGE
        assert classify_failure("the SolidWorks server is busy / call was rejected") == COM_TIMEOUT
        assert classify_failure("something totally novel") == UNKNOWN

    def test_playbook_ends_in_clarify(self):
        for cls in (SELECTION, SKETCH_DEF, GEOMETRY, MISSING_PARENT, PARAM_RANGE, UNKNOWN):
            assert retry_strategies(cls)[-1] == "clarify"


class TestDeferredItem:
    def test_next_strategy_advances_and_never_repeats(self):
        item = DeferredItem("F004", "hole", "requires an existing solid body")
        assert item.error_class == MISSING_PARENT
        s1 = item.next_strategy()
        assert s1 == "reorder_after_parent"
        from pipeline.deferred_retry import Attempt
        item.attempts.append(Attempt(2, s1, "failed"))
        s2 = item.next_strategy()
        assert s2 != s1  # never the identical strategy twice


class TestRunRetryPasses:
    def test_recovers_on_pass_2(self):
        # A build-order defect: fails pass 1, succeeds once the solid exists.
        q = DeferredQueue()
        q.add("F003", "hole", "hole F003 requires an existing solid body")
        calls = {"n": 0}

        def retry_one(item, strategy, ctx):
            calls["n"] += 1
            return True, f"{strategy}: recovered (faces={ctx.get('face_count')})"

        run_retry_passes(q, retry_one, topology_ctx=lambda: {"face_count": 12})
        assert q.items[0].recovered
        assert q.open_items() == []
        assert calls["n"] == 1

    def test_escalates_then_gives_up_to_clarify(self):
        q = DeferredQueue()
        q.add("F007", "hole", "SelectByID2 could not select target face")  # SELECTION: 3 real strategies

        def retry_one(item, strategy, ctx):
            return False, f"{strategy}: still failed"

        run_retry_passes(q, retry_one, cap=3)
        item = q.items[0]
        assert not item.recovered
        # tried the 3 non-clarify SELECTION strategies, never repeating one
        tried = [a.strategy for a in item.attempts]
        assert tried == ["reselect_by_enumerated_topology", "widen_selection_tolerance",
                         "rederive_coords_from_datum"]
        assert len(set(tried)) == len(tried)

    def test_no_thrash_when_all_exhausted(self):
        q = DeferredQueue()
        q.add("F007", "hole", "Type mismatch")  # PARAM_RANGE: only 1 real strategy then clarify
        n = {"c": 0}

        def retry_one(item, strategy, ctx):
            n["c"] += 1
            return False, "no"

        run_retry_passes(q, retry_one, cap=3)
        assert n["c"] == 1  # single real strategy tried once, then stops (no thrash)

    def test_recovered_item_not_retried_again(self):
        q = DeferredQueue()
        q.add("F003", "hole", "requires an existing solid body")
        seen = []

        def retry_one(item, strategy, ctx):
            seen.append(strategy)
            return True, "ok"

        run_retry_passes(q, retry_one, cap=3)
        assert len(seen) == 1  # recovered on first attempt, never retried


class TestClarificationQuestion:
    def test_shapes_like_assist_question(self):
        item = DeferredItem("F007", "hole", "SelectByID2 could not select target face")
        q = clarification_question(item, part="157-C")
        assert q["feature_id"] == "F007"
        assert q["default_if_unanswered"]  # always populated
        assert q["status"] == "pending"
        assert "select" in q["question_text"].lower()
        # consumable by human_assist.Question
        from pipeline.human_assist import Question
        Question.from_dict(q)


class TestQueueWrite:
    def test_writes_deferred_log(self, tmp_path):
        q = DeferredQueue()
        q.add("F003", "hole", "requires an existing solid body")
        q.items[0].recovered = True
        q.add("F007", "hole", "SelectByID2 failed")
        p = q.write(tmp_path)
        assert p.name == "_deferred_log.json"
        d = json.loads(p.read_text())
        assert d["total"] == 2 and d["recovered"] == 1 and d["open"] == 1


class TestAssistIntegration:
    def test_deferred_open_becomes_assist_question(self, tmp_path):
        from pipeline.human_assist import generate_assist_queue

        class _Res:
            resolved_extraction = {"dimensions": []}
            dim_resolutions = {}
            flags = []

        deferred = [{"feature_id": "F007", "feature_type": "hole", "recovered": False,
                     "error_class": SELECTION, "error_text": "SelectByID2 could not select face"},
                    {"feature_id": "F002", "feature_type": "hole", "recovered": True,
                     "error_class": MISSING_PARENT, "error_text": "requires existing solid"}]
        q = generate_assist_queue(resolution=_Res(), part="P", part_dir=tmp_path,
                                  safe_name="P", deferred_items=deferred)
        fids = {qq.feature_id for qq in q.pending()}
        assert "F007" in fids       # still-open deferred -> a question
        assert "F002" not in fids   # recovered -> no question
