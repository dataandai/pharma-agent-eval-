from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.data.repositories import SandboxRepository
from src.interpreter import RuleBasedInterpreter


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[1]
    shutil.copytree(source_root / "data", tmp_path / "data")
    SandboxRepository(tmp_path).reset()
    return tmp_path


@pytest.fixture()
def interpreter() -> RuleBasedInterpreter:
    """Tests run against the deterministic classifier, never the network,
    regardless of whether ANTHROPIC_API_KEY happens to be set."""
    return RuleBasedInterpreter()
