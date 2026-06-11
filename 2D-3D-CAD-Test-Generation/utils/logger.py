"""Structured logging used across the whole pipeline.

A single configured logger so every stage logs in a consistent format. Uses
`rich` for clean colored terminal output when available, falling back to the
stdlib formatter otherwise.
"""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def get_logger(name: str = "pipeline") -> logging.Logger:
    """Return the shared, lazily-configured pipeline logger.

    Configures the root pipeline handler exactly once. Safe to call from any
    module at import time.
    """
    global _CONFIGURED
    logger = logging.getLogger(name)

    if not _CONFIGURED:
        logger.setLevel(logging.INFO)
        handler: logging.Handler
        try:
            from rich.logging import RichHandler  # type: ignore

            handler = RichHandler(rich_tracebacks=True, show_path=False)
            handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
        except Exception:  # rich missing or failed — fall back to stdlib.
            handler = logging.StreamHandler(stream=sys.stderr)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
        # Avoid duplicate handlers if a parent already configured logging.
        if not logger.handlers:
            logger.addHandler(handler)
        logger.propagate = False
        _CONFIGURED = True

    return logger


def set_level(level: int) -> None:
    """Set the level on the shared pipeline logger (e.g. logging.DEBUG)."""
    get_logger().setLevel(level)
