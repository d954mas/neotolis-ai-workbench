"""naiw-tasks disk — per-subdirectory size of ~/naiw-data/ + threshold math.

Per-subdir order (locked): projects.yaml, secrets/, pi-packages/,
workspace/repos/, tasks/. After the per-subdir rows, prints a total line
and a host-disk-free informational line (NOT part of threshold math).

Thresholds:
  - >80% of max_data_size: append a WARNING line suggesting clean --older-than 30d.
  - >95% of max_data_size: enforced separately by startup_checks at start.

All du calls are 'du -sb' (apparent size in bytes; no -L so symlink loops
do not recurse). Non-zero exit is treated as partial — the row is annotated
(partial) and the parse falls through on any stdout that's available.
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import click

from naiw_tasks.config import Config

_LOG = logging.getLogger("naiw_tasks")

# Locked order — operator's mental model for what consumes space.
_SUBDIR_ORDER: tuple[str, ...] = (
    "projects.yaml",
    "secrets",
    "pi-packages",
    "workspace/repos",
    "tasks",
)


@dataclass(frozen=True)
class _Row:
    name: str
    bytes_: int
    partial: bool


def _du_sb(path: Path) -> tuple[int, bool]:
    """Return (bytes, partial). partial=True on non-zero exit OR no stdout."""
    if not path.exists():
        return (0, False)
    result = subprocess.run(
        ["du", "-sb", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if not result.stdout:
        return (0, True)
    try:
        head = result.stdout.split("\t", 1)[0].strip()
        size = int(head)
    except (ValueError, IndexError):
        return (0, True)
    partial = result.returncode != 0
    if partial:
        _LOG.info(
            "disk: partial du for %s (rc=%d): %s",
            path, result.returncode, result.stderr.strip(),
        )
    return (size, partial)


def _humanize_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(f)}{unit}"
            return f"{f:.2f}{unit}"
        f /= 1024.0
    return f"{f:.2f}TiB"  # unreachable


def _compute_rows(cfg: Config) -> tuple[list[_Row], int]:
    rows: list[_Row] = []
    total = 0
    for name in _SUBDIR_ORDER:
        p = cfg.data_root / name
        size, partial = _du_sb(p)
        rows.append(_Row(name=name, bytes_=size, partial=partial))
        total += size
    return rows, total


def _check_threshold(cfg: Config) -> tuple[int, int, float]:
    """Return (used_bytes, max_bytes, percent).

    Used by startup_checks for the >95% start refusal.
    """
    _rows, used = _compute_rows(cfg)
    if cfg.max_data_size <= 0:
        return (used, cfg.max_data_size, 0.0)
    return (used, cfg.max_data_size, 100.0 * used / cfg.max_data_size)


def run(cfg: Config) -> int:
    """Print breakdown to stdout. Returns 0."""
    rows, total = _compute_rows(cfg)
    # Column widths
    name_w = max(len(r.name) for r in rows) + 2
    for r in rows:
        tag = " (partial)" if r.partial else ""
        click.echo(
            f"  {r.name.ljust(name_w)}{_humanize_bytes(r.bytes_)}{tag}"
        )
    click.echo(
        f"  {'TOTAL'.ljust(name_w)}{_humanize_bytes(total)}"
    )
    # Host disk free — informational, NOT part of threshold math.
    try:
        usage = shutil.disk_usage(cfg.data_root)
        click.echo(
            f"\n  host disk @ {cfg.data_root}: "
            f"total={_humanize_bytes(usage.total)} "
            f"used={_humanize_bytes(usage.used)} "
            f"free={_humanize_bytes(usage.free)}"
        )
    except OSError as exc:
        _LOG.warning(
            "disk: cannot stat host disk at %s: %s", cfg.data_root, exc
        )

    # Threshold math against max_data_size.
    pct = 100.0 * total / cfg.max_data_size if cfg.max_data_size > 0 else 0.0
    if pct > 80.0:
        click.echo(
            f"\nWARNING: NAIW data at {pct:.1f}% of max_data_size "
            f"({_humanize_bytes(total)} / "
            f"{_humanize_bytes(cfg.max_data_size)}). "
            f"Run `naiw-tasks clean --older-than 30d` to reclaim space."
        )
    return 0
