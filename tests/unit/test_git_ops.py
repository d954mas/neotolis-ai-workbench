"""Tests for naiw_tasks.git_ops — subprocess argv shape assertions.

Every test uses the mock_subprocess_run fixture from conftest.py; no real git
is invoked. The only filesystem read is the no-rm-rf source-scan test.
"""

import subprocess
from pathlib import Path

import naiw_tasks.git_ops as git_ops
import pytest
from naiw_tasks.git_ops import (
    DEFAULT_BASE_CANDIDATES,
    GitWorktreeError,
    resolve_base,
    worktree_add,
    worktree_remove,
)

# ---------------------------------------------------------------------------
# resolve_base
# ---------------------------------------------------------------------------


def test_resolve_base_explicit_returns_input(mock_subprocess_run):
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="abc1234\n", stderr=""
    )

    sym, sha = resolve_base(Path("/repo"), "feature/x")

    assert (sym, sha) == ("feature/x", "abc1234")
    assert mock_subprocess_run.call_count == 1
    args = mock_subprocess_run.call_args_list[0].args[0]
    assert args == ["git", "-C", "/repo", "rev-parse", "--verify", "--short", "feature/x"]


def test_resolve_base_default_tries_in_order(mock_subprocess_run):
    mock_subprocess_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="bad"),
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="bad"),
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="bad"),
        subprocess.CompletedProcess(args=[], returncode=0, stdout="deadbee\n", stderr=""),
    ]

    sym, sha = resolve_base(Path("/repo"), None)

    assert (sym, sha) == ("master", "deadbee")
    assert mock_subprocess_run.call_count == 4
    refs_tried = [c.args[0][-1] for c in mock_subprocess_run.call_args_list]
    assert refs_tried == ["origin/main", "origin/master", "main", "master"]


def test_resolve_base_default_falls_back_to_head(mock_subprocess_run):
    mock_subprocess_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=0, stdout="cafef00\n", stderr=""),
    ]

    sym, sha = resolve_base(Path("/repo"), None)

    assert (sym, sha) == ("HEAD", "cafef00")
    assert mock_subprocess_run.call_count == 5


def test_resolve_base_raises_when_nothing_resolves(mock_subprocess_run):
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr="bad"
    )

    with pytest.raises(GitWorktreeError) as excinfo:
        resolve_base(Path("/repo"), None)

    assert "no base ref resolvable" in str(excinfo.value)
    assert mock_subprocess_run.call_count == len(DEFAULT_BASE_CANDIDATES)


# ---------------------------------------------------------------------------
# worktree_add
# ---------------------------------------------------------------------------


def test_worktree_add_branch_is_agent_prefix(mock_subprocess_run):
    worktree_add(
        Path("/repo"),
        "myproj-001",
        Path("/tasks/myproj-001/work"),
        "abc1234",
    )

    assert mock_subprocess_run.call_count == 1
    args = mock_subprocess_run.call_args_list[0].args[0]
    assert args == [
        "git",
        "-C",
        "/repo",
        "worktree",
        "add",
        "-b",
        "agent/myproj-001",
        "/tasks/myproj-001/work",
        "abc1234",
    ]
    # Explicit literal-prefix sanity check
    assert "agent/myproj-001" in args
    assert "-b" in args


def test_worktree_add_passes_encoding_utf8_errors_replace(mock_subprocess_run):
    worktree_add(
        Path("/repo"), "myproj-001", Path("/tasks/myproj-001/work"), "abc1234"
    )

    kwargs = mock_subprocess_run.call_args_list[0].kwargs
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_worktree_add_raises_with_stderr_verbatim(mock_subprocess_run):
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=128,
        stdout="",
        stderr="fatal: a branch named 'agent/foo-001' already exists\n",
    )

    with pytest.raises(GitWorktreeError) as excinfo:
        worktree_add(
            Path("/repo"), "foo-001", Path("/tasks/foo-001/work"), "abc1234"
        )

    assert (
        excinfo.value.stderr
        == "fatal: a branch named 'agent/foo-001' already exists\n"
    )


# ---------------------------------------------------------------------------
# worktree_remove
# ---------------------------------------------------------------------------


def test_worktree_remove_calls_force_then_prune_when_work_exists(
    mock_subprocess_run, tmp_path
):
    """Happy path: work dir exists on disk → git remove --force, then prune."""
    work = tmp_path / "work"
    work.mkdir()

    worktree_remove(Path("/repo"), work)

    assert mock_subprocess_run.call_count == 2
    first = mock_subprocess_run.call_args_list[0].args[0]
    second = mock_subprocess_run.call_args_list[1].args[0]
    assert first == [
        "git",
        "-C",
        "/repo",
        "worktree",
        "remove",
        "--force",
        str(work),
    ]
    assert second == ["git", "-C", "/repo", "worktree", "prune"]


def test_worktree_remove_skips_remove_when_work_path_missing(
    mock_subprocess_run, tmp_path
):
    """Out-of-band deletion tolerance: if the work dir is already gone on disk,
    skip the remove call (git would refuse with 'is not a working tree') and
    only run prune so the .git/worktrees admin record is cleaned up."""
    missing = tmp_path / "already-deleted"
    # Don't create it.

    worktree_remove(Path("/repo"), missing)

    # Only `git worktree prune` should have run.
    assert mock_subprocess_run.call_count == 1
    args = mock_subprocess_run.call_args_list[0].args[0]
    assert args == ["git", "-C", "/repo", "worktree", "prune"]


def test_worktree_remove_raises_when_remove_fails_on_existing_path(
    mock_subprocess_run, tmp_path
):
    """If work_path exists on disk but git remove --force fails (locked branch,
    inaccessible repo, etc.), worktree_remove must raise GitWorktreeError so
    the caller does NOT silently mark the task completed with a dirty disk."""
    work = tmp_path / "work"
    work.mkdir()

    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="fatal: worktree is locked\n",
    )

    with pytest.raises(GitWorktreeError) as excinfo:
        worktree_remove(Path("/repo"), work)

    assert "git worktree remove --force failed" in str(excinfo.value)
    assert excinfo.value.stderr == "fatal: worktree is locked\n"


def test_worktree_remove_prune_failure_is_best_effort(
    mock_subprocess_run, tmp_path
):
    """If prune itself fails, worktree_remove must not raise — prune is metadata
    cleanup and stale entries get reconciled on the next add/remove cycle."""
    missing = tmp_path / "already-gone"
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr="prune failed"
    )

    # Must not raise even though prune returned non-zero.
    worktree_remove(Path("/repo"), missing)


# ---------------------------------------------------------------------------
# Source-policy regression: never rm -rf
# ---------------------------------------------------------------------------


def test_no_rm_rf_anywhere_in_module():
    module_path = Path(git_ops.__file__).resolve()
    text = module_path.read_text(encoding="utf-8")
    assert "rm -rf" not in text
    assert "shutil.rmtree" not in text
    assert "os.removedirs" not in text
