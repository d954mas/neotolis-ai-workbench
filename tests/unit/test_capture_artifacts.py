"""Tests for lifecycle._capture_artifacts (finish-time artifact bundle).

Six tests cover the contract:
- All six artifact items land for a project task.
- Generic tasks skip the three git-driven captures.
- Pi-planted symlinks under io/output/ are refused with WARN, NOT followed.
- Hard-link is used when same-filesystem.
- Copy-via-O_NOFOLLOW is the EXDEV fallback.
- Capture failures (e.g., bad base_commit) log WARN and do NOT raise.
"""

import errno
import logging
import os
import subprocess
from pathlib import Path

from naiw_tasks.lifecycle import _capture_artifacts


def _make_git_repo(path: Path) -> str:
    """Create a tiny repo at path with one base commit + a change.

    Returns the base commit sha so callers can pass it as `base_commit`.
    """
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "base"], cwd=path,
        check=True, env=env,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path,
        capture_output=True, text=True, check=True, env=env,
    ).stdout.strip()
    # Add a follow-up commit so diff is non-empty.
    (path / "new.txt").write_text("new\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add new.txt"], cwd=path,
        check=True, env=env,
    )
    return sha


def _make_task_dir(tmp_path: Path) -> Path:
    td = tmp_path / "tasks" / "t-001"
    (td / "meta").mkdir(parents=True)
    (td / "io").mkdir()
    (td / "io" / "output").mkdir()
    (td / "io" / "terminal.log").write_text("hello\nworld\n")
    (td / "io" / "summary.md").write_text("# Summary\n")
    (td / "io" / "output" / "a.txt").write_text("apple\n")
    (td / "io" / "output" / "b").mkdir()
    (td / "io" / "output" / "b" / "c.txt").write_text("cherry\n")
    return td


def test_capture_writes_all_six_artifacts_for_project_task(tmp_path):
    td = _make_task_dir(tmp_path)
    work = tmp_path / "work"
    base = _make_git_repo(work)
    data = {
        "kind": "project",
        "base_commit": base,
        "worktree_path": str(work),
    }
    _capture_artifacts(td, data, work)
    art = td / "meta" / "artifacts"
    assert (art / "terminal.log").read_text() == "hello\nworld\n"
    assert (art / "summary.md").read_text() == "# Summary\n"
    assert (art / "output" / "a.txt").read_text() == "apple\n"
    assert (art / "output" / "b" / "c.txt").read_text() == "cherry\n"
    assert (art / "git-status.txt").exists()
    assert (art / "changed-files.txt").exists()
    assert (art / "diff.patch").exists()
    # diff.patch should mention the file added between base..HEAD
    assert "new.txt" in (art / "diff.patch").read_text()
    # changed-files.txt is name-status form; new.txt should appear there too
    assert "new.txt" in (art / "changed-files.txt").read_text()


def test_capture_overwrites_stale_output_on_retry(tmp_path):
    """Repeated capture must not leave stale files in meta/artifacts/output.
    Earlier code used os.link which raises EEXIST on the second call, so
    the new file was skipped and the OLD one survived. Worse, files
    deleted from io/output between the two captures also survived.
    """
    td = _make_task_dir(tmp_path)
    # First capture — full set.
    _capture_artifacts(td, {"kind": "generic"}, None)
    art = td / "meta" / "artifacts" / "output"
    assert (art / "a.txt").read_text() == "apple\n"
    assert (art / "b" / "c.txt").read_text() == "cherry\n"

    # Mutate io/output/ between captures:
    #  - change a.txt content (Pi rewrote the file)
    #  - delete b/c.txt (Pi removed the output)
    #  - add d.txt (Pi created a new output)
    (td / "io" / "output" / "a.txt").write_text("apricot\n")
    (td / "io" / "output" / "b" / "c.txt").unlink()
    (td / "io" / "output" / "d.txt").write_text("dragon\n")

    # Second capture — meta/artifacts/output must reflect the NEW state.
    _capture_artifacts(td, {"kind": "generic"}, None)
    assert (art / "a.txt").read_text() == "apricot\n", (
        "stale a.txt — second capture did not overwrite"
    )
    assert not (art / "b" / "c.txt").exists(), (
        "stale c.txt — file deleted from io/output survived in artifacts"
    )
    assert (art / "d.txt").read_text() == "dragon\n", (
        "new d.txt not captured on retry"
    )


