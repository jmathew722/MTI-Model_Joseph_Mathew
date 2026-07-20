"""Direct-COM SolidWorks automation subsystem (experimental, additive).

This package is the "pywin32" build path requested by the MTI_Python workstream:
a self-contained layer that drives SolidWorks through the Windows COM API using
explicit VARIANT/SAFEARRAY marshalling and a context-managed session.

IMPORTANT — how this relates to the rest of the repo:

  * This repo ALREADY builds ``.sldprt`` files by direct COM in
    ``pipeline/solidworks_builder.py`` (see ``--engine com``). That module is the
    proven geometry engine and is NOT replaced here. The prompt that requested
    this package assumed the current build stage was "VBA macro text executed via
    COM"; in this codebase the VBA macros (``pipeline/macro_generator.py``) and the
    COM build are two PARALLEL artifacts of the same build plan.

  * What this package adds on top: (1) a single source of truth for point/array
    VARIANT construction (``marshalling.py``), (2) a context-managed session that
    keeps SolidWorks open across builds and raises a structured
    :class:`~automation.com_client.SolidWorksComError` (``marshalling.py`` +
    ``com_client.py``), and (3) an op-level executor + a feature-flagged A/B entry
    point (``build_executor.py``) that runs the SAME build plan through the proven
    engine while emitting a structured build report for parity comparison.

  * Nothing here removes or modifies the VBA path; it is guarded behind
    ``BUILD_EXECUTOR_MODE`` (``automation/config.py``) so both paths can be A/B
    tested on the same build plan before either is retired.

Everything imports cleanly on any OS — the Windows-only ``win32com``/``pythoncom``
imports are lazy and only fire when a COM call is actually made.
"""
from __future__ import annotations

from automation.com_client import SolidWorksComError, SolidWorksSession
from automation.config import build_executor_mode

__all__ = [
    "SolidWorksSession",
    "SolidWorksComError",
    "build_executor_mode",
]
