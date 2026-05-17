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

import docker.errors
import naiw_tasks.lifecycle as lifecycle
import pytest
from naiw_tasks.config import Config
from naiw_tasks.model import FinishPolicy, Status, TaskKind
from naiw_tasks.path_validation import BindMountEscapeError

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


_FAKE_DIGEST = (
    "sha256:deadbeef0000000000000000000000000000000000000000000000000000000000"
)


def _fake_client(container_name: str = "naiw-task-alpha-001"):
    """Fake Docker client that mirrors real semantics:

    1. After container.remove() is called, subsequent client.containers.get(name)
       raises NotFound — same as a real daemon.
    2. container.attrs has "Image" populated up-front so the start() code path
       (which reads container.attrs["Image"] after reload()) gets the digest
       without triggering the blocked /images/<id>/json endpoint.

    Override `client.containers.get.side_effect` or `container.attrs` in
    individual tests to model APIError / digest-unresolvable scenarios.
    """
    client = MagicMock()
    container = MagicMock()
    container.name = container_name
    container.attrs = {
        "State": {"Status": "running"},
        "Image": _FAKE_DIGEST,
    }

    def _get(name):
        if container.remove.called:
            raise docker.errors.NotFound(f"{name} removed")
        return container

    client.containers.run = MagicMock(return_value=container)
    client.containers.get = MagicMock(side_effect=_get)
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes are not meaningful on Windows-native Python",
)
def test_make_skeleton_bind_mount_dirs_are_sticky_writable(tmp_naiw_data):
    """The image's pi user is hardcoded to uid 1000. When the operator's
    host uid differs (LDAP boxes, second-user installs), the default
    `mkdir` mode 0755 blocks pi from writing. _make_skeleton sets 1777
    (sticky-writable, /tmp-style) on EVERY bind-mount source — io/, io/.naiw,
    and (for generic tasks) work/ — so the container's pi can write
    regardless of operator uid."""
    td = lifecycle._make_skeleton(tmp_naiw_data, "task-001", TaskKind.GENERIC)

    for path in (td / "io", td / "io" / ".naiw", td / "work"):
        mode = path.stat().st_mode & 0o7777
        assert mode == 0o1777, (
            f"{path.name}: expected mode 1777 (sticky write), got {oct(mode)}"
        )

    # meta/ stays default — it is host-only, never bind-mounted into the
    # container. Default mkdir mode is operator-controlled (typically 0755).
    meta_mode = (td / "meta").stat().st_mode & 0o7777
    assert meta_mode != 0o1777, (
        f"meta/ MUST NOT be 1777 (host-only, not bind-mounted): {oct(meta_mode)}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes are not meaningful on Windows-native Python",
)
def test_make_worktree_writable_by_pi_dirs_are_sticky(tmp_path):
    """Directories inside a freshly-checked-out worktree must end up at
    mode 1777 so pi (uid 1000) inside the container can create/modify
    files regardless of the operator's host uid."""
    work = tmp_path / "work"
    (work / "src" / "deep").mkdir(parents=True)
    (work / "tests").mkdir()
    # Operator-default modes (0o755) before our fix
    work.chmod(0o755)
    (work / "src").chmod(0o755)
    (work / "src" / "deep").chmod(0o755)
    (work / "tests").chmod(0o755)

    lifecycle._make_worktree_writable_by_pi(work)

    for d in (work, work / "src", work / "src" / "deep", work / "tests"):
        mode = d.stat().st_mode & 0o7777
        assert mode == 0o1777, f"{d} expected 1777, got {oct(mode)}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes are not meaningful on Windows-native Python",
)
def test_make_worktree_writable_by_pi_files_get_world_write(tmp_path):
    """Regular files become world-writable (0o666) so pi can edit them."""
    work = tmp_path / "work"
    work.mkdir()
    f = work / "README.md"
    f.write_text("hi", encoding="utf-8")
    f.chmod(0o644)  # operator-default

    lifecycle._make_worktree_writable_by_pi(work)

    assert f.stat().st_mode & 0o7777 == 0o666


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes are not meaningful on Windows-native Python",
)
def test_make_worktree_writable_by_pi_preserves_executable_bit(tmp_path):
    """Executable files keep ANY exec bit so git's mode-tracking
    (100644 vs 100755) still sees the same mode — `git status` stays
    clean. Files without exec stay non-exec; files with exec get 0o777
    (write for all + exec for all)."""
    work = tmp_path / "work"
    work.mkdir()

    script = work / "build.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script.chmod(0o755)  # operator-checkout default for shell script

    plain = work / "README.md"
    plain.write_text("plain", encoding="utf-8")
    plain.chmod(0o644)

    lifecycle._make_worktree_writable_by_pi(work)

    script_mode = script.stat().st_mode & 0o7777
    plain_mode = plain.stat().st_mode & 0o7777

    # Executable file: 0o777 (some exec bit set → git mode stays 100755)
    assert script_mode == 0o777, f"build.sh: expected 0o777, got {oct(script_mode)}"
    assert script_mode & 0o111, "build.sh must keep AT LEAST one exec bit"
    # Non-executable file: 0o666 (no exec → git mode stays 100644)
    assert plain_mode == 0o666, f"README.md: expected 0o666, got {oct(plain_mode)}"
    assert not (plain_mode & 0o111), "README.md must keep NO exec bits"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX symlinks differ on Windows",
)
def test_make_worktree_writable_by_pi_skips_symlinks(tmp_path):
    """Symlinks in a worktree are NOT chmod'd — chmod through a symlink
    follows the link and could leak outside the worktree boundary
    (validate_bind_source already vetted the worktree path itself; we don't
    re-validate every symlink target here)."""
    work = tmp_path / "work"
    work.mkdir()
    # Target outside the worktree, owned by operator at strict mode
    outside = tmp_path / "secret"
    outside.write_text("secret", encoding="utf-8")
    outside.chmod(0o600)

    link = work / "shortcut"
    link.symlink_to(outside)

    lifecycle._make_worktree_writable_by_pi(work)

    # The symlink TARGET'S mode must remain 0o600 — we did not follow
    # the symlink and chmod the target.
    assert outside.stat().st_mode & 0o7777 == 0o600


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes are not meaningful on Windows-native Python",
)
def test_make_skeleton_project_io_is_sticky_no_work_yet(tmp_naiw_data):
    """Project tasks: same io/ stickiness, but work/ is NOT created here
    (git worktree add owns it later)."""
    td = lifecycle._make_skeleton(tmp_naiw_data, "alpha-001", TaskKind.PROJECT)

    for path in (td / "io", td / "io" / ".naiw"):
        assert path.stat().st_mode & 0o7777 == 0o1777
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
    assert task.image_tag == cfg.task_image
    assert task.image_digest.startswith("sha256:")

    # containers.run called once
    assert client.containers.run.call_count == 1
    _, kwargs = client.containers.run.call_args
    assert kwargs["image"] == cfg.task_image
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


