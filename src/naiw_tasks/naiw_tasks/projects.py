"""projects.yaml: safe_load + alias-only schema + workspace/repos/ prefix check.

Why fail-fast at load time: deep `git worktree add` errors hide the real
problem (operator added an alias but never cloned, or pointed at a path
outside the workspace prefix). validate_bind_source raises before any
container/git call so the operator sees the correct cause.
"""

from pathlib import Path

import yaml

from naiw_tasks.path_validation import validate_bind_source

ALLOWED_PROJECT_KEYS: frozenset[str] = frozenset({"path"})


def load(yaml_path: Path, data_root: Path) -> dict[str, dict]:
    """Parse projects.yaml and validate every entry against the strict schema.

    Each project's `path` must resolve under data_root/workspace/repos/.

    Raises:
        FileNotFoundError: yaml file does not exist.
        ValueError: yaml is unparseable, schema invalid, or any project's path
            fails bind-source validation. yaml.YAMLError is wrapped in ValueError
            so callers do not need to depend on yaml internals.
    """
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"projects.yaml: cannot parse {yaml_path} ({exc})"
        ) from exc
    if not isinstance(raw, dict) or "projects" not in raw:
        raise ValueError(
            f"projects.yaml: missing top-level 'projects:' key in {yaml_path}"
        )
    projects = raw["projects"]
    if not isinstance(projects, dict):
        raise ValueError(
            f"projects.yaml: 'projects' must be a mapping, "
            f"got {type(projects).__name__}"
        )

    prefix = data_root / "workspace" / "repos"
    out: dict[str, dict] = {}
    for alias, spec in projects.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"projects.yaml: project {alias!r} must be a mapping, "
                f"got {type(spec).__name__}"
            )
        unknown = set(spec.keys()) - ALLOWED_PROJECT_KEYS
        if unknown:
            raise ValueError(
                f"projects.yaml: project {alias!r} has unknown keys "
                f"{sorted(unknown)} (supported: {sorted(ALLOWED_PROJECT_KEYS)})"
            )
        if "path" not in spec:
            raise ValueError(
                f"projects.yaml: project {alias!r} missing required 'path'"
            )
        # Reject non-string `path:` values (yaml `null`, ints, lists) with a
        # ValueError BEFORE Path() — Path(None)/Path(123) would raise raw
        # TypeError, which lifecycle.start's except clause does not catch
        # (it only handles ValueError/FileNotFoundError from projects.load),
        # so the operator would see a Python traceback and the prewritten
        # task.json would stay in `created` instead of being moved to `failed`.
        raw_path = spec["path"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(
                f"projects.yaml: project {alias!r} 'path' must be a non-empty "
                f"string, got {type(raw_path).__name__}: {raw_path!r}"
            )
        # Expand `~` BEFORE validate_bind_source. `Path.resolve(strict=True)`
        # treats `~` as a literal directory component, so the shipped
        # `scripts/projects.yaml.example` (which uses `~/naiw-data/...`)
        # would otherwise be rejected even though it points at the right
        # location after shell-style tilde expansion.
        configured_path = Path(raw_path).expanduser()
        # Stricter prefix than ordinary bind mounts: project paths must live
        # under workspace/repos/, not just under the data root.
        resolved = validate_bind_source(configured_path, data_root, prefix=prefix)
        out[alias] = {"path": str(resolved)}
    return out
