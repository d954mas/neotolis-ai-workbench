"""projects.yaml: safe_load + alias-only schema + workspace/repos/ prefix check.

Why fail-fast at load time: deep `git worktree add` errors hide the real
problem (operator added an alias but never cloned, or pointed at a path
outside the workspace prefix). validate_bind_source raises before any
container/git call so the operator sees the correct cause.
"""

import os
from pathlib import Path

import yaml

from naiw_tasks.path_validation import validate_bind_source

ALLOWED_PROJECT_KEYS: frozenset[str] = frozenset({"path"})


def _resolve_project_path(raw_path: str, data_root: Path) -> Path:
    """Map a projects.yaml `path:` value (operator-typed, host-conventional)
    to a controller-visible Path under data_root.

    The operator edits projects.yaml on the host and writes paths in host
    conventions: `~/naiw-data/...`, `$NAIW_DATA_HOST/...`, or a plain absolute
    host path under their data root. The containerized controller cannot
    expand `~` (its HOME points at the in-container tmpfs, not the operator's
    real home) and cannot see arbitrary host paths (only data_root is
    bind-mounted). So we translate these host-side conventions to in-container
    paths before validate_bind_source resolves them.

    Rules:
      - `~/naiw-data` / `~/naiw-data/...` → data_root[/...] regardless of HOME.
        Operator's intent is "wherever my data root is"; same intent as
        `NAIW_DATA` env override. We do not invoke expanduser, so an in-container
        HOME mismatch cannot break the resolution.
      - Absolute path under `NAIW_DATA_HOST` (set by compose) → translate the
        prefix to data_root. Covers operators who write fully-resolved host paths.
      - Anything else → pass through to Path() (relative paths resolve against
        the controller's CWD which is data_root; absolute in-container paths
        work directly; absolute paths outside data_root are caught by the
        validate_bind_source prefix check downstream).
    """
    if raw_path == "~/naiw-data":
        return data_root
    if raw_path.startswith("~/naiw-data/"):
        return data_root / raw_path[len("~/naiw-data/"):]

    host_root_env = os.environ.get("NAIW_DATA_HOST")
    if host_root_env:
        host_root = Path(host_root_env)
        try:
            rel = Path(raw_path).relative_to(host_root)
        except ValueError:
            pass
        else:
            return data_root / rel

    return Path(raw_path)


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
        # Translate host-side path conventions (`~/naiw-data/...`,
        # `$NAIW_DATA_HOST/...`) to in-container paths under data_root before
        # validate_bind_source resolves them. See _resolve_project_path docstring
        # for the full mapping. Plain expanduser() would expand `~` against the
        # controller's tmpfs HOME and break operator-written yaml inside the
        # container.
        configured_path = _resolve_project_path(raw_path, data_root)
        # Stricter prefix than ordinary bind mounts: project paths must live
        # under workspace/repos/, not just under the data root.
        resolved = validate_bind_source(configured_path, data_root, prefix=prefix)
        out[alias] = {"path": str(resolved)}
    return out