def test_start_translates_volumes_to_host_root_when_set(
    tmp_naiw_data, mock_subprocess_run
):
    """When data_root_host is set (containerized controller path), the bind
    sources passed to `containers.run` must be rooted at host_root, NOT at the
    in-container data_root. The daemon resolves bind sources on the host's
    filesystem — passing in-container paths makes the daemon mount nonexistent
    host directories (silently creating empty ones) and breaks every task."""
    from dataclasses import replace

    client, _container = _fake_client(container_name="naiw-task-task-001")
    base = _make_cfg(tmp_naiw_data)
    # Simulate the containerized path: data_root is the in-container mount
    # target; data_root_host is the operator's real filesystem path on the host.
    cfg = replace(base, data_root_host=Path("/host/op/naiw-data"))

    lifecycle.start(
        cfg, client,
        project=None, base_ref=None, finish_policy="ask", secrets=[],
    )

    _, kwargs = client.containers.run.call_args
    vols = kwargs["volumes"]
    binds = {spec["bind"]: src for src, spec in vols.items()}

    # /work, /io, /pi-packages MUST all be rooted at host_root, not data_root.
    expected_prefix = "/host/op/naiw-data"
    for bind in ("/work", "/io", "/pi-packages"):
        assert bind in binds, f"missing bind mount {bind}; got {binds!r}"
        src = binds[bind]
        assert src.startswith(expected_prefix), (
            f"bind source for {bind} is {src!r}, expected prefix "
            f"{expected_prefix!r}; daemon-side path translation broken"
        )
        # Also confirm the in-container path is NOT leaking through.
        assert str(tmp_naiw_data) not in src, (
            f"in-container data_root {tmp_naiw_data!r} leaking into host-side "
            f"bind source {src!r}"
        )


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


def test_build_volumes_rejects_secret_traversal_via_narrow_prefix(tmp_naiw_data):
    """Defense-in-depth: even if a path-shaped secret name bypasses the CLI
    validator (programmatic caller), _build_volumes narrows the bind-source
    prefix to data_root/secrets/ so any `..` escape past the secrets dir is
    rejected by validate_bind_source before docker is touched. The pre-fix
    code used data_root as the prefix and let secrets/../<file> through."""
    # Set up the standard task skeleton + sibling files that traversal could target
    task_dir = tmp_naiw_data / "tasks" / "task-001"
    (task_dir / "work").mkdir(parents=True)
    (task_dir / "io").mkdir()
    # Create a file under data_root that traversal would point at via
    # secrets/../projects.yaml — older code mounted exactly this.
    (tmp_naiw_data / "projects.yaml").write_text("projects: {}\n", encoding="utf-8")

    with pytest.raises(BindMountEscapeError) as excinfo:
        lifecycle._build_volumes(
            tmp_naiw_data,
            task_dir,
            ["../projects.yaml"],
        )
    msg = str(excinfo.value)
    # The error must name the (narrower) secrets dir as the violated prefix —
    # NOT data_root, which would be the old broken behavior.
    assert "secrets" in msg


def test_build_volumes_accepts_real_secret_under_secrets_dir(tmp_naiw_data):
    """Sanity counterpart: a real secret file under data_root/secrets/ does
    pass validation (so the narrowed prefix did not break the happy path)."""
    task_dir = tmp_naiw_data / "tasks" / "task-001"
    (task_dir / "work").mkdir(parents=True)
    (task_dir / "io").mkdir()
    (tmp_naiw_data / "secrets" / "github_token").write_text(
        "tok", encoding="utf-8"
    )

    volumes = lifecycle._build_volumes(
        tmp_naiw_data, task_dir, ["github_token"]
    )
    secret_bind = next(
        spec for spec in volumes.values() if spec["bind"] == "/run/secrets/github_token"
    )
    assert secret_bind["mode"] == "ro"


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

    # finish must succeed without UnsupportedSchemaError (the bug we are
    # guarding against — store.read_task raises that on unknown schema).
    # The wrapper now short-circuits on terminal disk status (`failed` is
    # terminal), so finish prints "already failed; nothing to do" and the
    # on-disk status stays `failed`. The point of this regression test is
    # that finish() does NOT raise — schema-reading succeeded.
    lifecycle.finish(cfg, client, "alpha-001", policy_override="keep_worktree")

    on_disk2 = json.loads(tj.read_text(encoding="utf-8"))
    # Status preserved (idempotent on terminal); failure_reason preserved.
    assert on_disk2["status"] == "failed"
    assert "git worktree add failed" in on_disk2["failure_reason"]


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

    # finish reads schema_version=1 successfully — no UnsupportedSchemaError.
    # Under the broadened-terminal short-circuit, `failed` is idempotent.
    lifecycle.finish(cfg, client, "ghost-001", policy_override="keep_worktree")
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "failed"


