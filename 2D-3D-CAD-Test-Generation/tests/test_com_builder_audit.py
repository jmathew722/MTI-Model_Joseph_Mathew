"""Static-audit tests for the COM builder source (mirror of the macro_audit tests).

Runs with the normal suite (`python -m pytest tests/`) — there is no separate CI
workflow in this repo, so the pytest run IS the gate. If a future edit reintroduces
a banned/invented API or an inline VARIANT that bypasses com_marshal, this fails.
"""
from pipeline import com_builder_audit as cba


def test_installed_builder_source_is_clean():
    report = cba.audit_builder()
    assert report.ok, "solidworks_builder.py has audit errors: " + "; ".join(
        f.message for f in report.errors)
    assert report.to_dict()["error_count"] == 0


def test_detects_invented_getmodelboundingbox():
    report = cba.audit_source("box = sw_doc.GetModelBoundingBox()\n")
    assert not report.ok
    assert any(f.rule_id == "E004" for f in report.errors)


def test_detects_sketch_reselect_by_name():
    src = 'ok = sw_doc.Extension.SelectByID2(sketchName, "SKETCH", 0, 0, 0, False, 0, None, 0)\n'
    report = cba.audit_source(src)
    assert any(f.rule_id == "E006" for f in report.errors)


def test_detects_inline_variant_bypass():
    report = cba.audit_source("v = VARIANT(pythoncom.VT_DISPATCH, None)\n")
    assert any(f.rule_id == "E-CENTRALIZE" for f in report.errors)


def test_comment_lines_are_ignored():
    # A banned name in a COMMENT is not a violation (it's documentation).
    report = cba.audit_source("# never call GetModelBoundingBox here\n")
    assert report.ok
