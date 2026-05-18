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


def worktree_prune(repo: Path) -> None:
    """Best-effort `git worktree prune` against the base repo.

    Used after host-side removal of a task folder when the worktree subdirectory
    has already been torn down out-of-band — keeps the base repo's worktree
    bookkeeping clean. Non-zero exits and missing repos are tolerated (the
    operator may have deleted the base repo entirely); failures are silent so
    callers can call this unconditionally before removing a task folder.
    """
    if not repo.exists():
        return
    _git(repo, "worktree", "prune")


def worktree_remove(repo: Path, work_path: Path) -> None:
    """Force-remove worktree + prune metadata.

    Tolerant of out-of-band deletion: if `work_path` no longer exists on disk,
    git's "is not a working tree" exit is allowed and we proceed to prune.
    Otherwise, a non-zero exit from `git worktree remove --force` is a real
    failure (locked branch, inaccessible repo, etc.) and is raised so
    callers do not silently mark the task completed with a dirty disk state.

    Never raw recursive-delete the worktree path — git's bookkeeping must
    stay consistent, and only the two-step git-driven teardown preserves it.
    """
    if work_path.exists():
        result = _git(repo, "worktree", "remove", "--force", str(work_path))
        if result.returncode != 0:
            raise GitWorktreeError(
                f"git worktree remove --force failed "
                f"(exit {result.returncode}) for {work_path}",
                stderr=result.stderr,
            )
    # prune is best-effort metadata cleanup. If it fails the worktree-add
    # bookkeeping might be slightly stale, but the next remove/add cycle will
    # reconcile. Don't fail the whole operation on this.
    _git(repo, "worktree", "prune")
