"""Tests for naiw_tasks.git_ops — subprocess argv shape assertions.

Every test uses the mock_subprocess_run fixture from conftest.py; no real git
is invoked. The only filesystem read is the no-rm-rf source-scan test.
"""

import subprocess
from pathlib import Path

import pytest

import naiw_tasks.git_ops as git_ops
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


def test_worktree_remove_calls_force_then_prune(mock_subprocess_run):
    worktree_remove(Path("/repo"), Path("/tasks/foo/work"))

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
        "/tasks/foo/work",
    ]
    assert second == ["git", "-C", "/repo", "worktree", "prune"]


def test_worktree_remove_tolerates_nonzero_exit(mock_subprocess_run):
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr="not a working tree"
    )

    # Must not raise — out-of-band deletion is acceptable.
    result = worktree_remove(Path("/repo"), Path("/tasks/foo/work"))
    assert result is None
    assert mock_subprocess_run.call_count == 2


# ---------------------------------------------------------------------------
# Source-policy regression: never rm -rf
# ---------------------------------------------------------------------------


def test_no_rm_rf_anywhere_in_module():
    module_path = Path(git_ops.__file__).resolve()
    text = module_path.read_text(encoding="utf-8")
    assert "rm -rf" not in text
    assert "shutil.rmtree" not in text
    assert "os.removedirs" not in text
