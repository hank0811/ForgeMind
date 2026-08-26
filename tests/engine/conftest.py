from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo root with tasks/ and artifacts/ dirs, isolated per test."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / "artifacts").mkdir()
    return tmp_path