# ---------- start — failure rollback -----------------------------------------


def test_start_409_conflict_produces_clean_reason_with_docker_rm_hint(
    tmp_naiw_data, capsys
):
    """When containers.run hits 409 Conflict (name already in use by an orphan),
    the failure_reason and stderr must contain a ready-to-run `docker rm -f`
    command, not raw HTTP error text."""
    client, _ = _fake_client(container_name="naiw-task-task-001")

    # Construct an APIError with status_code=409 — daemon's signal for name conflict.
    fake_response = MagicMock()
    fake_response.status_code = 409
    fake_response.text = ""
    conflict_err = docker.errors.APIError(
        "409 Client Error: Conflict",
        response=fake_response,
        explanation=(
            'Conflict. The container name "/naiw-task-task-001" is already '
            'in use by container "abc123def456..."'
        ),
    )
    client.containers.run.side_effect = conflict_err

    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
        )

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    reason = on_disk["failure_reason"]

    from naiw_tasks.docker_client import PINNED_DOCKER_API_VERSION

    # Clean reason: includes the orphan name + cleanup command, NOT raw HTTP text.
    assert "naiw-task-task-001" in reason
    assert "already in use" in reason
    # The cleanup command MUST include BOTH:
    #   - -H <proxy_url> so the command stays inside the locked-proxy boundary
    #   - DOCKER_API_VERSION=<pinned> so docker CLI does not negotiate via
    #     /_ping (blocked) and does not default to its bundled-client version
    #     (which on docker CLI 26 is 1.45+, may mismatch our 1.43 pin).
    expected_cmd = (
        f"DOCKER_API_VERSION={PINNED_DOCKER_API_VERSION} "
        f"docker -H {cfg.docker_proxy_url} rm -f naiw-task-task-001"
    )
    assert expected_cmd in reason
    assert "409 Client Error" not in reason  # raw HTTP text stripped

    # Same hint surfaces on stderr.
    captured = capsys.readouterr()
    assert expected_cmd in captured.err


def test_start_409_cleanup_hint_never_omits_proxy_h_flag(tmp_naiw_data, capsys):
    """Regression guard: the 409-conflict cleanup hint MUST always include
    BOTH `DOCKER_API_VERSION=<pinned>` env prefix AND `-H <proxy_url>` flag.

    Without -H, docker CLI bypasses the proxy (uses /var/run/docker.sock).
    Without DOCKER_API_VERSION, modern docker CLI (26+) negotiates via /_ping
    (blocked by proxy) and defaults to v1.45 paths — may mismatch daemon.
    Both must stay together; this test catches drift in either direction."""
    import re

    from naiw_tasks.docker_client import PINNED_DOCKER_API_VERSION

    client, _ = _fake_client(container_name="naiw-task-task-001")
    fake_response = MagicMock()
    fake_response.status_code = 409
    conflict_err = docker.errors.APIError(
        "409 Client Error: Conflict", response=fake_response
    )
    client.containers.run.side_effect = conflict_err

    cfg = _make_cfg(tmp_naiw_data)
    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
        )

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    reason = json.loads(tj.read_text(encoding="utf-8"))["failure_reason"]
    captured = capsys.readouterr()

    for surface_name, surface in [("failure_reason", reason), ("stderr", captured.err)]:
        # A bare "docker rm -f naiw-task-task-001" (without -H prefix) forbidden.
        bare_rm_pattern = re.compile(r"(?<!-H )docker rm -f naiw-task-task-001")
        assert not bare_rm_pattern.search(surface), (
            f"bare `docker rm -f` (no -H) in {surface_name}: {surface}"
        )
        # Pinned API version must appear next to the docker invocation.
        assert f"DOCKER_API_VERSION={PINNED_DOCKER_API_VERSION}" in surface, (
            f"DOCKER_API_VERSION=<pinned> missing from {surface_name}: {surface}"
        )


def test_start_other_api_errors_keep_raw_message(tmp_naiw_data):
    """A non-409 APIError must NOT be reframed as a name conflict — the operator
    needs to see the actual daemon message."""
    client, _ = _fake_client(container_name="naiw-task-task-001")

    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.text = ""
    server_err = docker.errors.APIError(
        "500 Server Error: out of memory",
        response=fake_response,
    )
    client.containers.run.side_effect = server_err

    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
        )

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    reason = json.loads(tj.read_text(encoding="utf-8"))["failure_reason"]
    # Raw daemon message preserved — not rewritten as a name conflict
    assert "docker rm -f" not in reason
    assert "already in use" not in reason


