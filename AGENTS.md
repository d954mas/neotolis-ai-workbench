# AGENTS.md

NAIW — personal self-hosted manager for Pi tasks in isolated Docker containers. Single user.

## Principles

- KISS, DRY. Simple beats clever.
- Minimum code, files, dependencies. No premature abstractions.
- Updates don't break running tasks or state.
- Human-readable configs, visible behavior.

## Don'ts

- AI stays in its task container — no host, root, or other tasks.
- Sandbox isolation, not in-code validation (cap-drop, mounts, network).
- No "what" comments. Only "why" if non-obvious.
