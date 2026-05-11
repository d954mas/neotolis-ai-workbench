"""Tests for naiw_tasks.lifecycle — start/finish orchestration.

Mocks the docker client and uses mock_subprocess_run from conftest for git;
the real filesystem under tmp_naiw_data exercises store/ids/path_validation.
"""

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import docker.errors

import naiw_tasks.lifecycle as lifecycle
from naiw_tasks.config import Config
from naiw_tasks.model import FinishPolicy, Status, TaskKind


# ---------- helpers ----------------------------------------------------------


def _make_cfg(tmp_naiw_data: Path) -> Config:
    return Config(data_root=tmp_naiw_data)


def _make_fake_repo(data_root: Path, alias: str) -> Path:
    repo = data_root / "workspace" / "repos" / alias
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (data_root / "projects.yaml").write_text(
        f"projects:\n  {alias}:\n    path: {repo}\n",
        encoding="utf-8",
    )
    return repo


def _fake_client(container_name: str = "naiw-task-alpha-001"):
    client = MagicMock()
    container = MagicMock()
    container.name = container_name
    container.image.id = (
        "sha256:deadbeef0000000000000000000000000000000000000000000000000000000000"
    )
    client.containers.run = MagicMock(return_value=container)
    client.containers.get = MagicMock(return_value=container)
    return client, container


def _git_side_effect(task_work_path: Path):
    """side_effect for mock_subprocess_run.

    rev-parse → short sha; worktree add → mkdirs the work path (mimics git);
    worktree remove/prune → no-op success.
    """

    def _impl(args, *a, **kw):
        cmd = list(args)
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="abc1234\n", stderr=""
            )
        if "worktree" in cmd and "add" in cmd:
            task_work_path.mkdir(parents=True, exist_ok=True)
            (task_work_path / ".git").write_text(
                "gitdir: ../alpha/.git/worktrees/foo\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    return _impl


# ---------- skeleton kind-awareness -----------------------------------------


def test_make_skeleton_generic_creates_work(tmp_naiw_data):
    td = lifecycle._make_skeleton(tmp_naiw_data, "task-001", TaskKind.GENERIC)
    assert (td / "meta").is_dir()
    assert (td / "io" / ".naiw").is_dir()
    assert (td / "work").is_dir()
    assert list((td / "work").iterdir()) == []


def test_make_skeleton_project_does_not_create_work(tmp_naiw_data):
    td = lifecycle._make_skeleton(tmp_naiw_data, "alpha-001", TaskKind.PROJECT)
    assert (td / "meta").is_dir()
    assert (td / "io" / ".naiw").is_dir()
    assert not (td / "work").exists()


def test_lifecycle_module_does_not_call_rmdir():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "lifecycle.py"
    ).read_text(encoding="utf-8")
    assert ".rmdir(" not in src
    assert "os.rmdir" not in src
    assert "shutil.rmtree" not in src
    assert "rm -rf" not in src


# ---------- start — project task --------------------------------------------


