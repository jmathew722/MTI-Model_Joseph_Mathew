"""Context-managed SolidWorks COM connection with structured error reporting.

WINDOWS ONLY at call time. ``win32com``/``pythoncom`` are imported lazily inside
functions so this module imports on any OS; the Windows-only paths raise a clear
error only when actually invoked.

Design notes specific to SolidWorks + pywin32
--------------------------------------------
The requesting spec asked for *early-bound* dispatch via
``win32com.client.gencache.EnsureDispatch("SldWorks.Application")``. In practice
the ``SldWorks.Application`` IDispatch does **not** implement ``GetTypeInfo()``,
so ``EnsureDispatch`` (which calls it) fails with *"This COM object can not
automate the makepy process"* — this is documented at length in
``pipeline/solidworks_builder.py`` and is why the proven builder is late-bound.

:class:`SolidWorksSession` therefore does the right thing rather than the literal
thing: it (1) makes a best-effort attempt to warm the early-bound *type-library*
cache (``gencache``) for the SOLIDWORKS **constants** type library — which is what
early binding actually buys us (named enums + generated method signatures) — and
(2) connects to the application object with late-bound ``Dispatch`` when early
binding on the app object is refused. The generated constants are loaded either
way, so ``win32com.client.constants`` is populated exactly as the geometry engine
expects. The stale-cache rebuild requested in the spec is honoured against the
constants type library (the only thing gencache can generate for SolidWorks).
"""
from __future__ import annotations

import sys
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

# SOLIDWORKS Constant type library CLSID (swconst). Mirrors
# pipeline.solidworks_builder so both COM paths resolve the same enums.
_SW_CONST_TYPELIB_CLSID = "{4687F359-55D0-4CD3-B6CF-2EB42C11F989}"
_SW_CONST_FALLBACK_VERSIONS = (33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18)


class PlatformError(RuntimeError):
    """Raised when the COM automation layer is used off Windows."""


class SolidWorksComError(RuntimeError):
    """A COM call failed. Carries a structured payload for the lessons ledger.

    The payload schema (``method``, ``args``, ``hresult``, ``message``) matches
    what :func:`pipeline.must_meet.append_lesson` accepts — a free-form dict to
    which it adds a ``timestamp`` — so a failure can be appended to
    ``lessons_learned.jsonl`` verbatim via :meth:`as_lesson`.
    """

    def __init__(
        self,
        method: str,
        message: str,
        *,
        args: Optional[tuple] = None,
        hresult: Optional[int] = None,
        feature_id: str = "",
    ):
        self.method = method
        self.method_args = tuple(args or ())
        self.hresult = hresult
        self.feature_id = feature_id
        self.message = message
        hres = f" (HRESULT 0x{hresult & 0xFFFFFFFF:08X})" if isinstance(hresult, int) else ""
        super().__init__(f"{method}{hres}: {message}")

    def as_lesson(self) -> dict:
        """Structured record for ``lessons_learned.jsonl`` (append_lesson-ready)."""
        return {
            "source": "pywin32_build_executor",
            "kind": "com_error",
            "method": self.method,
            "args": [_reprish(a) for a in self.method_args],
            "hresult": (f"0x{self.hresult & 0xFFFFFFFF:08X}"
                        if isinstance(self.hresult, int) else None),
            "feature_id": self.feature_id or None,
            "message": self.message,
        }


def _reprish(value: Any) -> Any:
    """JSON-safe representation of a COM arg (COM objects stringify)."""
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_reprish(v) for v in value]
    return repr(value)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise PlatformError(
            "The pywin32 build executor requires Windows with SolidWorks 2024. "
            f"Current platform: {sys.platform!r}."
        )


def _warm_constants_cache(revision: Optional[str]) -> Optional[int]:
    """Warm/rebuild the gencache type-library cache for the SW constants.

    Deletes a stale ``gen_py`` cache when the SolidWorks revision changed since it
    was last built (the spec's stale-cache-rebuild requirement), then generates
    the constants module so ``win32com.client.constants`` is populated. NON-FATAL:
    a miss only means named-constant lookups fall back to literal defaults.
    """
    try:
        import win32com.client  # type: ignore
        from win32com.client import gencache  # type: ignore
    except Exception as e:  # pragma: no cover - only on a broken pywin32
        log.warning("pywin32 unavailable for gencache warm-up: %s", e)
        return None

    # Rebuild the cache if the SolidWorks revision differs from what we recorded.
    try:
        import os
        import tempfile

        marker = os.path.join(tempfile.gettempdir(), "mti_sw_gencache_revision.txt")
        prev = None
        if os.path.exists(marker):
            with open(marker, "r", encoding="utf-8") as f:
                prev = f.read().strip()
        if revision and prev and prev != revision:
            log.info("SolidWorks revision changed (%s -> %s); rebuilding gen_py cache.",
                     prev, revision)
            try:
                gencache.Rebuild()
            except Exception as e:
                log.warning("gencache.Rebuild failed (continuing): %s", e)
        if revision:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(revision))
    except Exception as e:
        log.warning("gencache revision check skipped: %s", e)

    for major in _SW_CONST_FALLBACK_VERSIONS:
        try:
            gencache.EnsureModule(_SW_CONST_TYPELIB_CLSID, 0, major, 0)
            log.info("Loaded SolidWorks constant type library v%d.0 (early-bound enums).", major)
            return major
        except Exception:
            continue
    log.warning("Could not load any SolidWorks constant type library (constants fall "
                "back to literal defaults).")
    return None


