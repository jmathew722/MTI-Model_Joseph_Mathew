"""Non-live tests for the pywin32 build-executor subsystem.

These exercise the pure/structural surfaces (config flag, structured error,
build report, bbox diff) — no SolidWorks connection required, so they run on any
OS. The live geometry tests are in ``test_build_executor_live.py`` (gated on
``SOLIDWORKS_LIVE_TEST=1``).
"""
import json

import pytest

from automation import config
from automation.build_executor import BuildReport, OperationOutcome
from automation.com_client import SolidWorksComError
from automation.compare import _bbox_close


# -- config flag ---------------------------------------------------------- #
def test_mode_defaults_to_vba(monkeypatch):
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    assert config.build_executor_mode() == config.MODE_VBA
    assert config.is_pywin32_mode() is False


def test_mode_pywin32_when_set(monkeypatch):
    monkeypatch.setenv(config.ENV_VAR, "pywin32")
    assert config.build_executor_mode() == config.MODE_PYWIN32
    assert config.is_pywin32_mode() is True


def test_mode_unknown_value_falls_back_to_vba(monkeypatch):
    monkeypatch.setenv(config.ENV_VAR, "banana")
    assert config.build_executor_mode() == config.MODE_VBA


def test_mode_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(config.ENV_VAR, "  PyWin32 ")
    assert config.build_executor_mode() == config.MODE_PYWIN32


# -- structured error payload --------------------------------------------- #
def test_com_error_payload_schema():
    err = SolidWorksComError("FeatureCut4", "returned Nothing",
                             args=(True, False, 0.5), hresult=-2147352571,
                             feature_id="F003")
    lesson = err.as_lesson()
    assert lesson["method"] == "FeatureCut4"
    assert lesson["feature_id"] == "F003"
    assert lesson["hresult"] == "0x80020005"
    assert lesson["message"] == "returned Nothing"
    assert lesson["kind"] == "com_error"
    assert lesson["args"] == [True, False, 0.5]
    # JSON-serialisable (append_lesson writes it verbatim).
    json.dumps(lesson)


def test_com_error_without_hresult():
    err = SolidWorksComError("InsertSketch", "no active sketch")
    assert err.as_lesson()["hresult"] is None
    assert "InsertSketch" in str(err)


# -- build report --------------------------------------------------------- #
def test_build_report_roundtrip(tmp_path):
    report = BuildReport(part="158-C")
    report.operations.append(OperationOutcome(op="extrude", feature_id="F001"))
    report.operations.append(
        OperationOutcome(op="fillet", feature_id="F009", status="FAIL",
                         critical=False, detail="edge not found"))
    report.ok = True
    report.feature_tree_count = 7
    report.bbox_m = [0.0, 0.0, 0.0, 0.1, 0.05, 0.02]

    d = report.as_dict()
    assert d["part"] == "158-C"
    assert d["mode"] == config.MODE_PYWIN32
    assert len(d["operations"]) == 2
    assert d["operations"][1]["status"] == "FAIL"

    path = report.write(tmp_path, "158-C")
    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["feature_tree_count"] == 7


# -- bbox parity helper --------------------------------------------------- #
def test_bbox_close_within_tolerance():
    a = [0.0, 0.0, 0.0, 0.1000, 0.05, 0.02]
    b = [0.0, 0.0, 0.0, 0.1002, 0.05, 0.02]  # 0.2 mm off, under 0.5 mm tol
    assert _bbox_close(a, b) is True


def test_bbox_close_outside_tolerance():
    a = [0.0, 0.0, 0.0, 0.100, 0.05, 0.02]
    b = [0.0, 0.0, 0.0, 0.110, 0.05, 0.02]  # 10 mm off
    assert _bbox_close(a, b) is False


def test_bbox_close_none_or_mismatched():
    assert _bbox_close(None, [1, 2, 3]) is False
    assert _bbox_close([1, 2, 3], [1, 2]) is False


# -- import cleanliness (no SolidWorks / Windows required) ---------------- #
def test_subsystem_imports_cleanly():
    import automation  # noqa: F401
    import automation.build_executor  # noqa: F401
    import automation.compare  # noqa: F401
    import automation.marshalling  # noqa: F401