def test_start_aborts_when_image_digest_missing_in_attrs(tmp_naiw_data, capsys):
    """If container inspect (CONTAINERS=1 endpoint) returns no Image field,
    start must raise StartFailed — a task without resolved image digest
    cannot be audited or recovered later."""
    client, container = _fake_client(container_name="naiw-task-task-001")
    # Drop the Image key entirely — simulates a corrupt or unexpected inspect
    # response. container.reload() is a MagicMock no-op so attrs stay as set.
    container.attrs = {"State": {"Status": "running"}}

    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
        )

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert "audit digest" in on_disk["failure_reason"]

    captured = capsys.readouterr()
    assert "naiw-tasks finish task-001 --delete-worktree" in captured.err


def test_start_aborts_when_image_digest_is_empty_string(tmp_naiw_data):
    """Empty string in attrs["Image"] is also treated as unresolved (falsy check)."""
    client, container = _fake_client(container_name="naiw-task-task-001")
    container.attrs = {"State": {"Status": "running"}, "Image": ""}

    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(lifecycle.StartFailed):
        lifecycle.start(
            cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
        )

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "failed"


def test_lifecycle_does_not_access_container_image_property():
    """Regression guard: lifecycle.py MUST NOT access container.image — that
    property calls client.images.get(...) under the hood (GET /images/<id>/json),
    which the locked proxy blocks (IMAGES=0 in deploy/proxy/README.md). Digest
    must come from container.attrs["Image"] after a reload() (CONTAINERS
    endpoint, allow-listed). Static text guard mirrors test_docker_client's
    `from_env` check pattern."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "container.image" not in src, (
        "container.image access in lifecycle.py would hit /images/* "
        "(blocked by proxy IMAGES=0); use container.attrs['Image'] "
        "after reload() instead"
    )
    # Also forbid the equivalent low-level path through `client.images`.
    assert "client.images" not in src, (
        "client.images.* access would hit /images/* (blocked by proxy)"
    )


def test_start_records_attrs_image_as_digest(tmp_naiw_data):
    """Happy path: container.attrs["Image"] becomes task.image_digest in task.json."""
    client, _ = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    task = lifecycle.start(
        cfg, client, project=None, base_ref=None, finish_policy="ask", secrets=[]
    )

    assert task.image_digest == _FAKE_DIGEST
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["image_digest"] == _FAKE_DIGEST


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
        "worktree" in c and "add" in c and "agent/alpha-001" in c for c in cmds
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


def test_finish_on_failed_short_circuits(tmp_naiw_data, capsys):
    # Broadened terminal short-circuit: a task whose disk status is already
    # `failed` (terminal) is left untouched by the wrapper. The operator's
    # `naiw-tasks finish <id>` is idempotent on every terminal state, not just
    # COMPLETED. This is the contract Phase 4 D-09 needs so the lazy-event
    # tailer can pre-mark a task `failed` (via `_teardown_and_mark`) without
    # the wrapper second-guessing the teardown that already happened.
    _pre_create_task(tmp_naiw_data, "task-001", "failed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)
    lifecycle.finish(cfg, client, "task-001", policy_override=None)
    # No container ops attempted — short-circuit fired.
    container.stop.assert_not_called()
    container.remove.assert_not_called()
    captured = capsys.readouterr()
    assert "already failed" in captured.out
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    # Disk status unchanged — wrapper did not rewrite to `completed`.
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "failed"


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


def test_finish_legacy_task_falls_back_to_projects_yaml(
    tmp_naiw_data, mock_subprocess_run, capsys
):
    """task.json without project_repo_path (created on old code) must still
    teardown worktree via projects.yaml fallback. Preserves CLAUDE.md's
    'updates must not break in-flight task.json state' invariant."""
    # Repo path is read from projects.yaml (the fallback) — _make_fake_repo
    # creates BOTH the repo dir and the yaml entry, both needed.
    _make_fake_repo(tmp_naiw_data, "alpha")
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    # Pre-create task WITHOUT project_repo_path (legacy schema)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        worktree_path=str(work),
        project_repo_path=None,
    )

    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "alpha-001", policy_override="delete_worktree")

    # Worktree remove was called via fallback
    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    git_remove_calls = [
        c for c in cmds if c and c[0] == "git" and "worktree" in c and "remove" in c
    ]
    assert len(git_remove_calls) == 1, f"expected fallback to remove worktree: {cmds}"

    captured = capsys.readouterr()
    assert "legacy task.json" in captured.err
    assert "projects.yaml fallback" in captured.err

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_legacy_task_without_fallback_warns_and_completes(
    tmp_naiw_data, mock_subprocess_run, capsys
):
    """Legacy task.json + projects.yaml gone: finish prints a clear leak warning,
    skips git_ops, but still marks completed. Operator can clean manually."""
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        worktree_path=str(work),
        project_repo_path=None,
    )
    # No projects.yaml at all — fallback fails.

    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "alpha-001", policy_override="delete_worktree")

    # NO git worktree remove call
    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    git_remove_calls = [
        c for c in cmds if c and c[0] == "git" and "worktree" in c and "remove" in c
    ]
    assert git_remove_calls == [], f"unexpected remove call: {cmds}"

    captured = capsys.readouterr()
    assert "NOT removed" in captured.err
    assert "clean up manually" in captured.err

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_legacy_task_alias_removed_warns(
    tmp_naiw_data, mock_subprocess_run, capsys
):
    """Legacy task + projects.yaml present but alias removed: same leak warning."""
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        worktree_path=str(work),
        project_repo_path=None,
    )
    # projects.yaml exists but no longer lists 'alpha'
    (tmp_naiw_data / "projects.yaml").write_text(
        "projects:\n  beta:\n    path: /tmp/other\n", encoding="utf-8"
    )

    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    # The projects.load call will raise BindMountEscapeError on /tmp/other since
    # it's not under workspace/repos/. Our fallback catches ValueError (which
    # BindMountEscapeError subclasses), so we still get the leak-warning path.
    lifecycle.finish(cfg, client, "alpha-001", policy_override="delete_worktree")

    captured = capsys.readouterr()
    assert "NOT removed" in captured.err

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_marks_failed_when_container_still_running_after_remove(
    tmp_naiw_data, capsys
):
    """If stop+remove silently fail and verify shows the container still
    running, finish must NOT mark completed — it marks FAILED so the operator
    can retry (since finish short-circuits only on completed)."""
    _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")
    # Force the "remove" call to be a no-op so get keeps returning the running container.
    container.remove.side_effect = docker.errors.APIError("daemon busy")
    # Also override get's side_effect to always return the container (defeats _fake_client's
    # remove-aware get).
    client.containers.get.side_effect = lambda name: container

    cfg = _make_cfg(tmp_naiw_data)
    with pytest.raises(SystemExit) as excinfo:
        lifecycle.finish(cfg, client, "task-001", policy_override=None)
    assert excinfo.value.code == 1

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert "state='running'" in on_disk["failure_reason"]
    assert "still present" in on_disk["failure_reason"]

    captured = capsys.readouterr()
    assert "retry: naiw-tasks finish task-001" in captured.err


def test_finish_marks_failed_when_verify_call_itself_errors(
    tmp_naiw_data, capsys
):
    """If verify-step's containers.get raises APIError (e.g., proxy down between
    remove and verify), refuse to mark completed."""
    _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")
    # First call returns container (so stop/remove are attempted), second call
    # (verify) raises APIError.
    calls = {"n": 0}

    def _get(name):
        calls["n"] += 1
        if calls["n"] == 1:
            return container
        raise docker.errors.APIError("connection refused")

    client.containers.get.side_effect = _get
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(SystemExit) as excinfo:
        lifecycle.finish(cfg, client, "task-001", policy_override=None)
    assert excinfo.value.code == 1

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert "cannot verify" in on_disk["failure_reason"]


def test_finish_marks_failed_when_container_record_persists_in_exited_state(
    tmp_naiw_data, capsys
):
    """An exited container record is NOT enough to consider finish complete —
    `docker ps -a` still shows the container, the name is still reserved,
    and finish short-circuits on completed so the operator would lose the
    CLI path back to cleanup. Only NotFound counts as success."""
    _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")
    container.attrs = {"State": {"Status": "exited"}}
    # remove silently fails, daemon kept the record in exited state.
    container.remove.side_effect = docker.errors.APIError("removal blocked")
    client.containers.get.side_effect = lambda name: container

    cfg = _make_cfg(tmp_naiw_data)
    with pytest.raises(SystemExit) as excinfo:
        lifecycle.finish(cfg, client, "task-001", policy_override=None)
    assert excinfo.value.code == 1

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    # Failure reason includes the surviving state for diagnostics
    assert "state='exited'" in on_disk["failure_reason"]
    assert "after stop+remove" in on_disk["failure_reason"]

    captured = capsys.readouterr()
    assert "retry: naiw-tasks finish task-001" in captured.err


def test_finish_marks_failed_for_dead_and_created_states(tmp_naiw_data):
    """`dead` and `created` are also non-NotFound — same rule, same outcome."""
    for state in ("dead", "created"):
        _pre_create_task(
            tmp_naiw_data, f"task-{state[:3]}", "running"
        )
        client, container = _fake_client(
            container_name=f"naiw-task-task-{state[:3]}"
        )
        container.attrs = {"State": {"Status": state}}
        container.remove.side_effect = docker.errors.APIError("blocked")
        # Bind `container` explicitly to avoid Python's late-binding closure
        # capture: the lambda fires synchronously inside finish() during this
        # same loop iteration, but ruff B023 still flags lazy capture.
        client.containers.get.side_effect = (
            lambda name, container=container: container
        )

        cfg = _make_cfg(tmp_naiw_data)
        with pytest.raises(SystemExit) as excinfo:
            lifecycle.finish(
                cfg, client, f"task-{state[:3]}", policy_override=None
            )
        assert excinfo.value.code == 1

        tj = (
            tmp_naiw_data
            / "tasks"
            / f"task-{state[:3]}"
            / "meta"
            / "task.json"
        )
        on_disk = json.loads(tj.read_text(encoding="utf-8"))
        assert on_disk["status"] == "failed"
        assert f"state='{state}'" in on_disk["failure_reason"]


def test_finish_failed_by_verify_step_can_be_retried_via_helper(tmp_naiw_data, capsys):
    """After a verify-step failure leaves task in 'failed', the operator-facing
    wrapper is idempotent on terminal states (broadened short-circuit). The
    retry path goes through the shared helper `_teardown_and_mark` directly,
    which does NOT short-circuit and runs the full teardown sequence."""
    _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")

    # First attempt: stop+remove silently fail; container stays running; finish marks failed.
    container.remove.side_effect = docker.errors.APIError("daemon busy")
    client.containers.get.side_effect = lambda name: container
    cfg = _make_cfg(tmp_naiw_data)
    with pytest.raises(SystemExit):
        lifecycle.finish(cfg, client, "task-001", policy_override=None)

    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "failed"

    # Second attempt via wrapper: short-circuits (failed is terminal).
    container.remove.reset_mock()
    lifecycle.finish(cfg, client, "task-001", policy_override=None)
    container.remove.assert_not_called()
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "failed"

    # Third attempt via the shared helper: docker is healthy now, helper
    # bypasses the short-circuit, full teardown completes, status flips
    # to completed.
    container.remove.side_effect = None
    container.remove.reset_mock()

    def _get_after_recovery(name):
        if container.remove.called:
            raise docker.errors.NotFound(f"{name} removed")
        return container

    client.containers.get.side_effect = _get_after_recovery
    task_dir = tmp_naiw_data / "tasks" / "task-001"
    lifecycle._teardown_and_mark(
        cfg,
        client,
        "task-001",
        task_dir,
        terminal_status=Status.COMPLETED,
        policy_override=None,
    )

    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_skips_worktree_teardown_when_container_still_alive(
    tmp_naiw_data, mock_subprocess_run
):
    """When verify shows container still running, finish must mark failed
    WITHOUT touching the worktree — a live container may have files open."""
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

    client, container = _fake_client(container_name="naiw-task-alpha-001")
    container.remove.side_effect = docker.errors.APIError("daemon busy")
    client.containers.get.side_effect = lambda name: container
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(SystemExit):
        lifecycle.finish(cfg, client, "alpha-001", policy_override="delete_worktree")

    # git worktree remove was NOT called
    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    git_remove_calls = [
        c for c in cmds if c and c[0] == "git" and "worktree" in c and "remove" in c
    ]
    assert git_remove_calls == [], (
        f"worktree must not be torn down while container is alive: {cmds}"
    )


# ---------- _resolve_finish_policy — interactive vs non-interactive ---------


def test_resolve_finish_policy_cli_override_wins_over_task_policy(monkeypatch):
    """CLI flag wins regardless of TTY or stored policy."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert (
        lifecycle._resolve_finish_policy("keep_worktree", "ask")
        is FinishPolicy.KEEP_WORKTREE
    )
    assert (
        lifecycle._resolve_finish_policy("delete_worktree", "ask")
        is FinishPolicy.DELETE_WORKTREE
    )


