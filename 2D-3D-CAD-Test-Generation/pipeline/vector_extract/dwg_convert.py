"""DWG → DXF conversion WITHOUT requiring the ODA File Converter.

Engine chain, first success wins:

1. **ezdwg** (pip) — pure Rust/Python read-only DWG parser; handles AC1014
   (R14) through AC1032 (2018). No external install.
2. **SolidWorks translator** (COM) — SolidWorks imports DWG natively,
   including ancient R13 (AC1012) files, and exports DXF. Runs in a
   SUBPROCESS so COM apartment threading never leaks into the caller
   (FastAPI worker threads / pipeline).
3. **ODA File Converter** — used only if it happens to be installed.

Every attempt is recorded in ``notes`` so a failed conversion tells the user
exactly what was tried and why it failed — never a bare error.

Also runnable as a script (used for the SolidWorks subprocess hop):
    python dwg_convert.py --solidworks <src.dwg> <dst.dxf>
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_SW_TIMEOUT_S = 180  # first SolidWorks launch can take a while


def detect_dwg_version(path: Path) -> str:
    """The 6-byte DWG version tag (e.g. 'AC1012' = R13, 'AC1032' = 2018)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(6).decode("ascii", "replace")
    except OSError:
        return "??????"


def _try_ezdwg(src: Path, dst: Path, notes: list[str]) -> bool:
    try:
        import ezdwg
    except ImportError:
        notes.append("ezdwg not installed (pip install ezdwg).")
        return False
    try:
        ezdwg.to_dxf(str(src), str(dst))
        if dst.is_file() and dst.stat().st_size > 0:
            return True
        notes.append("ezdwg produced no output.")
    except Exception as e:
        notes.append(f"ezdwg: {e}")
    return False


def _try_solidworks_subprocess(src: Path, dst: Path, notes: list[str]) -> bool:
    """Convert via the SolidWorks DWG translator, isolated in a subprocess."""
    try:
        import win32com.client  # noqa: F401 — availability check only
    except ImportError:
        notes.append("SolidWorks translator unavailable (pywin32 not installed).")
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--solidworks",
             str(src), str(dst)],
            capture_output=True, text=True, timeout=_SW_TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return True
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        notes.append("SolidWorks translator failed: "
                     + (detail[-1] if detail else f"exit {proc.returncode}"))
    except subprocess.TimeoutExpired:
        notes.append(f"SolidWorks translator timed out after {_SW_TIMEOUT_S}s.")
    except Exception as e:
        notes.append(f"SolidWorks translator: {type(e).__name__}: {e}")
    return False


def _solidworks_convert(src: Path, dst: Path) -> None:
    """Runs INSIDE the subprocess: DWG → SolidWorks drawing import → DXF."""
    import pythoncom
    import win32com.client
    from win32com.client import VARIANT

    pythoncom.CoInitialize()
    sw = win32com.client.Dispatch("SldWorks.Application")
    import_data = sw.GetImportFileData(str(src))
    errs = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.LoadFile4(str(src), "r", import_data, errs)
    if doc is None:
        raise RuntimeError(f"SolidWorks could not import the DWG (err={errs.value}).")
    try:
        doc.SaveAs3(str(dst), 0, 0)
    finally:
        sw.CloseDoc(doc.GetTitle)
    if not dst.is_file():
        raise RuntimeError("SolidWorks produced no DXF output.")


def _try_oda(src: Path, dst: Path, notes: list[str]) -> bool:
    import glob as _glob
    import shutil
    import tempfile

    exe = shutil.which("ODAFileConverter")
    if not exe:
        for pattern in (r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
                        r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe"):
            hits = sorted(_glob.glob(pattern))
            if hits:
                exe = hits[-1]
                break
    if not exe:
        notes.append("ODA File Converter not installed (optional).")
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            work = tdir / src.name
            work.write_bytes(src.read_bytes())
            out_dir = tdir / "out"
            out_dir.mkdir()
            subprocess.run([exe, str(tdir), str(out_dir), "ACAD2018", "DXF", "0", "1",
                            src.name], capture_output=True, timeout=120)
            hits = list(out_dir.glob("*.dxf"))
            if hits:
                dst.write_bytes(hits[0].read_bytes())
                return True
        notes.append("ODA File Converter produced no DXF.")
    except Exception as e:
        notes.append(f"ODA File Converter: {type(e).__name__}: {e}")
    return False


def dwg_to_dxf(src: str | Path, dst: str | Path, notes: list[str] | None = None) -> str | None:
    """Convert ``src`` (.dwg) to ``dst`` (.dxf). Returns the engine name that
    succeeded ('ezdwg' | 'solidworks' | 'oda') or None, with the reason for
    every failed attempt appended to ``notes``."""
    src, dst = Path(src), Path(dst)
    if notes is None:
        notes = []
    version = detect_dwg_version(src)
    notes.append(f"DWG version tag: {version}.")
    if _try_ezdwg(src, dst, notes):
        return "ezdwg"
    if _try_solidworks_subprocess(src, dst, notes):
        return "solidworks"
    if _try_oda(src, dst, notes):
        return "oda"
    return None


if __name__ == "__main__":  # subprocess entry: --solidworks <src> <dst>
    if len(sys.argv) == 4 and sys.argv[1] == "--solidworks":
        try:
            _solidworks_convert(Path(sys.argv[2]), Path(sys.argv[3]))
            sys.exit(0)
        except Exception as e:  # message becomes the caller's note
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
    print("usage: dwg_convert.py --solidworks <src.dwg> <dst.dxf>", file=sys.stderr)
    sys.exit(2)
