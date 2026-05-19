"""Regression: a log event after done MUST NOT mask the terminal transition.

Locks the contract that `naiw-signal log` is status-neutral. Without the
reconcile.select_latest_transition_kind filter (introduced after Phase 6
code review), the controller's lazy-event tailer would feed "log" as
the pending event kind, compute_status would have no log branch, the
task would stay `running`, events_offset would advance past both events,
and the terminal signal would be lost.

This test exercises the bug scenario through the deployed wire (wrapper
shim → controller container → SDK → proxy → engine), so a future
refactor that re-introduces blind events[-1].kind selection (in
list_cmd, reap, or anywhere else) will fail here, not silently in prod.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_project(data_root: Path, alias: str = "donelog") -> str:
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


def test_log_event_after_done_does_not_mask_terminal_transition(
    compose_stack, run_naiw_tasks,
):
    """Pi emits `done` then `log` in the same batch. The next `list` MUST
    reconcile the task as `completed`, not leave it stuck on `running`.
    """
    data_root = compose_stack["data_root"]
    env = compose_stack["env"]
    alias = _seed_project(data_root)

    result = run_naiw_tasks("start", alias)
    assert result.returncode == 0, f"start failed: stderr={result.stderr!r}"

    task_dirs = sorted((data_root / "tasks").glob(f"{alias}-*"))
    assert len(task_dirs) == 1
    task_dir = task_dirs[0]
    task_id = task_dir.name
    container = f"naiw-task-{task_id}"

    # Emit BOTH events back-to-back inside the same shell so they land in
    # one read window — `done` first, then a status-neutral `log`. The
    # log MUST be transparent to the reconciler.
    subprocess.run(
        [
            "docker", "exec", container,
            "sh", "-c",
            'naiw-signal done --summary "task ok" && naiw-signal log "wrapping up"',
        ],
        env=env, check=True, capture_output=True,
    )

    # Drive reconcile. The lazy-event tailer applies the events on the next
    # `naiw-tasks list`. With the bug, status stays "running" because
    # "log" was picked as the pending kind and compute_status had no log
    # branch. With the fix, "done" wins despite "log" being newer, and
    # the row transitions to "completed".
    transitioned = False
    last_status = None
    for _ in range(15):
        result = run_naiw_tasks("list", "--all")
        assert result.returncode == 0, result.stderr
        task_json = _read_task_json(task_dir)
        last_status = task_json["status"]
        if last_status == "completed":
            transitioned = True
            break
        time.sleep(2)

    assert transitioned, (
        f"task stuck at status={last_status!r} — log event masked the "
        f"terminal kind. See reconcile.select_latest_transition_kind."
    )

    # Belt-and-braces: events_offset is past BOTH events (the log event
    # was consumed transparently, not deferred).
    task_json = _read_task_json(task_dir)
    assert task_json["events_offset"] > 0, task_json

    # Cleanup so the session leaves no leftover task.
    result = run_naiw_tasks("clean", "--older-than", "0s", "--yes")
    assert result.returncode == 0, f"clean failed: stderr={result.stderr!r}"
