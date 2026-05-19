"""Scenario 5: doctor surfaces drift.

Start a project task. Mutate the live container via `docker update
--pids-limit=0` to simulate "Docker daemon lost a hardening flag". Assert:

- `naiw-tasks list --all` renders `(drift)` in the NOTES column for that row.
- `naiw-tasks doctor <id>` exits 1 AND emits the PidsLimit diff
  (`expected=512`, `actual=0`).

`docker update --pids-limit=0` sets HostConfig.PidsLimit=0
(NOT None / NOT "removed"). The drift module's expected value remains
512 — the controller surfaces the divergence.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Literal "(drift)" anchor — documents the rendered NOTES marker shape
# (the drift module stores the marker as "drift" and format_notes wraps
# it with parens, producing "(drift)" in the table).
EXPECTED_NOTES_MARKER = "(drift)"


def _seed_project(data_root: Path, alias: str = "drift") -> str:
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


def test_doctor_surfaces_drift(compose_stack, run_naiw_tasks):
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

    # Mutate the container via host docker CLI — NOT through the proxy.
    # The test simulates an out-of-band flag change ("daemon lost a flag")
    # which the drift audit must surface. Pitfall 7: --pids-limit=0 sets
    # PidsLimit=0, not None.
    subprocess.run(
        ["docker", "update", "--pids-limit=0", container],
        env=env, check=True, capture_output=True,
    )

    result = run_naiw_tasks("list", "--all")
    assert result.returncode == 0, f"list failed: stderr={result.stderr!r}"
    # The rendered NOTES column contains "(drift)" for this task row.
    assert EXPECTED_NOTES_MARKER in result.stdout, (
        f'expected "(drift)" in list output, got:\n{result.stdout}'
    )

    result = run_naiw_tasks("doctor", task_id)
    # doctor returns exit code 1 when drift is detected.
    assert result.returncode == 1, (
        f"doctor expected exit 1 on drift, got {result.returncode}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Field name surfaces in the diff. Drift module reports PidsLimit
    # with "resource" severity.
    assert "PidsLimit" in result.stdout, (
        f"doctor must name PidsLimit in the diff:\n{result.stdout}"
    )
    assert "expected=512" in result.stdout, (
        f"doctor must show expected=512:\n{result.stdout}"
    )

    # Cleanup — finish so the next session has a clean slate.
    result = run_naiw_tasks("finish", task_id)
    assert result.returncode == 0, result.stderr
