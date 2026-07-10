"""Tests for Phase D — methods_config dispatch + construction_experiment harness."""
import os
from pathlib import Path

import pytest

from pipeline.methods_config import _DEFAULTS, is_known, load_methods, method_for


class TestMethodsConfig:
    def test_defaults(self):
        assert method_for("hole") == "sketch_circle_cut"
        assert method_for("slot") == "slot2d"
        assert method_for("cut") == "sketch_rect_cut"

    def test_unknown_class_safe(self):
        assert method_for("nonexistent") == ""  # never raises

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MTI_METHOD_SLOT", "create_sketch_slot")
        assert method_for("slot") == "create_sketch_slot"

    def test_hole_wizard_optin_wins(self, monkeypatch):
        monkeypatch.setenv("MTI_ENABLE_HOLE_WIZARD", "1")
        assert method_for("hole") == "hole_wizard5"

    def test_json_override(self, tmp_path, monkeypatch):
        # Point the config loader at a temp methods.json.
        cfg = tmp_path / "methods.json"
        cfg.write_text('{"methods": {"cut": "sketch_rect_cut_v2"}}')
        monkeypatch.setattr("pipeline.methods_config._config_path", lambda: cfg)
        assert load_methods()["cut"] == "sketch_rect_cut_v2"
        assert load_methods()["hole"] == "sketch_circle_cut"  # unchanged default

    def test_is_known(self):
        assert is_known("hole", "sketch_circle_cut")
        assert is_known("slot", "slot2d")
        assert not is_known("hole", "made_up")


class TestConstructionExperiment:
    def test_hole_experiment_picks_sketch_circle(self, tmp_path):
        pytest.importorskip("cadquery")
        pytest.importorskip("scipy")
        from pipeline.construction_experiment import run_hole_experiment

        res = run_hole_experiment(tmp_path / "hole")
        assert res.winner == "sketch_circle_cut"
        assert any(t.built and t.verified_ok for t in res.trials)

    def test_slot_experiment_picks_slot2d(self, tmp_path):
        pytest.importorskip("cadquery")
        pytest.importorskip("scipy")
        from pipeline.construction_experiment import run_slot_experiment

        res = run_slot_experiment(tmp_path / "slot")
        assert res.winner == "slot2d"

    def test_record_to_methods_json_no_winner_is_noop(self, tmp_path):
        from pipeline.construction_experiment import ExperimentResult, record_to_methods_json

        r = ExperimentResult("hole")  # no trials -> no winner
        r.decide()
        assert record_to_methods_json(r, config_dir=tmp_path) is None

    def test_record_to_methods_json_writes_winner(self, tmp_path):
        from pipeline.construction_experiment import ExperimentResult, MethodTrial, record_to_methods_json

        r = ExperimentResult("slot")
        r.trials.append(MethodTrial("slot2d", "cadquery", built=True, verified_ok=True))
        r.decide()
        p = record_to_methods_json(r, config_dir=tmp_path)
        assert p and p.is_file()
        import json
        data = json.loads(p.read_text())
        assert data["methods"]["slot"] == "slot2d"
