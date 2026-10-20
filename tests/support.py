"""Shared test helpers: the package on sys.path, stack folders in a temp directory, the mocks."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