class SolidWorksSession:
    """Context manager around a live SolidWorks application object.

    Usage::

        with SolidWorksSession() as sw:
            doc = sw.new_document(template_path)
            ...

    On ``__enter__`` it connects to a running SolidWorks (or launches one) and
    exposes :attr:`app` and :attr:`math_utility`. On ``__exit__`` it **does not
    close SolidWorks** — the application stays open across builds; only the COM
    reference held here is released and COM is uninitialised for the thread.
    """

    def __init__(self, *, launch_if_absent: bool = True, visible: bool = True):
        self.launch_if_absent = launch_if_absent
        self.visible = visible
        self.app = None
        self.math_utility = None
        self._co_initialised = False
        self._constants_version: Optional[int] = None

    # -- lifecycle --------------------------------------------------------- #
    def __enter__(self) -> "SolidWorksSession":
        _require_windows()
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom.CoInitialize()
        self._co_initialised = True

        try:
            active = win32com.client.GetActiveObject("SldWorks.Application")
            self.app = win32com.client.Dispatch(active)
            log.info("Connected to existing SolidWorks instance.")
        except Exception:
            if not self.launch_if_absent:
                raise SolidWorksComError(
                    "GetActiveObject", "No running SolidWorks instance and "
                    "launch_if_absent=False.")
            try:
                self.app = win32com.client.Dispatch("SldWorks.Application")
                self.app.Visible = self.visible
                log.info("Launched a new SolidWorks instance.")
            except Exception as e:
                raise SolidWorksComError(
                    "Dispatch(SldWorks.Application)",
                    f"failed to connect to or launch SolidWorks: {e}",
                    hresult=_hresult_of(e)) from e

        if self.app is None:
            raise SolidWorksComError("Dispatch(SldWorks.Application)",
                                     "obtained a null application object.")

        revision = None
        try:
            revision = str(self.app.RevisionNumber)
            log.info("SolidWorks revision: %s", revision)
        except Exception:
            log.warning("Could not read SolidWorks revision number (continuing).")
        self._constants_version = _warm_constants_cache(revision)

        # MathUtility is the SW factory for MathPoint/MathVector — used by the
        # marshalling helpers to build native geometry objects.
        try:
            self.math_utility = self.app.GetMathUtility()
        except Exception as e:
            log.warning("GetMathUtility failed (continuing without it): %s", e)
            self.math_utility = None
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Deliberately DO NOT call self.app.ExitApp()/CloseAllDocuments — the app
        # stays open across builds. Only release our reference + uninit COM.
        self.app = None
        self.math_utility = None
        if self._co_initialised:
            try:
                import pythoncom  # type: ignore

                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._co_initialised = False
        return False  # never suppress exceptions

    # -- convenience ------------------------------------------------------- #
    @property
    def active_doc(self):
        """The active SolidWorks document (or ``None``)."""
        if self.app is None:
            return None
        try:
            return self.app.ActiveDoc
        except Exception:
            return None

    def _call(self, obj, method: str, *args, feature_id: str = ""):
        """Invoke ``obj.method(*args)``, converting COM failures to structured errors.

        Every public build method routes its COM calls through here so a failure
        is logged with the HRESULT + method + args and re-raised as
        :class:`SolidWorksComError`.
        """
        try:
            import pywintypes  # type: ignore

            com_error = pywintypes.com_error
        except Exception:  # pragma: no cover
            com_error = Exception
        try:
            fn = getattr(obj, method)
            return fn(*args)
        except com_error as e:  # type: ignore[misc]
            hres = _hresult_of(e)
            log.error("COM call %s%s failed (HRESULT 0x%08X): %s",
                      method, _short_args(args), (hres or 0) & 0xFFFFFFFF, e)
            raise SolidWorksComError(method, str(e), args=args, hresult=hres,
                                     feature_id=feature_id) from e
        except Exception as e:
            log.error("COM call %s%s raised: %s", method, _short_args(args), e)
            raise SolidWorksComError(method, str(e), args=args,
                                     feature_id=feature_id) from e

    def new_document(self, template_path: Optional[str] = None):
        """Create a new part document from ``template_path`` (or the SW default)."""
        from pipeline.solidworks_builder import create_new_part

        return create_new_part(self.app, template_path)


def _hresult_of(exc: Exception) -> Optional[int]:
    """Extract the HRESULT from a ``pywintypes.com_error`` when present."""
    args = getattr(exc, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    hres = getattr(exc, "hresult", None)
    return hres if isinstance(hres, int) else None


def _short_args(args: tuple, limit: int = 6) -> str:
    shown = ", ".join(_reprish(a).__str__()[:24] for a in args[:limit])
    if len(args) > limit:
        shown += ", …"
    return f"({shown})"