def test_start_project_happy(tmp_naiw_data, mock_subprocess_run, capsys):
    _make_fake_repo(tmp_naiw_data, "alpha")
    work_path = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    mock_subprocess_run.side_effect = _git_side_effect(work_path)
    client, container = _fake_client(container_name="naiw-task-alpha-001")

    cfg = _make_cfg(tmp_naiw_data)
    task = lifecycle.start(
        cfg,
        client,
        project="alpha",
        base_ref=None,
        finish_policy="ask",
        secrets=[],
    )

    assert task.id == "alpha-001"
    assert task.kind == TaskKind.PROJECT
    assert task.status == Status.RUNNING
    assert task.branch == "agent/alpha-001"
    assert task.worktree_path is not None
    assert task.worktree_path.endswith("tasks/alpha-001/work") or task.worktree_path.endswith(
        "tasks\\alpha-001\\work"
    )
    assert task.base_branch == "origin/main"
    assert task.base_commit == "abc1234"
    assert task.image_tag == "naiw-task-image:latest"
    assert task.image_digest.startswith("sha256:")

    # containers.run called once
    assert client.containers.run.call_count == 1
    _, kwargs = client.containers.run.call_args
    assert kwargs["image"] == "naiw-task-image:latest"
    assert kwargs["name"] == "naiw-task-alpha-001"
    assert kwargs["labels"] == {
        "naiw.managed": "1",
        "naiw.task-id": "alpha-001",
        "naiw.role": "task-container",
        "naiw.project": "alpha",
    }
    assert kwargs["detach"] is True
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS

    for key in HARDENED_HOST_CONFIG_KWARGS:
        assert key in kwargs, f"missing hardened key {key!r}"

    # Volumes
    vols = kwargs["volumes"]
    work_bind = None
    io_bind = None
    pi_bind = None
    for src, spec in vols.items():
        if spec["bind"] == "/work":
            work_bind = (src, spec)
        elif spec["bind"] == "/io":
            io_bind = (src, spec)
        elif spec["bind"] == "/pi-packages":
            pi_bind = (src, spec)
    assert work_bind is not None
    assert work_bind[1]["mode"] == "rw"
    assert "alpha-001" in work_bind[0]
    assert io_bind is not None
    assert io_bind[1]["mode"] == "rw"
    assert pi_bind is not None
    assert pi_bind[1]["mode"] == "ro"

    # The git worktree add call was issued (mock_subprocess_run is the SUT for git)
    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    assert any("worktree" in c and "add" in c for c in cmds)

    # task.json on disk
    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert tj.exists()
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["status"] == "running"

    # All three task subdirs exist
    assert (tmp_naiw_data / "tasks" / "alpha-001" / "meta").is_dir()
    assert (tmp_naiw_data / "tasks" / "alpha-001" / "work").is_dir()
    assert (tmp_naiw_data / "tasks" / "alpha-001" / "io").is_dir()

    captured = capsys.readouterr()
    assert "started naiw-task-alpha-001 (status=running)" in captured.out
    assert "naiw-tasks attach alpha-001" in captured.out
    assert "Ctrl-P Ctrl-Q" in captured.out


# ---------- start — generic task --------------------------------------------


def test_start_generic_happy(tmp_naiw_data, mock_subprocess_run, capsys):
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    task = lifecycle.start(
        cfg,
        client,
        project=None,
        base_ref=None,
        finish_policy="ask",
        secrets=[],
    )

    assert task.id == "task-001"
    assert task.kind == TaskKind.GENERIC
    assert task.project is None
    assert task.branch is None
    assert task.worktree_path is None

    # No git invoked for generic tasks
    assert mock_subprocess_run.call_count == 0

    # work/ created and empty
    work = tmp_naiw_data / "tasks" / "task-001" / "work"
    assert work.is_dir()
    assert list(work.iterdir()) == []

    # Labels do NOT include naiw.project
    _, kwargs = client.containers.run.call_args
    assert "naiw.project" not in kwargs["labels"]
    assert kwargs["labels"]["naiw.task-id"] == "task-001"

    captured = capsys.readouterr()
    assert "started naiw-task-task-001 (status=running)" in captured.out


# ---------- start — bind-source defence --------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics differ on Windows")
def test_start_aborts_on_symlinked_pi_packages(tmp_naiw_data):
    # Replace ~/naiw-data/pi-packages/ with a symlink to /etc
    pi_pkgs = tmp_naiw_data / "pi-packages"
    pi_pkgs.rmdir()
    os.symlink("/etc", str(pi_pkgs))

    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg,
            client,
            project=None,
            base_ref=None,
            finish_policy="ask",
            secrets=[],
        )
    # containers.run NOT called
    assert client.containers.run.call_count == 0
    # task.json.status = failed (the skeleton was created, so update fires)
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    if tj.exists():
        assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "failed"


# ---------- start — secrets --------------------------------------------------


def test_start_with_secret_validates_and_mounts(tmp_naiw_data):
    secret = tmp_naiw_data / "secrets" / "test_token"
    secret.write_text("s3cret", encoding="utf-8")
    os.chmod(secret, 0o600)

    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    task = lifecycle.start(
        cfg,
        client,
        project=None,
        base_ref=None,
        finish_policy="ask",
        secrets=["test_token"],
    )
    assert task.secrets == ["test_token"]

    _, kwargs = client.containers.run.call_args
    vols = kwargs["volumes"]
    found = False
    for src, spec in vols.items():
        if spec["bind"] == "/run/secrets/test_token":
            assert "test_token" in src
            assert spec["mode"] == "ro"
            found = True
    assert found, "secret bind mount missing"


def test_start_with_unknown_secret_aborts(tmp_naiw_data):
    client, _ = _fake_client()
    cfg = _make_cfg(tmp_naiw_data)
    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg,
            client,
            project=None,
            base_ref=None,
            finish_policy="ask",
            secrets=["nonexistent_token"],
        )


# ---------- start — scaffold-first invariant ---------------------------------


