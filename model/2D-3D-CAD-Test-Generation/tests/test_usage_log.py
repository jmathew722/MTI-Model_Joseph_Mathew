"""Tests for the token/cost ledger (pipeline.usage_log)."""
import json

from pipeline.usage_log import LEDGER_JSONL, LEDGER_TXT, estimate_cost, record_run


class TestEstimateCost:
    def test_sonnet_input_output(self):
        # 1M input @ $3 + 1M output @ $15 = $18.00
        cost = estimate_cost(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "claude-sonnet-4-6"
        )
        assert round(cost, 4) == 18.0

    def test_opus_input_output(self):
        cost = estimate_cost(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "claude-opus-4-8"
        )
        assert round(cost, 4) == 30.0  # $5 + $25

    def test_cache_read_and_write_priced(self):
        # cache read 1M @ $0.30 + cache write 1M @ $3.75 = $4.05 (sonnet)
        cost = estimate_cost(
            {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000},
            "claude-sonnet-4-6",
        )
        assert round(cost, 4) == 4.05

    def test_cache_hit_is_free(self):
        assert estimate_cost({"cache_hits": 1, "calls": 0}, "claude-sonnet-4-6") == 0.0

    def test_unknown_model_uses_fallback(self):
        # Falls back to sonnet-tier pricing rather than crashing.
        cost = estimate_cost({"input_tokens": 1_000_000}, "some-future-model")
        assert round(cost, 4) == 3.0


class TestRecordRun:
    def test_writes_ledger_files(self, tmp_path):
        usage = {"input_tokens": 10_000, "output_tokens": 4_000, "calls": 1}
        txt = record_run(tmp_path, "135-A", "claude-sonnet-4-6", usage)
        assert txt.exists()
        assert (tmp_path / LEDGER_JSONL).exists()
        assert (tmp_path / LEDGER_TXT).exists()
        body = txt.read_text(encoding="utf-8")
        assert "135-A" in body
        assert "TOTAL API COST TO DATE" in body

    def test_running_total_accumulates(self, tmp_path):
        record_run(tmp_path, "A", "claude-sonnet-4-6", {"input_tokens": 1_000_000})  # $3
        record_run(tmp_path, "B", "claude-opus-4-8", {"input_tokens": 1_000_000})    # $5
        rows = [
            json.loads(l)
            for l in (tmp_path / LEDGER_JSONL).read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert len(rows) == 2
        assert "$8.0000" in (tmp_path / LEDGER_TXT).read_text(encoding="utf-8")

    def test_cache_hit_row_is_free_and_labeled(self, tmp_path):
        txt = record_run(tmp_path, "A", "claude-sonnet-4-6", {"cache_hits": 1, "calls": 0})
        body = txt.read_text(encoding="utf-8")
        assert "(cache hit)" in body
        assert "$0.0000" in body
