"""Tests for pipeline.vector_extract.dwg_convert — the no-ODA DWG engine chain."""
from pathlib import Path

from pipeline.vector_extract import dwg_convert as dc


def _fake_dwg(tmp_path, version=b"AC1012") -> Path:
    p = tmp_path / "part.dwg"
    p.write_bytes(version + b"\x00" * 100)
    return p


class TestVersionDetect:
    def test_r13_tag(self, tmp_path):
        assert dc.detect_dwg_version(_fake_dwg(tmp_path, b"AC1012")) == "AC1012"

    def test_2018_tag(self, tmp_path):
        assert dc.detect_dwg_version(_fake_dwg(tmp_path, b"AC1032")) == "AC1032"

    def test_missing_file(self, tmp_path):
        assert dc.detect_dwg_version(tmp_path / "nope.dwg") == "??????"


class TestEngineChain:
    def test_first_engine_wins(self, tmp_path, monkeypatch):
        def ok(src, dst, notes):
            Path(dst).write_text("dxf")
            return True

        monkeypatch.setattr(dc, "_try_ezdwg", ok)
        monkeypatch.setattr(dc, "_try_solidworks_subprocess",
                            lambda *a: (_ for _ in ()).throw(AssertionError("must not run")))
        notes: list[str] = []
        engine = dc.dwg_to_dxf(_fake_dwg(tmp_path), tmp_path / "o.dxf", notes)
        assert engine == "ezdwg"

    def test_falls_through_to_solidworks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dc, "_try_ezdwg",
                            lambda s, d, n: n.append("ezdwg: unsupported") or False)

        def sw_ok(src, dst, notes):
            Path(dst).write_text("dxf")
            return True

        monkeypatch.setattr(dc, "_try_solidworks_subprocess", sw_ok)
        notes: list[str] = []
        engine = dc.dwg_to_dxf(_fake_dwg(tmp_path), tmp_path / "o.dxf", notes)
        assert engine == "solidworks"
        assert any("unsupported" in n for n in notes)

    def test_all_engines_fail_returns_none_with_reasons(self, tmp_path, monkeypatch):
        for fn in ("_try_ezdwg", "_try_solidworks_subprocess", "_try_oda"):
            monkeypatch.setattr(dc, fn,
                                lambda s, d, n, _fn=fn: n.append(f"{_fn} failed") or False)
        notes: list[str] = []
        assert dc.dwg_to_dxf(_fake_dwg(tmp_path), tmp_path / "o.dxf", notes) is None
        assert sum("failed" in n for n in notes) == 3
        assert any("AC1012" in n for n in notes)  # version always reported

    def test_ezdwg_rejects_r13_honestly(self, tmp_path):
        # Real ezdwg call on a fake R13 header: must fail with a note, not crash.
        notes: list[str] = []
        ok = dc._try_ezdwg(_fake_dwg(tmp_path, b"AC1012"), tmp_path / "o.dxf", notes)
        assert ok is False
        assert notes, "failure must be explained in notes"


class TestDxfHolesUsesChain:
    def test_dwg_fallback_notes_mention_engines(self, tmp_path, monkeypatch):
        from pipeline.vector_extract import dxf_holes

        monkeypatch.setattr(dc, "dwg_to_dxf", lambda s, d, n: n.append("all failed") or None)
        geom = dxf_holes.extract_dxf_geometry(_fake_dwg(tmp_path))
        assert geom.is_raster
        assert any("engine" in n or "FALLBACK" in n for n in geom.notes)