def test_start_writes_schema_valid_scaffold_before_any_failable_op(tmp_naiw_data):
    """projects.load raising must leave a schema-valid task.json on disk."""
    # No projects.yaml exists → projects.load raises FileNotFoundError, which is
    # now caught by start()'s except clause and translated to StartFailed (clean
    # operator-visible error, not raw Python traceback).
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project="alpha", base_ref=None, finish_policy="ask", secrets=[]
        )

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert tj.exists(), "scaffold task.json must exist after early failure"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["id"] == "alpha-001"
    assert on_disk["kind"] == "project"
    assert on_disk["container_name"] == "naiw-task-alpha-001"
    assert on_disk["status"] == "failed"
    assert "projects.yaml" in on_disk["failure_reason"]


def test_start_corrupted_projects_yaml_raises_start_failed(tmp_naiw_data, capsys):
    """A malformed projects.yaml must surface as StartFailed, not a YAML traceback."""
    (tmp_naiw_data / "projects.yaml").write_text(
        "this is: not: valid: yaml: }}}\n", encoding="utf-8"
    )
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project="alpha", base_ref=None, finish_policy="ask", secrets=[]
        )

    captured = capsys.readouterr()
    assert "naiw-tasks: start failed for task alpha-001" in captured.err

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"


def test_start_failed_before_worktree_keeps_finish_recoverable(
    tmp_naiw_data, mock_subprocess_run
):
    """Worktree-add failure: task.json is schema-valid and finish works on it."""
    _make_fake_repo(tmp_naiw_data, "alpha")
    # rev-parse OK, worktree add → fail
    def _git_fail_worktree(args, *a, **kw):
        cmd = list(args)
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="abc1234\n", stderr=""
            )
        if "worktree" in cmd and "add" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=128,
                stdout="",
                stderr="fatal: invalid reference: bogus\n",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    mock_subprocess_run.side_effect = _git_fail_worktree
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project="alpha", base_ref=None, finish_policy="ask", secrets=[]
        )

    # task.json must be schema-valid (has schema_version, id, kind, container_name)
    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["id"] == "alpha-001"
    assert on_disk["kind"] == "project"
    assert on_disk["container_name"] == "naiw-task-alpha-001"
    assert on_disk["status"] == "failed"
    assert "git worktree add failed" in on_disk["failure_reason"]

    # finish must succeed without UnsupportedSchemaError (the bug we're fixing).
    # Use policy_override to skip the interactive prompt (the failed task has
    # worktree_path=None so policy is functionally irrelevant — pass keep_worktree
    # to assert no git call is attempted).
    lifecycle.finish(cfg, client, "alpha-001", policy_override="keep_worktree")

    on_disk2 = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk2["status"] == "completed"
    assert on_disk2["finished_at"] is not None


def test_start_failed_before_projects_load_keeps_finish_recoverable(tmp_naiw_data):
    """projects.yaml unknown alias: task.json is schema-valid and finish works."""
    (tmp_naiw_data / "projects.yaml").write_text(
        "projects: {}\n", encoding="utf-8"
    )
    client, _ = _fake_client(container_name="naiw-task-ghost-001")
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project="ghost", base_ref=None, finish_policy="ask", secrets=[]
        )

    tj = tmp_naiw_data / "tasks" / "ghost-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["status"] == "failed"
    assert "unknown project" in on_disk["failure_reason"]

    # finish reads, sees schema_version=1, marks completed — no UnsupportedSchemaError
    lifecycle.finish(cfg, client, "ghost-001", policy_override="keep_worktree")
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


# ---------- start — failure rollback -----------------------------------------


def test_start_writes_failed_status_on_containers_run_error(
    tmp_naiw_data, mock_subprocess_run, capsys
):
    _make_fake_repo(tmp_naiw_data, "alpha")
    work_path = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    mock_subprocess_run.side_effect = _git_side_effect(work_path)

    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    client.containers.run.side_effect = docker.errors.APIError("docker create failed")
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg,
            client,
            project="alpha",
            base_ref=None,
            finish_policy="ask",
            secrets=[],
        )

    # task.json.status = failed
    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert tj.exists()
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert on_disk["failure_reason"]

    # Worktree dir LEFT IN PLACE
    assert work_path.exists()

    captured = capsys.readouterr()
    assert "naiw-tasks finish alpha-001 --delete-worktree" in captured.err


# ---------- status writes ----------------------------------------------------


def test_start_writes_status_running_after_run(tmp_naiw_data):
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.start(
        cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
    )
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "running"


