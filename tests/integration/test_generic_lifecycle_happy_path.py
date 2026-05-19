"""Scenario 2: generic task happy path.

Same shape as scenario 1 but without a project alias — `start` produces a
generic task (id pattern `task-NNN`), no worktree, no agent/ branch, no
git-driven artifacts. terminal.log capture still happens.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _read_task_json(task_dir: Path) -> dict:
    return json.loads((task_dir / "meta" / "task.json").read_text(encoding="utf-8"))


def _find_generic_task_dir(data_root: Path) -> Path:
    candidates = sorted((data_root / "tasks").glob("task-*"))
    assert len(candidates) == 1, f"expected exactly 1 generic task dir, got {candidates}"
    return candidates[0]


def test_generic_lifecycle_happy_path(compose_stack, run_naiw_tasks):
    data_root = compose_stack["data_root"]
    env = compose_stack["env"]

    result = run_naiw_tasks("start")
    assert result.returncode == 0, f"start failed: stderr={result.stderr!r}"

    task_dir = _find_generic_task_dir(data_root)
    task_id = task_dir.name
    task_json = _read_task_json(task_dir)
    assert task_json["status"] == "running"
    # Generic tasks must NOT carry a project alias.
    assert task_json.get("project") in (None, ""), task_json

    subprocess.run(
        [
            "docker", "exec", f"naiw-task-{task_id}",
            "naiw-signal", "done", "--summary", "generic-ok",
        ],
        env=env, check=True, capture_output=True,
    )

    for _ in range(15):
        result = run_naiw_tasks("list", "--all")
        assert result.returncode == 0, result.stderr
        task_json = _read_task_json(task_dir)
        if task_json["events_offset"] > 0:
            break
        time.sleep(2)
    else:
        pytest.fail(f"events_offset did not advance: {task_json}")

    result = run_naiw_tasks("finish", task_id)
    assert result.returncode == 0, f"finish failed: stderr={result.stderr!r}"
    task_json = _read_task_json(task_dir)
    assert task_json["status"] == "completed"

    artifacts = task_dir / "meta" / "artifacts"
    assert artifacts.is_dir()
    # Generic tasks: terminal.log is captured; the three git-driven captures
    # are not (no worktree -> no git-status.txt / changed-files.txt / diff.patch).
    assert (artifacts / "terminal.log").exists()
    assert not (artifacts / "git-status.txt").exists(), (
        "generic task must not produce git-status.txt"
    )
    assert not (artifacts / "diff.patch").exists(), (
        "generic task must not produce diff.patch"
    )

    result = run_naiw_tasks("clean", "--older-than", "0s", "--yes")
    assert result.returncode == 0, result.stderr
    assert not task_dir.exists()
