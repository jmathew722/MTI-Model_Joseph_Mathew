"""Pytest configuration: ensure the project root is importable.

Lets tests do ``from pipeline... import`` / ``from utils... import`` regardless
of the directory pytest is invoked from.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
