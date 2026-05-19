"""Scenario 3: recover after docker kill.

Start a project task, SIGKILL its container, wait for reconciliation to
flip status to interrupted, then recover. Asserts the new container is
healthy, recovery_count==1, and the host-side recovery banner is appended
to terminal.log without truncation.

Note on banner wording: lifecycle._recover writes
`===== RECOVERY ATTEMPT #N AT <iso-ts-ms-Z> =====` BEFORE starting the
new container (past-tense "RECOVERED" would lie if the run failed). We
assert against that exact prefix.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Same banner prefix lifecycle._append_recovery_banner emits. The test
# locks the format so a future refactor surfaces here, not in the operator
# runbook.
RECOVERY_BANNER_RE = re.compile(
    r"===== RECOVERY ATTEMPT #\d+ AT \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ====="
)


def _read_task_json(task_dir: Path) -> dict:
    return json.loads((task_dir / "meta" / "task.json").read_text(encoding="utf-8"))


def test_recover_after_docker_kill(compose_stack, run_naiw_tasks, seed_project):
    data_root = compose_stack["data_root"]
    env = compose_stack["env"]
    alias = seed_project("recov")

    result = run_naiw_tasks("start", alias)
    assert result.returncode == 0, f"start failed: stderr={result.stderr!r}"

    task_dirs = sorted((data_root / "tasks").glob(f"{alias}-*"))
    assert len(task_dirs) == 1
    task_dir = task_dirs[0]
    task_id = task_dir.name
    container = f"naiw-task-{task_id}"

    # SIGKILL the task container directly (NOT through the proxy — the test
    # simulates a daemon-driven kill, not an operator finish).
    subprocess.run(
        ["docker", "kill", "--signal=SIGKILL", container],
        env=env, check=True, capture_output=True,
    )

    # `naiw-tasks list` reconciles the running-on-disk status against the
    # absent-on-daemon container; truth table maps that to interrupted.
    for _ in range(15):
        result = run_naiw_tasks("list", "--all")
        assert result.returncode == 0, result.stderr
        task_json = _read_task_json(task_dir)
        if task_json["status"] == "interrupted":
            break
        time.sleep(2)
    else:
        pytest.fail(f"status did not reconcile to interrupted: {task_json}")

    log_size_before = (task_dir / "io" / "terminal.log").stat().st_size

    result = run_naiw_tasks("recover", task_id)
    assert result.returncode == 0, f"recover failed: stderr={result.stderr!r}"

    task_json = _read_task_json(task_dir)
    assert task_json["status"] == "running", task_json
    assert task_json["recovery_count"] == 1, task_json

    # terminal.log: append-only across the recover boundary AND carries the
    # banner. We check size is monotonic AND the regex matches somewhere.
    log_path = task_dir / "io" / "terminal.log"
    log_size_after = log_path.stat().st_size
    assert log_size_after >= log_size_before, (
        f"terminal.log shrunk across recover: {log_size_before} -> {log_size_after}"
    )
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    assert RECOVERY_BANNER_RE.search(log_text), (
        f"recovery banner missing from terminal.log:\n{log_text[-500:]}"
    )

    # Cleanup so the next session doesn't inherit a running task.
    result = run_naiw_tasks("finish", task_id)
    assert result.returncode == 0, result.stderr
