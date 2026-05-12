"""Shared fixtures for naiw_tasks unit tests.

Fixtures exposed:
  tmp_naiw_data       Path to a per-test isolated NAIW_DATA skeleton.
  mock_subprocess_run MagicMock around naiw_tasks.git_ops.subprocess.run.
  mock_execvp         MagicMock around naiw_tasks.attach.os.execvp.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_naiw_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "naiw-data"
    (root / "secrets").mkdir(parents=True)
    (root / "pi-packages").mkdir()
    (root / "workspace" / "repos").mkdir(parents=True)
    (root / "tasks").mkdir()
    (root / ".locks").mkdir()
    (root / ".counters").mkdir()
    os.chmod(root / "secrets", 0o700)
    monkeypatch.setenv("NAIW_DATA", str(root))
    return root


@pytest.fixture
def mock_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    # Lazy import — modules consumed by these fixtures may not yet exist when a
    # test file in another plan-task is collected; the import only fires for tests
    # that actually request the fixture.
    import naiw_tasks.git_ops as git_ops

    mock = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
    )
    monkeypatch.setattr(git_ops.subprocess, "run", mock)
    return mock


@pytest.fixture
def mock_execvpe(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import naiw_tasks.attach as attach_mod

    mock = MagicMock()
    monkeypatch.setattr(attach_mod.os, "execvpe", mock)
    return mock
