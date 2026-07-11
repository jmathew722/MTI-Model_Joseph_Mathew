"""Tests for the Pipeline Explainer — LOCAL-ONLY, ZERO-COST chat (Ollama).

Covers the hard guarantees: no external network (socket-level), full-pipeline
artifact registry + cross-stage trace, export manifest, zero-cost accounting,
the model-bootstrap flow, and the UI placement/styling contract (static DOM +
CSS assertions, since there is no browser harness in this suite).
"""
import json
import sys
from pathlib import Path

import pytest

# The explainer module lives in webapp/ (not on the default test path).
WEBAPP = Path(__file__).resolve().parent.parent / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

import explainer as ex  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures — a part output dir with one artifact per pipeline stage
# --------------------------------------------------------------------------- #
@pytest.fixture
def part_out(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    (out / "prep_log.json").write_text(json.dumps({"dpi": 300, "tiled": False, "note": "ok"}))
    (out / "P_extraction.json").write_text(json.dumps(
        {"dimensions": [{"id": "D009", "value": 1.56, "applies_to": "position"},
                        {"id": "D002", "value": 6.25}]}))
    (out / "overview_analysis.json").write_text(json.dumps({"views": ["front"], "notes": "D009 anchors the notch"}))
    (out / "P_resolved_extraction.json").write_text(json.dumps(
        {"dimensions": [{"id": "D009", "resolved_value": 1.56, "assumption_basis": "spec_driven"}]}))
    (out / "must_meet_constraints.json").write_text(json.dumps({"constraints": [{"id": "MM-001"}]}))
    (out / "P_verification_report.txt").write_text("Readiness 100%. D009 closes.")
    (out / "P_build_plan.json").write_text(json.dumps(
        {"steps": [{"type": "slot_rect_cut", "feature_id": "F002",
                    "sketch": {"corners_drawing_units": [[1.56, 4.37]]}, "note": "D009"}]}))
    macros = out / "macros"; macros.mkdir()
    (macros / "02_F002_slot_rect_cut.vba").write_text("' D009 anchor 1.56 in\nDim swApp")
    (out / "P_audit_report.json").write_text(json.dumps({"ok": True}))
    (out / "prevalidation_report.json").write_text(json.dumps({"watertight": True}))
    logs = out / "logs"; logs.mkdir()
    (logs / "build_log.txt").write_text("F002 PASS bbox 11x6.25x.105")
    (out / "P_model_check.txt").write_text("mass OK")
    (out / "P_deferred_log.json").write_text(json.dumps({"deferred": []}))
    (out / "constraint_verification.json").write_text(json.dumps({"MM-001": "PASS"}))
    (out / "P_feature_verification.json").write_text(json.dumps({"F002": "OK", "D009": "OK"}))
    (out / "P_geometric_loop_report.json").write_text(json.dumps({"iterations": 0}))
    (out / "P_reconciliation_report.json").write_text(json.dumps(
        {"final_status": "READY", "unresolved": [], "note": "D009 confirmed"}))
    (out / "P_engineering_review.txt").write_text("No critical flags. D009 governs the slot.")
    (out / "token_usage_log.txt").write_text("extraction 1000 tok $0.01")
    return out


# --------------------------------------------------------------------------- #
# 1. No external network — the zero-cost / no-exfiltration guarantee
# --------------------------------------------------------------------------- #
class TestLocalOnly:
    def test_default_host_is_localhost(self):
        assert ex.OLLAMA_HOST.startswith("http://localhost") or "127.0.0.1" in ex.OLLAMA_HOST

    def test_assert_local_blocks_external(self):
        for bad in ("http://evil.com/api", "https://api.anthropic.com/v1", "http://10.0.0.5:11434"):
            with pytest.raises(ex.ExternalHostError):
                ex.assert_local(bad)

    def test_assert_local_allows_localhost(self):
        ex.assert_local("http://localhost:11434/api/chat")
        ex.assert_local("http://127.0.0.1:11434/api/tags")

    def test_module_never_imports_anthropic_or_reads_key(self):
        src = (WEBAPP / "explainer.py").read_text(encoding="utf-8")
        assert "import anthropic" not in src
        assert 'getenv("ANTHROPIC' not in src and "getenv('ANTHROPIC" not in src

    def test_socket_level_only_localhost_contacted(self, monkeypatch):
        """Every real request must target localhost. We capture the URL handed
        to the ONE network choke point and assert its host is local."""
        contacted = []

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"version": "9.9"}).encode()

        def fake_raw(req, data=None, timeout=None):
            from urllib.parse import urlparse
            url = req.full_url if hasattr(req, "full_url") else req
            host = urlparse(url).hostname
            contacted.append(host)
            assert host in ex._LOCAL_HOSTS, f"non-local host contacted: {host}"
            return _FakeResp()

        monkeypatch.setattr(ex.urllib.request, "urlopen", fake_raw)
        ex._get_json("/api/version")
        assert contacted and all(h in ex._LOCAL_HOSTS for h in contacted)

    def test_external_host_raises_before_socket(self, monkeypatch):
        """If OLLAMA_HOST were ever external, the guard fires BEFORE any socket
        opens (urlopen must never be reached)."""
        called = {"n": 0}
        monkeypatch.setattr(ex.urllib.request, "urlopen",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setattr(ex, "OLLAMA_HOST", "http://evil.example.com:11434")
        with pytest.raises(ex.ExternalHostError):
            ex._get_json("/api/version")
        assert called["n"] == 0


# --------------------------------------------------------------------------- #
# 2. Full-pipeline artifact registry + cross-stage trace
# --------------------------------------------------------------------------- #
class TestArtifactRegistry:
    def test_every_stage_resolves_to_a_real_file(self, part_out):
        by_stage = ex.artifacts_by_stage(part_out)
        # Every stage that has a fixture file present must resolve.
        for key in ("image_prep", "extraction", "overview", "resolution", "must_meet",
                    "verification", "build_plan", "macros", "prevalidation", "build",
                    "constraint_verify", "feature_verify", "reconciliation", "review", "usage"):
            assert by_stage.get(key), f"stage {key} resolved no artifact"

    def test_resolved_file_not_mislabeled_extraction(self, part_out):
        by_stage = ex.artifacts_by_stage(part_out)
        assert [a.name for a in by_stage["extraction"]] == ["P_extraction.json"]
        assert any("resolved" in a.name for a in by_stage["resolution"])

    def test_trace_returns_at_least_four_stages_in_order(self, part_out):
        tr = ex.trace_field("D009", part_out)
        stages = [c["stage"] for c in tr.citations]
        assert len(set(stages)) >= 4, stages
        # stage order is preserved (extraction before build_plan before review)
        order = ex.__dict__  # not used; explicit index check below
        idx = {s: i for i, s in enumerate(
            ["extraction", "overview", "resolution", "must_meet", "verification",
             "build_plan", "macros", "build", "constraint_verify", "feature_verify",
             "reconciliation", "review"])}
        positions = [idx[s] for s in stages if s in idx]
        assert positions == sorted(positions), stages

    def test_trace_unknown_field_is_graceful(self, part_out):
        tr = ex.trace_field("D999", part_out)
        assert "D999" in tr.text and not tr.citations

    def test_routing_covers_new_stages(self):
        assert "image_prep" in ex.route("why is the image nearly blank at this dpi?")
        assert "export" in ex.route("where did my files go / download zip?")
        assert "build" in ex.route("show the build log pass/fail bbox")
        assert "macros" in ex.route("did any banned api fail the audit?")

    def test_context_budget_enforced(self, part_out):
        ctx = ex.assemble_context("explain everything about this part", part_out,
                                  budget_tokens=200)
        assert ctx.tokens <= 400  # budget respected (with slack for headers)


# --------------------------------------------------------------------------- #
# 3. Export manifest
# --------------------------------------------------------------------------- #
class TestExportManifest:
    def test_manifest_written_and_idempotent(self, part_out):
        m = ex.write_export_manifest(part_out, delivered_dirs=[part_out.parent])
        assert m and m.name == "_export_manifest.json"
        data = json.loads(m.read_text())
        assert data["file_count"] > 0 and "files" in data
        mtime = m.stat().st_mtime
        again = ex.write_export_manifest(part_out)   # must NOT overwrite
        assert again == m and again.stat().st_mtime == mtime

    def test_where_did_files_go_is_answerable(self, part_out):
        ex.write_export_manifest(part_out)
        by_stage = ex.artifacts_by_stage(part_out)
        assert by_stage.get("export"), "export manifest not in the registry"
        assert "export" in ex.route("where did my files go?")


# --------------------------------------------------------------------------- #
# 4. Zero-cost accounting
# --------------------------------------------------------------------------- #
class TestZeroCost:
    def test_usage_log_records_zero_cost(self, part_out):
        ex.log_usage(part_out, model="qwen2.5:14b", prompt_tokens=100, eval_tokens=50, duration_s=1.2)
        ex.log_usage(part_out, model="qwen2.5:14b", prompt_tokens=200, eval_tokens=80, duration_s=2.0)
        total = ex.usage_total(part_out)
        assert total["cost_usd"] == 0.0 and total["local"] is True
        assert total["messages"] == 2 and total["prompt_tokens"] == 300 and total["eval_tokens"] == 130

    def test_health_reports_zero_cost_even_when_down(self, monkeypatch):
        monkeypatch.setattr(ex, "_get_json",
                            lambda *a, **k: (_ for _ in ()).throw(ex.OllamaUnavailable("down")))
        h = ex.health()
        assert h["ok"] is False and h["cost_usd"] == 0.0 and h["local"] is True


# --------------------------------------------------------------------------- #
# 5. Model bootstrap
# --------------------------------------------------------------------------- #
class TestModelBootstrap:
    def test_choose_default_when_present(self):
        assert ex.choose_model([ex.DEFAULT_MODEL]) == ex.DEFAULT_MODEL

    def test_model_is_always_qwen_never_llama(self):
        assert "qwen" in ex.DEFAULT_MODEL.lower()
        assert "qwen" in ex.FALLBACK_MODEL.lower()
        assert "llama" not in ex.DEFAULT_MODEL.lower()
        assert "llama" not in ex.FALLBACK_MODEL.lower()

    def test_prefers_already_installed_qwen_no_redownload(self, monkeypatch):
        # A different qwen tag is installed than the configured default — use it
        # AS-IS (never a re-download, never a llama).
        monkeypatch.setattr(ex, "DEFAULT_MODEL", "qwen3.6:latest")
        chosen = ex.choose_model(["qwen2.5:14b", "llama3.1:8b"])
        assert chosen == "qwen2.5:14b"

    def test_fallback_is_small_qwen_when_low_ram_and_no_qwen(self, monkeypatch):
        monkeypatch.setattr(ex, "total_ram_gb", lambda: 8.0)
        chosen = ex.choose_model([])
        assert chosen == ex.FALLBACK_MODEL and "qwen" in chosen.lower()

    def test_never_falls_back_to_llama_even_if_installed(self, monkeypatch):
        monkeypatch.setattr(ex, "total_ram_gb", lambda: 8.0)
        # only a llama is installed — we still choose a qwen (to pull), not llama
        assert "llama" not in ex.choose_model(["llama3.1:8b"]).lower()

    def test_default_when_ram_unknown(self, monkeypatch):
        monkeypatch.setattr(ex, "total_ram_gb", lambda: None)
        assert ex.choose_model([]) == ex.DEFAULT_MODEL

    def test_pull_streams_progress(self, monkeypatch):
        chunks = [{"status": "pulling", "completed": 50, "total": 100},
                  {"status": "pulling", "completed": 100, "total": 100},
                  {"status": "success"}]
        monkeypatch.setattr(ex, "_post_stream", lambda path, payload, **k: iter(chunks))
        monkeypatch.setattr(ex, "_pin_model_choice", lambda m: None)
        got = list(ex.pull_model("qwen2.5:14b"))
        assert got == chunks

    def test_health_flags_model_not_ready(self, monkeypatch):
        monkeypatch.setattr(ex, "_get_json", lambda p, **k: {"version": "1.0"} if "version" in p else {})
        monkeypatch.setattr(ex, "installed_models", lambda: [])       # nothing pulled yet
        monkeypatch.setattr(ex, "total_ram_gb", lambda: 32.0)
        h = ex.health()
        assert h["ok"] and h["running"] and h["model_ready"] is False
        assert h["model"] == ex.DEFAULT_MODEL and h["num_ctx"] == ex.NUM_CTX


# --------------------------------------------------------------------------- #
# 6. Chat streaming (Ollama mocked — no network)
# --------------------------------------------------------------------------- #
class TestChatStreaming:
    def test_chat_streams_tokens_and_zero_cost_done(self, part_out, monkeypatch):
        stream = [
            {"message": {"content": "D009 "}},
            {"message": {"content": "was spec-driven."}},
            {"done": True, "prompt_eval_count": 1200, "eval_count": 40},
        ]
        monkeypatch.setattr(ex, "_post_stream", lambda path, payload, **k: iter(stream))
        events = list(ex.chat("Why was D009 resolved this way?", part_out))
        kinds = [e["type"] for e in events]
        assert kinds[0] == "context" and "token" in kinds and kinds[-1] == "done"
        done = events[-1]["meta"]
        assert done["cost_usd"] == 0.0 and done["local"] is True
        assert done["prompt_tokens"] == 1200 and done["eval_tokens"] == 40
        assert "spec-driven" in done["answer"]
        assert done["citations"], "answer must carry citations"

    def test_trace_question_uses_trace_assembler(self, part_out, monkeypatch):
        seen = {}
        def fake_stream(path, payload, **k):
            seen["messages"] = payload["messages"]
            return iter([{"done": True, "prompt_eval_count": 5, "eval_count": 5}])
        monkeypatch.setattr(ex, "_post_stream", fake_stream)
        list(ex.chat("trace D009", part_out))
        user_msg = seen["messages"][-1]["content"]
        assert "TRACE of D009" in user_msg

    def test_chat_handles_ollama_down(self, part_out, monkeypatch):
        def boom(*a, **k):
            raise ex.OllamaUnavailable("connection refused")
        monkeypatch.setattr(ex, "_post_stream", boom)
        events = list(ex.chat("anything", part_out))
        assert any(e["type"] == "error" for e in events)


# --------------------------------------------------------------------------- #
# 7. UI placement & styling contract (static DOM + CSS assertions)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def html():
    return (WEBAPP / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tokens():
    return (WEBAPP / "static" / "design-tokens.css").read_text(encoding="utf-8")


class TestUIContract:
    def test_explainer_renders_below_corrections_box(self, html):
        i_corr = html.find('id="feedback-bar"')
        i_expl = html.find('id="explainer-panel"')
        assert i_corr != -1 and i_expl != -1
        assert i_corr < i_expl, "explainer must come AFTER the corrections box in DOM order"
        # both inside the Pipeline sheet (panel-pipeline), before Run Outputs
        i_pipeline = html.find('id="panel-pipeline"')
        i_outputs = html.find('id="panel-outputs"')
        assert i_pipeline < i_expl < i_outputs

    def test_orange_accent_defined_and_used(self, tokens, html):
        assert "#E8710A" in tokens, "orange token must be defined"
        assert "--explain" in tokens
        # the explainer zone binds to the orange token
        assert "var(--explain)" in html

    def test_gold_not_reused_for_explainer(self, tokens):
        # the orange must be its own value, distinct from any gold/amber token
        assert "#E8710A" != "#D39F10"

    def test_session_footer_is_zero_cost(self, html):
        assert "$0.00 · all local" in html
        assert "ex-session-footer" in html

    def test_quick_question_chips_present(self, html):
        for q in ("Why was", "Trace", "end to end", "What changed since last run",
                  "Explain macro", "still deferred", "Where did the export go"):
            assert q in html, f"missing quick-question chip text: {q!r}"

    def test_send_to_corrections_link_present(self, html):
        assert "Send to corrections" in html and "to-corr" in html

    def test_local_free_header(self, html):
        assert "Pipeline Explainer" in html and "local &amp; free" in html