def test_resolve_finish_policy_stored_non_ask_returns_as_is(monkeypatch):
    """Stored delete/keep is returned without consulting TTY or input."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert (
        lifecycle._resolve_finish_policy(None, "keep_worktree")
        is FinishPolicy.KEEP_WORKTREE
    )
    assert (
        lifecycle._resolve_finish_policy(None, "delete_worktree")
        is FinishPolicy.DELETE_WORKTREE
    )


def test_resolve_finish_policy_non_tty_ask_defaults_to_delete_with_warning(
    monkeypatch, capsys
):
    """Non-TTY + stored policy=ask: no prompt, defaults to delete_worktree,
    prints a visible note to stderr so cron logs document the choice."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    result = lifecycle._resolve_finish_policy(None, "ask")

    assert result is FinishPolicy.DELETE_WORKTREE
    captured = capsys.readouterr()
    assert "non-interactive context" in captured.err
    assert "delete_worktree" in captured.err
    assert "--keep-worktree" in captured.err


def test_resolve_finish_policy_tty_ask_prompts_yes(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert (
        lifecycle._resolve_finish_policy(None, "ask") is FinishPolicy.KEEP_WORKTREE
    )


def test_resolve_finish_policy_tty_ask_prompts_no(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert (
        lifecycle._resolve_finish_policy(None, "ask") is FinishPolicy.DELETE_WORKTREE
    )


def test_resolve_finish_policy_tty_ask_default_is_no(monkeypatch):
    """Empty answer / non-y answer defaults to delete (the explicit prompt is [y/N])."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert (
        lifecycle._resolve_finish_policy(None, "ask") is FinishPolicy.DELETE_WORKTREE
    )


def test_finish_non_tty_ask_policy_deletes_worktree_integration(
    tmp_naiw_data, mock_subprocess_run, capsys, monkeypatch
):
    """Integration: task with finish_policy=ask, finish called without override
    in non-TTY context — worktree removed, stderr documents the choice."""
    repo = _make_fake_repo(tmp_naiw_data, "alpha")
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    _pre_create_task(
        tmp_naiw_data,
        "alpha-001",
        "running",
        kind="project",
        project="alpha",
        finish_policy="ask",
        worktree_path=str(work),
        project_repo_path=str(repo),
    )

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "alpha-001", policy_override=None)

    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    git_remove_calls = [
        c for c in cmds if c and c[0] == "git" and "worktree" in c and "remove" in c
    ]
    assert len(git_remove_calls) == 1

    captured = capsys.readouterr()
    assert "non-interactive context" in captured.err

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_marks_failed_when_git_worktree_remove_fails(
    tmp_naiw_data, mock_subprocess_run, capsys
):
    """If git worktree remove --force returns non-zero on an existing worktree,
    finish must surface as FAILED — operator asked for delete_worktree, disk
    state is dirty, marking completed would hide the dirty state behind the
    short-circuit-on-completed rule."""
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

    # Git worktree remove --force returns non-zero (locked branch, etc.)
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="fatal: worktree is locked: contains uncommitted changes\n",
    )

    client, _ = _fake_client(container_name="naiw-task-alpha-001")
    cfg = _make_cfg(tmp_naiw_data)

    with pytest.raises(SystemExit) as excinfo:
        lifecycle.finish(
            cfg, client, "alpha-001", policy_override="delete_worktree"
        )
    assert excinfo.value.code == 1

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert "git worktree remove failed" in on_disk["failure_reason"]
    # First line of git stderr surfaced
    assert "worktree is locked" in on_disk["failure_reason"]

    captured = capsys.readouterr()
    assert "retry: naiw-tasks finish alpha-001" in captured.err

    # Worktree dir intentionally left on disk for operator inspection
    assert work.exists()


def test_finish_tolerates_already_missing_work_path(
    tmp_naiw_data, mock_subprocess_run
):
    """If the worktree was already deleted out-of-band, finish completes
    cleanly: git_ops.worktree_remove skips the remove call (path doesn't
    exist on disk), runs prune for metadata cleanup, task marked completed."""
    repo = _make_fake_repo(tmp_naiw_data, "alpha")
    # Note: NO work dir created.
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
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

    # Only prune was called (remove was skipped because work_path missing).
    cmds = [list(c.args[0]) for c in mock_subprocess_run.call_args_list]
    git_calls = [c for c in cmds if c and c[0] == "git"]
    assert len(git_calls) == 1
    assert "prune" in git_calls[0]
    assert "remove" not in git_calls[0]

    tj = tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


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
    from unittest.mock import MagicMock as _MM

    import docker.errors as _de
    import naiw_tasks.lifecycle as _lifecycle
    from naiw_tasks.config import Config as _Config

    data_root = Path(data_root_str)
    cfg = _Config(data_root=data_root)
    client = _MM()
    container = _MM()
    container.name = f"naiw-task-{task_id}"
    container.attrs = {"State": {"Status": "running"}}

    # Mirror real Docker: after remove, get raises NotFound — needed by the
    # finish() verify step.
    def _get(name):
        if container.remove.called:
            raise _de.NotFound(f"{name} removed")
        return container

    client.containers.get = _MM(side_effect=_get)
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


# ---------- finish — broadened terminal short-circuit -----------------------


def test_finish_already_completed_short_circuits_with_friendly_message(
    tmp_naiw_data, capsys
):
    _pre_create_task(tmp_naiw_data, "task-001", "completed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    client.containers.get.assert_not_called()
    container.stop.assert_not_called()
    container.remove.assert_not_called()
    captured = capsys.readouterr()
    assert "already completed" in captured.out
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_already_failed_short_circuits_with_friendly_message(
    tmp_naiw_data, capsys
):
    _pre_create_task(tmp_naiw_data, "task-001", "failed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    client.containers.get.assert_not_called()
    container.stop.assert_not_called()
    container.remove.assert_not_called()
    captured = capsys.readouterr()
    assert "already failed" in captured.out
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "failed"


def test_finish_already_cancelled_short_circuits_with_friendly_message(
    tmp_naiw_data, capsys
):
    _pre_create_task(tmp_naiw_data, "task-001", "cancelled")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    client.containers.get.assert_not_called()
    container.stop.assert_not_called()
    container.remove.assert_not_called()
    captured = capsys.readouterr()
    assert "already cancelled" in captured.out
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "cancelled"


def test_finish_on_interrupted_runs_full_teardown(tmp_naiw_data):
    # `interrupted` is NOT terminal — the operator can still finish an
    # interrupted task (per the permissive matrix). The wrapper must NOT
    # short-circuit; full stop+remove+verify+atomic mark-completed runs.
    _pre_create_task(tmp_naiw_data, "task-001", "interrupted")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "completed"
    assert on_disk["finished_at"] is not None


def test_finish_on_waiting_for_user_runs_full_teardown(tmp_naiw_data):
    _pre_create_task(tmp_naiw_data, "task-001", "waiting_for_user")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "completed"
    assert on_disk["finished_at"] is not None


# ---------- finish(force=True) — operator recovery hatch --------------------


def test_finish_force_on_failed_re_runs_teardown_preserving_failed_status(
    tmp_naiw_data,
):
    # The B1 recovery scenario: auto_finish wrote `failed` to disk but the
    # container survived. Operator types `naiw-tasks finish <id> --force`.
    # Expected: teardown runs again; status stays `failed` (the operator's
    # audit trail of WHY this task is failed is not overwritten with
    # `completed`).
    _pre_create_task(tmp_naiw_data, "task-001", "failed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None, force=True)

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert on_disk["finished_at"] is not None


def test_finish_force_on_cancelled_re_runs_teardown_preserving_cancelled(
    tmp_naiw_data,
):
    _pre_create_task(tmp_naiw_data, "task-001", "cancelled")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None, force=True)

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "cancelled"


def test_finish_force_on_completed_re_runs_teardown_preserving_completed(
    tmp_naiw_data,
):
    _pre_create_task(tmp_naiw_data, "task-001", "completed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None, force=True)

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_force_on_running_behaves_like_normal_finish(tmp_naiw_data):
    # `--force` is only meaningful for terminal statuses (where the short-
    # circuit would otherwise trip). On a `running` task it is a no-op —
    # full teardown runs and status flips to `completed` exactly as without
    # the flag. Locking this in prevents future drift where `--force` might
    # be interpreted as "preserve current status no matter what."
    _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None, force=True)

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = tmp_naiw_data / "tasks" / "task-001" / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_finish_force_default_false_preserves_short_circuit(tmp_naiw_data, capsys):
    # Belt-and-braces: confirm the default value of `force` matches the
    # pre-Phase-4-recovery behaviour. A call site that does not pass `force`
    # must still hit the short-circuit on terminal statuses.
    _pre_create_task(tmp_naiw_data, "task-001", "failed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle.finish(cfg, client, "task-001", policy_override=None)

    container.stop.assert_not_called()
    container.remove.assert_not_called()
    captured = capsys.readouterr()
    assert "already failed" in captured.out


# ---------- _teardown_and_mark — shared helper contract ----------------------


def test_teardown_and_mark_writes_completed_status(tmp_naiw_data):
    task_dir = _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle._teardown_and_mark(
        cfg,
        client,
        "task-001",
        task_dir,
        terminal_status=Status.COMPLETED,
        policy_override=None,
    )

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = task_dir / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "completed"
    assert on_disk["finished_at"] is not None
    assert on_disk["updated_at"] is not None


def test_teardown_and_mark_writes_failed_status(tmp_naiw_data):
    # The same teardown sequence (stop+remove+verify+optional worktree) runs
    # whether the caller asks for COMPLETED or FAILED as the terminal state.
    # The lazy-event tailer will call with FAILED on a `fail` event with
    # auto_finish=true.
    task_dir = _pre_create_task(tmp_naiw_data, "task-001", "running")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle._teardown_and_mark(
        cfg,
        client,
        "task-001",
        task_dir,
        terminal_status=Status.FAILED,
        policy_override=None,
    )

    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = task_dir / "meta" / "task.json"
    on_disk = json.loads(tj.read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert on_disk["finished_at"] is not None


def test_teardown_and_mark_does_not_short_circuit_on_completed_disk_state(
    tmp_naiw_data,
):
    # The helper is the lazy-event-tailer's entry point — its caller has
    # already decided teardown is needed (e.g., the tailer pre-flipped status
    # in a race with another process, or the helper is invoked unconditionally
    # for a `done` event). The helper must NOT inspect the on-disk status to
    # short-circuit; that decision belongs to the wrapper.
    task_dir = _pre_create_task(tmp_naiw_data, "task-001", "completed")
    client, container = _fake_client(container_name="naiw-task-task-001")
    cfg = _make_cfg(tmp_naiw_data)

    lifecycle._teardown_and_mark(
        cfg,
        client,
        "task-001",
        task_dir,
        terminal_status=Status.COMPLETED,
        policy_override=None,
    )

    # Container ops DID run (proves no short-circuit).
    container.stop.assert_called_once()
    container.remove.assert_called_once()
    tj = task_dir / "meta" / "task.json"
    assert json.loads(tj.read_text(encoding="utf-8"))["status"] == "completed"


def test_teardown_and_mark_applies_delete_worktree_policy(
    tmp_naiw_data, mock_subprocess_run
):
    repo = _make_fake_repo(tmp_naiw_data, "alpha")
    work = tmp_naiw_data / "tasks" / "alpha-001" / "work"
    work.mkdir(parents=True)
    task_dir = _pre_create_task(
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

    lifecycle._teardown_and_mark(
        cfg,
        client,
        "alpha-001",
        task_dir,
        terminal_status=Status.COMPLETED,
        policy_override="delete_worktree",
    )

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


# ---------- source-level invariants -----------------------------------------


def test_lifecycle_finish_delegates_to_teardown_and_mark():
    import re as _re

    src_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "lifecycle.py"
    )
    src = src_path.read_text(encoding="utf-8")

    # _teardown_and_mark is defined at module scope.
    assert _re.search(r"^def _teardown_and_mark\(", src, _re.MULTILINE), (
        "_teardown_and_mark function definition missing from lifecycle.py"
    )

    # Extract finish() body — bytes from `^def finish(` up to the next
    # top-level `^def `.
    lines = src.splitlines()
    finish_start = None
    finish_end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("def finish("):
            finish_start = i
        elif finish_start is not None and line.startswith("def "):
            finish_end = i
            break
    assert finish_start is not None, "finish() function missing from lifecycle.py"
    finish_body = "\n".join(lines[finish_start:finish_end])

    assert "_teardown_and_mark(" in finish_body, (
        "finish() wrapper must call _teardown_and_mark(); body was:\n"
        f"{finish_body}"
    )
    assert "terminal_status=Status.COMPLETED" in finish_body, (
        "finish() wrapper must invoke _teardown_and_mark with "
        "terminal_status=Status.COMPLETED"
    )


def test_lifecycle_terminal_statuses_set_has_three_values():
    src_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "lifecycle.py"
    )
    src = src_path.read_text(encoding="utf-8")

    # The constant exists.
    assert "TERMINAL_STATUSES" in src, (
        "TERMINAL_STATUSES constant missing from lifecycle.py"
    )

    # All three terminal Status members referenced (either via Status.X or as
    # the literal string value — accept both shapes).
    has_completed = (
        "Status.COMPLETED" in src or '"completed"' in src or "'completed'" in src
    )
    has_failed = (
        "Status.FAILED" in src or '"failed"' in src or "'failed'" in src
    )
    has_cancelled = (
        "Status.CANCELLED" in src
        or '"cancelled"' in src
        or "'cancelled'" in src
    )
    assert has_completed and has_failed and has_cancelled, (
        "TERMINAL_STATUSES must cover all three terminal states "
        "(completed, failed, cancelled)"
    )


def test_lifecycle_module_has_no_gsd_refs():
    import re as _re

    src_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "lifecycle.py"
    )
    src = src_path.read_text(encoding="utf-8")
    forbidden = [
        r"\bD-\d{2}\b",
        r"\bPhase [0-9]",
        r"\bRESEARCH\b",
        r"\bPlan [0-9]",
        r"\bLIST-\d{2}\b",
        r"\bSIG-\d{2}\b",
        r"\bDATA-\d{2}\b",
        r"\bCTRL-\d{2}\b",
    ]
    for pattern in forbidden:
        match = _re.search(pattern, src)
        assert match is None, (
            f"GSD/planning ref {match.group()!r} leaked into lifecycle.py — "
            f"strip the comment (allowed-doc files only)"
        )
