# Git policy (controller side)

The NAIW controller is a deterministic provisioner. It creates worktrees,
starts containers, captures artefacts, and tears down. It does NOT
commit, push, or merge on the operator's or Pi's behalf.

## Contract

1. **No automatic commit.** The controller never invokes ``git ... commit``
   or any equivalent. Pi inside the container MAY stage and record changes
   when explicitly instructed in the task — that is an operator-chosen Pi
   behaviour, not a controller behaviour.

2. **No automatic push.** The controller never invokes ``git ... push``.
   Uploading to the remote is a manual operator action — either by the
   operator on the host or by Pi inside the container, when explicitly
   instructed in the task.

3. **No automatic merge.** The controller never invokes ``git ... merge``.
   Merging is an operator action.

4. **Branch naming.** The controller creates worktrees ONLY on branches
   matching ``agent/<task-id>`` (existing GIT-01 contract). Any other
   branch name is refused at worktree-add time.

## Live guard

The contract above is enforced as code by
`tests/unit/test_git_ops_static_policy.py`. The test reads the source of
`src/naiw_tasks/naiw_tasks/git_ops.py` and
`src/naiw_tasks/naiw_tasks/lifecycle.py` and asserts that the literal
substrings ``git push``, ``git commit``, and ``git merge`` do not appear.
A future PR that introduces any of those tokens will fail CI.

The test mirrors the long-standing static-source-text guard for the
Docker-from-env ban (`tests/unit/test_docker_client.py::test_no_docker_from_env_in_module`).
It is intentionally low-tech — a substring scan over two files — so the
guarantee survives refactors, code-mode changes, and any future expansion
of the controller's git surface.

## GitHub-side enforcement

See `docs/github-bot.md` for the recommended GitHub Ruleset + App scopes
that complement this controller-side contract. The ruleset is the
GitHub-side trust boundary: the controller-side contract (this file +
the live guard) guarantees the controller never tries to push or merge;
the ruleset guarantees that even if a future bug or compromised Pi
attempts it, GitHub itself refuses writes to protected paths.

## Cross-references

- Requirement: `GIT-04` (see `.planning/REQUIREMENTS.md`)
- Static guard: `tests/unit/test_git_ops_static_policy.py`
- Operator-facing GitHub setup: `docs/github-bot.md`
- Trust boundary architectural view: `README.md` (Trust boundary section)