def test_capture_includes_uncommitted_changes_and_untracked(tmp_path):
    """Pi typically edits files without committing. `git diff <base>..HEAD`
    would lose every uncommitted change; the artifact would only show
    committed-on-top-of-base diffs. We compare base to working tree and
    append untracked files via git ls-files --others.
    """
    td = _make_task_dir(tmp_path)
    work = tmp_path / "work"
    base = _make_git_repo(work)
    # After the helper repo's second commit, modify a committed file
    # WITHOUT committing, and create a brand-new untracked file.
    (work / "README.md").write_text("base\nedit by Pi\n")
    (work / "agent-scratch.txt").write_text("agent created me\n")
    data = {
        "kind": "project",
        "base_commit": base,
        "worktree_path": str(work),
    }
    _capture_artifacts(td, data, work)
    art = td / "meta" / "artifacts"
    diff = (art / "diff.patch").read_text()
    changed = (art / "changed-files.txt").read_text()
    # Uncommitted edit must show up in the patch.
    assert "edit by Pi" in diff, (
        f"uncommitted edit lost from diff.patch: {diff!r}"
    )
    # And in changed-files.txt as a modified line.
    assert "README.md" in changed
    # Untracked file is appended with the `??` tag.
    assert "agent-scratch.txt" in changed
    assert "??\tagent-scratch.txt" in changed, (
        f"untracked must carry ?? tag: {changed!r}"
    )


def test_generic_task_skips_git_artifacts(tmp_path):
    td = _make_task_dir(tmp_path)
    data = {"kind": "generic"}
    _capture_artifacts(td, data, None)
    art = td / "meta" / "artifacts"
    assert (art / "terminal.log").exists()
    assert (art / "summary.md").exists()
    assert (art / "output" / "a.txt").exists()
    assert not (art / "git-status.txt").exists()
    assert not (art / "changed-files.txt").exists()
    assert not (art / "diff.patch").exists()


def test_capture_refuses_pi_planted_symlink(tmp_path, caplog):
    td = _make_task_dir(tmp_path)
    # Pi-planted symlink under io/output/ — must NOT be exfiltrated.
    evil_target = tmp_path / "host-secret.txt"
    evil_target.write_text("host secret\n")
    (td / "io" / "output" / "evil").symlink_to(evil_target)
    with caplog.at_level(logging.WARNING, logger="naiw_tasks"):
        _capture_artifacts(td, {"kind": "generic"}, None)
    art = td / "meta" / "artifacts" / "output"
    # The symlink must NOT have been copied/linked into the bundle.
    assert not (art / "evil").exists()
    # And the warning must have fired.
    assert any(
        "evil" in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_output_capture_uses_hardlink_when_same_filesystem(tmp_path):
    td = _make_task_dir(tmp_path)
    _capture_artifacts(td, {"kind": "generic"}, None)
    src = td / "io" / "output" / "a.txt"
    dst = td / "meta" / "artifacts" / "output" / "a.txt"
    assert dst.stat().st_ino == src.stat().st_ino
    assert dst.stat().st_nlink >= 2


def test_output_capture_falls_back_to_copy_on_exdev(tmp_path, monkeypatch):
    td = _make_task_dir(tmp_path)

    def fail_link(src, dst):  # noqa: ARG001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    # Patch os.link as seen by the lifecycle module.
    from naiw_tasks import lifecycle as lifecycle_mod
    monkeypatch.setattr(lifecycle_mod.os, "link", fail_link)
    _capture_artifacts(td, {"kind": "generic"}, None)
    src = td / "io" / "output" / "a.txt"
    dst = td / "meta" / "artifacts" / "output" / "a.txt"
    assert dst.read_text() == "apple\n"
    # Copied (not linked) — inodes differ.
    assert dst.stat().st_ino != src.stat().st_ino


def test_capture_failure_does_not_block_teardown(tmp_path, caplog):
    td = _make_task_dir(tmp_path)
    # Project task with a bogus base_commit makes git diff fail.
    work = tmp_path / "work"
    _make_git_repo(work)
    data = {
        "kind": "project",
        "base_commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "worktree_path": str(work),
    }
    with caplog.at_level(logging.WARNING, logger="naiw_tasks"):
        # Must not raise.
        _capture_artifacts(td, data, work)
    # git-status.txt always writes (status doesn't need base_commit).
    assert (td / "meta" / "artifacts" / "git-status.txt").exists()
    # A WARNING about a non-zero git result MUST have fired.
    assert any(
        "non-zero" in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]
