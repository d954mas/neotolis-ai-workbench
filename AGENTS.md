# AGENTS.md

NAIW - personal self-hosted manager for Pi tasks in isolated Docker containers. Single user.

## Principles

- KISS, DRY. Simple beats clever.
- Minimum code, files, dependencies. No premature abstractions.
- Updates don't break running tasks or state.
- Human-readable configs, visible behavior.

## Comment Policy

- Comments are for invariants, tradeoffs, and non-obvious failure modes.
- Do not narrate what the code already says.
- Do not copy rationale across files. Put it at the boundary that enforces it.
- Prefer deleting stale comments over updating prose around simple code.

## Don'ts

- AI stays in its task container - no host, root, or other tasks.
- Sandbox isolation, not in-code validation (cap-drop, mounts, network).
- No "what" comments. Only "why" if non-obvious.
