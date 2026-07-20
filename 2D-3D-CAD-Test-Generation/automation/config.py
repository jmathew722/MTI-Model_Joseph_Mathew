"""Feature-flag configuration for the build-executor A/B switch.

``BUILD_EXECUTOR_MODE`` selects which path turns a resolved extraction into a
built ``.sldprt``:

  * ``vba``     — (default) the existing, proven path: generate the VBA macro
                  package (``pipeline/macro_generator.py``) and COM-build the
                  ``.sldprt`` via ``pipeline/solidworks_builder.py``. Unchanged.
  * ``pywin32`` — the new experimental path: run the SAME build plan through
                  ``automation.build_executor``, which drives SolidWorks through
                  the context-managed :class:`automation.com_client.SolidWorksSession`
                  and emits a structured build report for parity comparison.

The default is ``vba`` so nothing changes until a run explicitly opts in.
"""
from __future__ import annotations

import os

MODE_VBA = "vba"
MODE_PYWIN32 = "pywin32"
_VALID_MODES = (MODE_VBA, MODE_PYWIN32)

ENV_VAR = "BUILD_EXECUTOR_MODE"


def build_executor_mode(default: str = MODE_VBA) -> str:
    """Return the active build-executor mode from ``$BUILD_EXECUTOR_MODE``.

    Unknown/empty values fall back to ``default`` (``vba``) — the flag must never
    be a way to silently break a build.
    """
    raw = (os.getenv(ENV_VAR) or "").strip().lower()
    if raw in _VALID_MODES:
        return raw
    return default


def is_pywin32_mode() -> bool:
    """True when the experimental pywin32 build path is selected."""
    return build_executor_mode() == MODE_PYWIN32
