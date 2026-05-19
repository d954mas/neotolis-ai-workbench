"""Scenario 4: auto_finish via fail event.

Start a project task with the `--auto-finish` flag (verified inline by
reading `src/naiw_tasks/naiw_tasks/cli.py`), send `naiw-signal fail`
inside the container, then run `naiw-tasks reap` to drive the teardown.
Asserts:

- task.json.status == "failed"
- meta/artifacts/terminal.log and meta/artifacts/diff.patch present
- the task container no longer exists on the daemon

**auto_finish setup path:** preferred path (CLI flag). `naiw-tasks start
--auto-finish <alias>` is the operator-facing contract. The plan
contemplates a fallback (`store.update_task` exec'd inside the controller
container) for the case where the flag is absent — that path is NOT used
here because the flag is present.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_project(data_root: Path, alias: str = "autofin") -> str:
    repo = data_root / "workspace" / "repos" / alias
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True,
    )
    (data_root / "projects.yaml").write_text(
        f"projects:\n  {alias}:\n    path: workspace/repos/{alias}\n",
        encoding="utf-8",
    )
    return alias


def _read_task_json(task_dir: Path) -> dict:
    return json.loads((task_dir / "meta" / "task.json").read_text(encoding="utf-8"))


def test_auto_finish_via_fail_event(compose_stack, run_naiw_tasks):
    data_root = compose_stack["data_root"]
    env = compose_stack["env"]
    alias = _seed_project(data_root)

    # Preferred auto_finish setup path: the start command's --auto-finish flag.
    result = run_naiw_tasks("start", "--auto-finish", alias)
    assert result.returncode == 0, f"start --auto-finish failed: {result.stderr!r}"

    task_dirs = sorted((data_root / "tasks").glob(f"{alias}-*"))
    assert len(task_dirs) == 1
    task_dir = task_dirs[0]
    task_id = task_dir.name
    container = f"naiw-task-{task_id}"

    task_json = _read_task_json(task_dir)
    assert task_json.get("auto_finish") is True, task_json

    subprocess.run(
        [
            "docker", "exec", container,
            "naiw-signal", "fail", "--reason", "integration test",
        ],
        env=env, check=True, capture_output=True,
    )

    # `reap` is the operator surface for auto_finish — it applies pending
    # done/fail events with auto_finish=true and drives teardown_and_mark
    # directly. Polling because the container tear-down + atomic write
    # are not instant.
    for _ in range(15):
        result = run_naiw_tasks("reap")
        assert result.returncode == 0, result.stderr
        task_json = _read_task_json(task_dir)
        if task_json["status"] == "failed":
            break
        time.sleep(2)
    else:
        pytest.fail(f"task did not transition to failed: {task_json}")

    artifacts = task_dir / "meta" / "artifacts"
    assert artifacts.is_dir(), f"artifacts dir missing at {artifacts}"
    assert (artifacts / "terminal.log").exists()
    assert (artifacts / "diff.patch").exists(), (
        "auto_finish must capture diff.patch (project task)"
    )

    # Container must be gone — auto_finish runs the full teardown.
    inspect = subprocess.run(
        ["docker", "inspect", container],
        env=env, capture_output=True, text=True, check=False,
    )
    assert inspect.returncode != 0, (
        f"task container {container!r} still exists after auto_finish: {inspect.stdout!r}"
    )
