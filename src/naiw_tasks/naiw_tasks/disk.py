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

Linux-only: `-b` is a GNU coreutils extension. BSD/macOS `du` will
reject it. NAIW's controller is Linux-only per CLAUDE.md; for BSD dev
boxes use `brew install coreutils` and PATH `gdu`, or run via WSL.
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import click

from naiw_tasks.config import Config
from naiw_tasks.format import humanize_iec_bytes

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


def du_sb(path: Path) -> tuple[int, bool]:
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


def _compute_rows(cfg: Config) -> tuple[list[_Row], int]:
    rows: list[_Row] = []
    total = 0
    for name in _SUBDIR_ORDER:
        p = cfg.data_root / name
        size, partial = du_sb(p)
        rows.append(_Row(name=name, bytes_=size, partial=partial))
        total += size
    return rows, total


def threshold(cfg: Config) -> tuple[int, int, float]:
    """Return (used_bytes, max_bytes, percent).

    Public hook consumed by startup_checks for the >95% start refusal.
    Returns pct=0.0 when max_data_size <= 0 (treated as "gate disabled"
    by callers — config._parse_max_data_size enforces > 0 for operator
    config files, but Python-API callers can still pass 0/negative).
    """
    _rows, used = _compute_rows(cfg)
    if cfg.max_data_size <= 0:
        return (used, cfg.max_data_size, 0.0)
    return (used, cfg.max_data_size, 100.0 * used / cfg.max_data_size)


def run(cfg: Config) -> int:
    """Print breakdown to stdout. Returns 0."""
    rows, total = _compute_rows(cfg)
    partial_rows = [r for r in rows if r.partial]
    # Column widths
    name_w = max(len(r.name) for r in rows) + 2
    for r in rows:
        tag = " (partial)" if r.partial else ""
        click.echo(
            f"  {r.name.ljust(name_w)}{humanize_iec_bytes(r.bytes_)}{tag}"
        )
    click.echo(
        f"  {'TOTAL'.ljust(name_w)}{humanize_iec_bytes(total)}"
    )
    # Loud notice when any subdir was partial — total understates real usage
    # and the threshold warning below may not fire even when the operator
    # is actually near the cap. Stronger than the per-row "(partial)" tag.
    if partial_rows:
        partial_names = ", ".join(r.name for r in partial_rows)
        click.echo(
            f"\nNOTE: {len(partial_rows)} subdir(s) returned partial size "
            f"({partial_names}) — TOTAL may understate actual usage; "
            f"check permissions / unmounted volumes."
        )
    # Host disk free — informational, NOT part of threshold math.
    try:
        usage = shutil.disk_usage(cfg.data_root)
        click.echo(
            f"\n  host disk @ {cfg.data_root}: "
            f"total={humanize_iec_bytes(usage.total)} "
            f"used={humanize_iec_bytes(usage.used)} "
            f"free={humanize_iec_bytes(usage.free)}"
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
            f"({humanize_iec_bytes(total)} / "
            f"{humanize_iec_bytes(cfg.max_data_size)}). "
            f"Run `naiw-tasks clean --older-than 30d` to reclaim space."
        )
    return 0
