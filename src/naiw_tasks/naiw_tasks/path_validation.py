"""Bind-mount source defence: every source must resolve under data_root.

Mitigates the source-symlink-to-/etc class of attack. Docker dereferences
bind-mount source paths on the host side at mount-create time, so the only
safe contract is to resolve the path with strict=True and require it to live
under the data root (or a stricter prefix supplied by the caller — used for
projects.yaml entries which must be under workspace/repos/).
"""

from pathlib import Path


class BindMountEscapeError(ValueError):
    """A bind-mount source path escapes the allowed prefix."""


def validate_bind_source(
    source: Path | str,
    data_root: Path | str,
    prefix: Path | str | None = None,
) -> Path:
    """Resolve `source` and assert it is under `prefix` (defaults to `data_root`).

    Raises BindMountEscapeError on missing source, broken symlink target, or
    any escape. Returns the resolved path on success.
    """
    src = Path(source)
    root = Path(data_root)
    pfx = Path(prefix) if prefix is not None else root

    try:
        resolved = src.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        resolved_pfx = pfx.resolve(strict=True)
    except FileNotFoundError as exc:
        raise BindMountEscapeError(
            f"bind mount source does not exist or has missing link target: "
            f"{src} ({exc})"
        ) from exc

    if not resolved.is_relative_to(resolved_pfx):
        raise BindMountEscapeError(
            f"bind mount source escapes prefix:\n"
            f"  source:        {src}\n"
            f"  resolved:      {resolved}\n"
            f"  required pfx:  {resolved_pfx}\n"
            f"  data root:     {resolved_root}"
        )
    return resolved