def test_start_writes_running_and_prints_hint(tmp_naiw_data, capsys):
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.start(
        cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
    )
    captured = capsys.readouterr()
    assert "started naiw-task-task-001 (status=running)" in captured.out


# ---------- worktree + controller.log invariants -----------------------------


def test_project_work_is_worktree(tmp_naiw_data, mock_subprocess_run):
    _make_fake_repo(tmp_naiw_data, "alpha")
    work_path = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    mock_subprocess_run.side_effect = _git_side_effect(work_path)
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.start(
        cfg, client, project="alpha", base_ref=None, finish_policy="ask", secrets=[]
    )
    # subprocess called with worktree add
    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    assert any(
        "worktree" in c and "add" in c and f"agent/alpha-001" in c for c in cmds
    )


def test_generic_work_is_empty(tmp_naiw_data):
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.start(
        cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
    )
    work = tmp_naiw_data / "tasks" / "task-001" / "work"
    assert work.is_dir()
    assert list(work.iterdir()) == []


def test_controller_log_written_to_meta(tmp_naiw_data):
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.start(
        cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
    )
    log = tmp_naiw_data / "tasks" / "task-001" / "meta" / "controller.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "task-001" in text


# ---------- finish — permissive matrix ---------------------------------------


def _pre_create_task(
    data_root: Path,
    task_id: str,
    status: str,
    kind: str = "generic",
    project: str | None = None,
    finish_policy: str = "ask",
    worktree_path: str | None = None,
    project_repo_path: str | None = None,
) -> Path:
    """Pre-create a meta/task.json on disk in the requested state."""
    task_dir = data_root / "tasks" / task_id
    (task_dir / "meta").mkdir(parents=True, exist_ok=True)
    (task_dir / "io" / ".naiw").mkdir(parents=True, exist_ok=True)
    if kind == "generic":
        (task_dir / "work").mkdir(exist_ok=True)
    body = {
        "schema_version": 1,
        "id": task_id,
        "kind": kind,
        "container_name": f"naiw-task-{task_id}",
        "image_tag": "naiw-task-image:latest",
        "created_at": "2026-05-11T00:00:00.000Z",
        "updated_at": "2026-05-11T00:00:00.000Z",
        "status": status,
        "failure_reason": None,
        "started_at": None,
        "finished_at": None,
        "image_digest": None,
        "finish_policy": finish_policy,
        "auto_finish": False,
        "project": project,
        "branch": (f"agent/{task_id}" if project else None),
        "worktree_path": worktree_path,
        "base_branch": None,
        "base_commit": None,
        "project_repo_path": project_repo_path,
        "labels": {},
        "secrets": [],
        "events_offset": 0,
        "recovery_count": 0,
        "recovery_history": [],
    }
    (task_dir / "meta" / "task.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return task_dir


def test_finish_running_calls_stop_remove_and_marks_completed(tmp_naiw_data):
    _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "completed"
    assert on_disk["finished_at"] is not None


def test_finish_created_marks_completed(tmp_naiw_data):
    _pre_create_task(tmp_naiw_data, "task-001", "created")
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.finish(cfg, client, "task-001", policy_override=None)
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_failed_marks_completed(tmp_naiw_data):
    _pre_create_task(tmp_naiw_data, "task-001", "failed")
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.finish(cfg, client, "task-001", policy_override=None)
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_completed_is_idempotent(tmp_naiw_data, capsys):
    _pre_create_task(tmp_naiw_data, "task-001", "completed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    container.stop.assert_not_called()
    container.remove.assert_not_called()
    captured = capsys.readouterr()
    assert "already completed; nothing to do" in captured.out
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_tolerates_container_already_removed(tmp_naiw_data):
    _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, _ = _fake_client(container_name="naiw-task-task-001")
    client.containers.get.side_effect = docker.errors.NotFound("gone")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_keep_worktree_does_not_call_git_remove(
    tmp_naiw_data, mock_subprocess_run
):
    _make_fake_repo(tmp_naiw_data, "alpha")
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        worktree_path=str(work),
    )
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "alpha-001", policy_override="keep_worktree")

    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    for c in cmds:
        assert not ("worktree" in c and "remove" in c), f"unexpected git call: {c}"
        assert not ("worktree" in c and "prune" in c), f"unexpected git call: {c}"


