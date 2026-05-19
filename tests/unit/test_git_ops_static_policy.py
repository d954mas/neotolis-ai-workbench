"""GIT-04 source-text guard.

Mirror of tests/unit/test_docker_client.py::test_no_docker_from_env_in_module.
The substrings forbidden here all include a space — bare `in` check is
sufficient; no regex/word-boundary needed.

Future PRs that introduce ``git push`` / ``git commit`` / ``git merge`` to
``git_ops.py`` or ``lifecycle.py`` will fail CI here.

GIT-04 contract:
- The NAIW controller is a deterministic provisioner. It never invokes git
  to commit, push, or merge on the operator's or Pi's behalf.
- Pi inside the container MAY stage and commit when explicitly instructed
  in the task — that is a Pi behaviour, not a controller behaviour.
- See ``docs/git-policy.md`` for the operator-facing narrative.
"""
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "naiw_tasks" / "naiw_tasks"
)
_FORBIDDEN_TOKENS: tuple[str, ...] = ("git push", "git commit", "git merge")


def test_git_ops_has_no_forbidden_git_operations() -> None:
    """GIT-04: controller never auto-commits, pushes, or merges on Pi's behalf."""
    src = (_SRC / "git_ops.py").read_text(encoding="utf-8")
    for tok in _FORBIDDEN_TOKENS:
        assert tok not in src, (
            f"git_ops.py contains forbidden token {tok!r} (GIT-04 contract violation)"
        )


def test_lifecycle_has_no_forbidden_git_operations() -> None:
    """GIT-04: lifecycle.py never auto-commits, pushes, or merges."""
    src = (_SRC / "lifecycle.py").read_text(encoding="utf-8")
    for tok in _FORBIDDEN_TOKENS:
        assert tok not in src, (
            f"lifecycle.py contains forbidden token {tok!r} (GIT-04 contract violation)"
        )
