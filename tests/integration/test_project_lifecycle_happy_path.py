"""Scenario 1: project task happy path.

Full lifecycle via the wrapper: start project task -> naiw-signal done
(inside the task container) -> list applies the event -> finish captures
artifacts -> clean removes the task folder. Exercises the reconcile loop
AND the artifact-capture path through the deployed compose stack.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_project(data_root: Path, alias: str = "demo") -> str:
    """Seed a tiny git repo under workspace/repos/<alias>/ with one commit.

    Also writes projects.yaml so the controller's projects.load() resolves
    the alias to the on-disk path.
    """
    repo = data_root / "workspace" / "repos" / alias
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo, check=True, capture_output=True,
    )
    py = data_root / "projects.yaml"
    py.write_text(
        f"projects:\n  {alias}:\n    path: workspace/repos/{alias}\n",
        encoding="utf-8",
    )
    return alias


def _read_task_json(task_dir: Path) -> dict:
    return json.loads((task_dir / "meta" / "task.json").read_text(encoding="utf-8"))


def _find_task_dir(data_root: Path, alias: str) -> Path:
    """Locate the single tasks/<alias>-NNN/ directory for this run."""
    candidates = sorted((data_root / "tasks").glob(f"{alias}-*"))
    assert len(candidates) == 1, (
        f"expected exactly 1 task dir for alias={alias!r}, got {candidates}"
    )
    return candidates[0]


def test_project_lifecycle_happy_path(compose_stack, run_naiw_tasks):
    data_root = compose_stack["data_root"]
    env = compose_stack["env"]
    alias = _seed_project(data_root)

    result = run_naiw_tasks("start", alias)
    assert result.returncode == 0, f"start failed: stderr={result.stderr!r}"

    task_dir = _find_task_dir(data_root, alias)
    task_id = task_dir.name
    task_json = _read_task_json(task_dir)
    assert task_json["status"] == "running", task_json

    # Send `naiw-signal done` inside the task container — the controller's
    # lazy-event tailer applies the event on the next `naiw-tasks list`.
    subprocess.run(
        [
            "docker", "exec", f"naiw-task-{task_id}",
            "naiw-signal", "done", "--summary", "hello",
        ],
        env=env, check=True, capture_output=True,
    )

    # Poll list until events_offset advances (the event was consumed).
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
    assert task_json["status"] == "completed", task_json

    artifacts = task_dir / "meta" / "artifacts"
    assert artifacts.is_dir(), f"artifacts dir missing at {artifacts}"
    assert (artifacts / "terminal.log").exists()
    assert (artifacts / "git-status.txt").exists()
    assert (artifacts / "diff.patch").exists()

    result = run_naiw_tasks("clean", "--older-than", "0s", "--yes")
    assert result.returncode == 0, f"clean failed: stderr={result.stderr!r}"
    assert not task_dir.exists(), f"task dir survived clean: {task_dir}"