def test_finish_delete_worktree_calls_git_remove_then_prune(
    tmp_naiw_data, mock_subprocess_run
):
    repo = _make_fake_repo(tmp_naiw_data, "alpha")
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        worktree_path=str(work),
        project_repo_path=str(repo),
    )
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "alpha-001", policy_override="delete_worktree")

    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    git_calls = [c for c in cmds if c and c[0] == "git"]
    remove_idx = None
    prune_idx = None
    for i, c in enumerate(git_calls):
        if "worktree" in c and "remove" in c:
            remove_idx = i
        elif "worktree" in c and "prune" in c:
            prune_idx = i
    assert remove_idx is not None, f"no worktree remove call: {git_calls}"
    assert prune_idx is not None, f"no worktree prune call: {git_calls}"
    assert remove_idx < prune_idx, "remove must happen before prune"


def test_finish_uses_project_repo_path_not_projects_yaml(
    tmp_naiw_data, mock_subprocess_run
):
    """finish must read repo path from task.json.project_repo_path, never re-load
    projects.yaml. This decouples teardown from config edits between start/finish."""
    repo = _make_fake_repo(tmp_naiw_data, "alpha")
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        worktree_path=str(work),
        project_repo_path=str(repo),
    )

    # Now WIPE projects.yaml — simulate operator removing/renaming the alias
    # between start and finish. The task.json already has the resolved path,
    # so finish must NOT depend on the yaml being readable.
    (tmp_naiw_data / "projects.yaml").unlink()

    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    # Must succeed (no FileNotFoundError from projects.load)
    lifecycle.finish(cfg, client, "alpha-001", policy_override="delete_worktree")

    # And git_ops.worktree_remove must have been called against the original repo
    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    git_calls = [c for c in cmds if c and c[0] == "git" and "worktree" in c and "remove" in c]
    assert len(git_calls) == 1, f"expected one worktree remove, got: {git_calls}"
    # `git -C <repo_path> worktree remove --force <work_path>`
    assert str(repo) in git_calls[0]

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_corrupted_projects_yaml_still_completes(
    tmp_naiw_data, mock_subprocess_run
):
    """Even when projects.yaml is unparseable, finish must complete the task."""
    repo = _make_fake_repo(tmp_naiw_data, "alpha")
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        worktree_path=str(work),
        project_repo_path=str(repo),
    )

    (tmp_naiw_data / "projects.yaml").write_text(
        "this is: not: valid: yaml: }}}\n", encoding="utf-8"
    )

    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "alpha-001", policy_override="delete_worktree")
    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_start_records_project_repo_path_in_task_json(
    tmp_naiw_data, mock_subprocess_run
):
    """start() must record the resolved repo path so finish can be self-contained."""
    repo = _make_fake_repo(tmp_naiw_data, "alpha")
    work_path = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    mock_subprocess_run.side_effect = _git_side_effect(work_path)
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    task = lifecycle.start(
        cfg, client, project="alpha", base_ref=None, finish_policy="ask", secrets=[]
    )

    assert task.project_repo_path is not None
    # Resolved form must match what's on disk
    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["project_repo_path"] == str(repo.resolve())
    assert on_disk["project_repo_path"] == task.project_repo_path


def test_start_generic_does_not_set_project_repo_path(tmp_naiw_data):
    """Generic tasks have no repo; project_repo_path stays None."""
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    task = lifecycle.start(
        cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
    )

    assert task.project_repo_path is None
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["project_repo_path"] is None


def test_finish_never_uses_rm_rf():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "rm -rf" not in src
    assert "shutil.rmtree" not in src


# ---------- concurrent finish (flock) ---------------------------------------


def _finish_worker(data_root_str: str, task_id: str) -> int:
    """Spawn-process worker calling lifecycle.finish."""
    # Re-import inside spawn child — module globals are fresh per process.
    import naiw_tasks.lifecycle as _lifecycle
    from naiw_tasks.config import Config as _Config
    from unittest.mock import MagicMock as _MM
    import docker.errors as _de  # noqa: F401

    data_root = Path(data_root_str)
    cfg = _Config(data_root=data_root)
    client = _MM()
    container = _MM()
    container.name = f"naiw-task-{task_id}"
    client.containers.get = _MM(return_value=container)
    try:
        _lifecycle.finish(cfg, client, task_id, policy_override=None)
        return 0
    except Exception:
        return 1


def test_finish_concurrent_serialise_under_flock(tmp_naiw_data):
    _pre_create_task(tmp_naiw_data, "task-001", "running")

    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_finish_worker, args=(str(tmp_naiw_data), "task-001"))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    for p in procs:
        assert p.exitcode == 0, f"finish worker exit {p.exitcode}"

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"
