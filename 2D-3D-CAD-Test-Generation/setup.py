"""Environment checker / installer for the 2D->3D SolidWorks pipeline.

Run:  python setup.py

Checks Python version, installs required packages, verifies the pywin32
post-install hook (Windows only), and creates a .env from the template. Prints a
clear PASS / FAIL / SKIP for each check. On non-Windows machines the SolidWorks
and pywin32 checks are SKIPPED (the extraction/validation half still works).
"""
from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
HERE = Path(__file__).resolve().parent

# (import_name, pip_spec, windows_only)
REQUIREMENTS = [
    ("anthropic", "anthropic>=0.69.0", False),
    ("PIL", "pillow>=10.0.0", False),
    ("pdf2image", "pdf2image>=1.16.0", False),
    ("fitz", "PyMuPDF>=1.24.0", False),
    ("numpy", "numpy>=1.24.0", False),
    ("pydantic", "pydantic>=2.0.0", False),
    ("dotenv", "python-dotenv>=1.0.0", False),
    ("rich", "rich>=13.0.0", False),
    ("pytest", "pytest>=7.0.0", False),
    ("trimesh", "trimesh>=4.0.0", False),
    # scipy is required by trimesh's cross-section measurement used in the
    # post-build must-meet verification (it is NOT in trimesh's base install).
    ("scipy", "scipy>=1.10.0", False),
    # DWG/DXF intake: DXF renders directly (ezdxf); DWG converts via the engine
    # chain whose first, always-available link is the ezdwg pip package.
    ("ezdxf", "ezdxf>=1.4.0", False),
    ("ezdwg", "ezdwg>=0.9.0", False),
    ("win32com", "pywin32>=306", True),
]

# Python version window. Lower bound: pipeline/UI features. Upper bound is a
# HARD ceiling: the CadQuery pre-validation stage pulls in numba, which has no
# wheel for Python 3.13+, so the dependency install fails there. Keep in sync
# with webapp/run.ps1.
PY_MIN = (3, 10)
PY_MAX_EXCL = (3, 13)

# ANSI fallbacks if rich is not yet installed.
GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def _result(label: str, status: str, detail: str = "") -> bool:
    color = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}.get(status, "")
    print(f"  [{color}{status}{RESET}] {label}" + (f" — {detail}" if detail else ""))
    return status != "FAIL"


def check_python() -> bool:
    ver = sys.version_info[:2]
    ok = PY_MIN <= ver < PY_MAX_EXCL
    label = f"Python >={PY_MIN[0]}.{PY_MIN[1]},<{PY_MAX_EXCL[0]}.{PY_MAX_EXCL[1]}"
    if ok:
        detail = f"found {platform.python_version()}"
    elif ver >= PY_MAX_EXCL:
        detail = (
            f"found {platform.python_version()} - too new; numba/cadquery have no "
            f"wheel for {PY_MAX_EXCL[0]}.{PY_MAX_EXCL[1]}+. Install Python 3.12 "
            "(winget install Python.Python.3.12)."
        )
    else:
        detail = f"found {platform.python_version()} - too old"
    return _result(label, "PASS" if ok else "FAIL", detail)


def _pip_install(spec: str) -> bool:
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", spec],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def check_and_install_packages(do_install: bool = True) -> bool:
    all_ok = True
    for import_name, pip_spec, windows_only in REQUIREMENTS:
        if windows_only and not IS_WINDOWS:
            _result(f"{pip_spec}", "SKIP", "Windows-only")
            continue
        try:
            importlib.import_module(import_name)
            _result(pip_spec, "PASS", "already installed")
        except ImportError:
            if not do_install:
                _result(pip_spec, "FAIL", "not installed")
                all_ok = False
                continue
            print(f"  installing {pip_spec} ...")
            if _pip_install(pip_spec):
                try:
                    importlib.invalidate_caches()
                    importlib.import_module(import_name)
                    _result(pip_spec, "PASS", "installed")
                except ImportError:
                    _result(pip_spec, "FAIL", "installed but not importable")
                    all_ok = False
            else:
                _result(pip_spec, "FAIL", "pip install failed")
                all_ok = False
    return all_ok


def check_pywin32_makepy() -> bool:
    """Verify the pywin32 makepy hook works (generates COM type-library bindings)."""
    if not IS_WINDOWS:
        return _result("pywin32 makepy hook", "SKIP", "Windows-only")
    try:
        # Running makepy with no args lists available type libraries — proves the
        # hook is functional without requiring SolidWorks to be installed.
        subprocess.check_call(
            [sys.executable, "-m", "win32com.client.makepy", "-i"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        return _result("pywin32 makepy hook", "PASS")
    except Exception as e:
        return _result("pywin32 makepy hook", "FAIL", str(e))


def check_poppler() -> bool:
    """PDF->image via pdf2image needs poppler; PyMuPDF is the fallback if absent."""
    if shutil.which("pdftoppm"):
        return _result("poppler (pdf2image backend)", "PASS")
    return _result(
        "poppler (pdf2image backend)",
        "SKIP",
        "not found — PDFs will use the PyMuPDF fallback",
    )


def ensure_env_file() -> bool:
    env = HERE / ".env"
    template = HERE / ".env.template"
    if env.exists():
        return _result(".env file", "PASS", "exists")
    if template.exists():
        shutil.copy(template, env)
        return _result(".env file", "PASS", "created from .env.template — add your API key")
    return _result(".env file", "FAIL", "no .env.template to copy from")


def main() -> int:
    print("=" * 60)
    print(" 2D -> 3D SolidWorks Pipeline — environment setup")
    print(f" Platform: {sys.platform} | Python: {platform.python_version()}")
    print("=" * 60)

    results = []
    print("\nPython version:")
    results.append(check_python())

    print("\nPackages:")
    results.append(check_and_install_packages(do_install=True))

    print("\nWindows COM (pywin32):")
    results.append(check_pywin32_makepy())

    print("\nPDF backend:")
    check_poppler()  # informational; not a hard failure

    print("\nConfiguration:")
    results.append(ensure_env_file())

    print("\n" + "=" * 60)
    if all(results):
        print(f"{GREEN}All required checks passed.{RESET}")
        if not IS_WINDOWS:
            print(
                f"{YELLOW}Note:{RESET} SolidWorks/pywin32 steps were skipped (non-Windows). "
                "Use --validate-only to run the extraction pipeline here."
            )
        print("Next: edit .env and set ANTHROPIC_API_KEY, then run main.py.")
        return 0
    print(f"{RED}Some required checks FAILED — see above.{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
