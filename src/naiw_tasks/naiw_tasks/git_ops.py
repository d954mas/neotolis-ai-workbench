"""subprocess wrappers around `git` — worktree add/remove + base ref resolution.

Why subprocess not GitPython: GitPython itself shells out, so we get the same
behaviour with one less dependency. encoding="utf-8" + errors="replace" makes
stderr capture stable on minimal-locale CI images (LANG=C decodes git output
through locale.getpreferredencoding() which can be ANSI_X3.4-1968).
"""

import subprocess
from pathlib import Path

DEFAULT_BASE_CANDIDATES: tuple[str, ...] = (
    "origin/main",
    "origin/master",
    "main",
    "master",
    "HEAD",
)


class GitWorktreeError(RuntimeError):
    """Wraps a non-zero git exit. .stderr holds git's verbatim stderr."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def _git(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def resolve_base(repo: Path, explicit: str | None) -> tuple[str, str]:
    """Return (symbolic_ref, short_sha).

    Explicit ref is used verbatim; default tries DEFAULT_BASE_CANDIDATES in order.
    """
    candidates: tuple[str, ...] = (explicit,) if explicit else DEFAULT_BASE_CANDIDATES
    for ref in candidates:
        if ref is None:
            continue
        result = _git(repo, "rev-parse", "--verify", "--short", ref)
        if result.returncode == 0:
            return ref, result.stdout.strip()
    raise GitWorktreeError(
        f"no base ref resolvable in {repo}; tried: {list(candidates)}"
    )


def worktree_add(
    repo: Path,
    task_id: str,
    work_path: Path,
    base_ref: str,
) -> None:
    """Create a worktree at work_path on branch agent/<task_id> from base_ref."""
    # Branch literal lives in code — the API shape enforces it; no caller can
    # pass a custom branch.
    branch = f"agent/{task_id}"
    result = _git(
        repo,
        "worktree",
        "add",
        "-b",
        branch,
        str(work_path),
        base_ref,
    )
    if result.returncode != 0:
        raise GitWorktreeError(
            f"git worktree add failed (exit {result.returncode}) for "
            f"branch {branch!r} at {work_path}",
            stderr=result.stderr,
        )


def worktree_remove(repo: Path, work_path: Path) -> None:
    """Force-remove worktree + prune. Tolerant of out-of-band deletion."""
    # remove --force first (idempotent against missing worktree); then prune
    # drops the .git/worktrees/<id>/ admin entry left behind by --force.
    # Never raw recursive-delete the worktree path — git's bookkeeping must
    # stay consistent, and only the two-step git-driven teardown preserves it.
    _git(repo, "worktree", "remove", "--force", str(work_path))
    _git(repo, "worktree", "prune")
