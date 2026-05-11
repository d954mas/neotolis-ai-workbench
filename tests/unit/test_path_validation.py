"""Tests for naiw_tasks.path_validation — bind-mount source defence."""

import os
import sys
from pathlib import Path

import pytest

from naiw_tasks.path_validation import BindMountEscapeError, validate_bind_source


def test_validate_bind_source_accepts_path_under_root(tmp_naiw_data: Path) -> None:
    src = tmp_naiw_data / "tasks"
    resolved = validate_bind_source(src, tmp_naiw_data)
    assert resolved == src.resolve(strict=True)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX symlink semantics — Windows symlinks need elevation/dev-mode",
)
def test_symlink_escape_rejected(tmp_naiw_data: Path, tmp_path: Path) -> None:
    target = Path("/etc")
    if not target.exists():
        pytest.skip("/etc not present on this system")
    evil = tmp_path / "evil"
    os.symlink(target, evil)
    with pytest.raises(BindMountEscapeError) as exc:
        validate_bind_source(evil, tmp_naiw_data)
    msg = str(exc.value)
    assert "evil" in msg or str(evil) in msg
    # Resolved path is part of the diagnostic message.
    assert "/etc" in msg or "resolved" in msg


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX symlink semantics — Windows symlinks need elevation/dev-mode",
)
def test_intermediate_symlink_rejected(tmp_naiw_data: Path, tmp_path: Path) -> None:
    target = Path("/etc")
    if not target.exists():
        pytest.skip("/etc not present on this system")
    alt = tmp_path / "alt"
    os.symlink(target, alt)
    with pytest.raises(BindMountEscapeError):
        validate_bind_source(alt / "passwd", tmp_naiw_data)


def test_missing_source_rejected(tmp_naiw_data: Path) -> None:
    with pytest.raises(BindMountEscapeError) as exc:
        validate_bind_source(tmp_naiw_data / "nope" / "noway", tmp_naiw_data)
    assert "does not exist" in str(exc.value) or "missing" in str(exc.value)


def test_stricter_prefix_under_workspace_repos(tmp_naiw_data: Path) -> None:
    workspace_repos = tmp_naiw_data / "workspace" / "repos"
    # tasks/ is under data_root but NOT under workspace/repos/ — escape.
    tasks_x = tmp_naiw_data / "tasks"
    with pytest.raises(BindMountEscapeError):
        validate_bind_source(tasks_x, tmp_naiw_data, prefix=workspace_repos)

    # A path under workspace/repos/ — accepted.
    repo = workspace_repos / "myrepo"
    repo.mkdir()
    resolved = validate_bind_source(repo, tmp_naiw_data, prefix=workspace_repos)
    assert resolved == repo.resolve(strict=True)


def test_data_root_itself_is_accepted(tmp_naiw_data: Path) -> None:
    # data_root itself is_relative_to data_root (Python 3.9+ semantics).
    resolved = validate_bind_source(tmp_naiw_data, tmp_naiw_data)
    assert resolved == tmp_naiw_data.resolve(strict=True)


def test_string_path_accepted(tmp_naiw_data: Path) -> None:
    # Caller may pass str, Path, or PathLike — all must work.
    resolved = validate_bind_source(str(tmp_naiw_data / "tasks"), str(tmp_naiw_data))
    assert resolved == (tmp_naiw_data / "tasks").resolve(strict=True)
