"""Consolidated health-check umbrella for `naiw-tasks doctor`.

Two entry points:
  run_global(cfg, client)            -- argument-less form; runs the four
                                        startup_checks probes, the disk
                                        threshold reporter, and a drift
                                        audit across every running task.
  run_per_task(cfg, client, task_id) -- per-task form; reports the drift
                                        diff for ONE task only.

Exit codes (operator-facing):
  0  clean
  1  drift detected OR disk used > 80% of max_data_size (informational)
  2  startup-check failed (precedence over drift — startup safety wins)
  3  invalid or unknown task id (per-task form only)
"""

import click

from naiw_tasks import disk as disk_mod
from naiw_tasks import drift as drift_mod
from naiw_tasks import startup_checks
from naiw_tasks.config import Config
from naiw_tasks.format import humanize_iec_bytes
from naiw_tasks.ids import validate_task_id
from naiw_tasks.model import Status
from naiw_tasks.store import read_task

_LABEL_FILTER = {"label": "naiw.managed=1"}


def _format_bytes(n: int) -> str:
    """IEC-binary humaniser shared with disk + startup_checks output."""
    return humanize_iec_bytes(n)


def _format_max(n: int) -> str:
    if n <= 0:
        return "<unlimited>"
    return _format_bytes(n)


def run_global(cfg: Config, client) -> int:
    """Argument-less doctor.

    Order is fixed and load-bearing: startup_checks must report (and gate
    on failure) BEFORE we attempt any further work, because a broken
    proxy/data-root would make the disk + drift sections meaningless.
    """
    click.echo("[startup_checks]")
    try:
        startup_checks.run_all(cfg, client, gate_disk_threshold=False)
        click.echo("  OK")
    except startup_checks.StartupCheckFailed:
        # StartupCheckFailed.__init__ has already written to stderr; we
        # surface the failure as exit 2 and skip the rest of the audit.
        return 2

    click.echo("")
    click.echo("[disk]")
    used, max_bytes, pct = disk_mod.threshold(cfg)
    click.echo(
        f"  used={_format_bytes(used)} "
        f"max={_format_max(max_bytes)} "
        f"pct={pct:.1f}%"
    )
    disk_warn = pct > 80.0

    click.echo("")
    click.echo("[drift]")
    any_drift = False
    containers = client.containers.list(all=True, filters=_LABEL_FILTER)
    by_task_id = {}
    for c in containers:
        labels = getattr(c, "labels", None) or {}
        tid = labels.get("naiw.task-id")
        if tid:
            by_task_id[tid] = c

    tasks_dir = cfg.data_root / "tasks"
    if tasks_dir.is_dir():
        for task_dir in sorted(tasks_dir.glob("*")):
            if not task_dir.is_dir():
                continue
            try:
                task = read_task(task_dir)
            except Exception:
                # Read-only audit — a malformed task.json is reported
                # elsewhere (list_cmd); doctor stays silent rather than
                # double-noising the operator.
                continue
            if str(task.get("status")) != str(Status.RUNNING):
                continue
            container = by_task_id.get(task.get("id"))
            if container is None:
                continue
            attrs = getattr(container, "attrs", {}) or {}
            state = attrs.get("State", {}) or {}
            if state.get("Status") != "running":
                continue
            host_config = attrs.get("HostConfig", {}) or {}
            ctr_config = attrs.get("Config", {}) or {}
            items = drift_mod.compute_drift(
                host_config,
                ctr_config,
                expected_storage_bind=drift_mod.expected_storage_bind(
                    cfg, task.get("id") or task_dir.name,
                ),
            )
            if items:
                any_drift = True
                click.echo(f"  {task.get('id')}:")
                for it in items:
                    field = getattr(it, "field", "?")
                    expected = getattr(it, "expected", "?")
                    actual = getattr(it, "actual", "?")
                    severity = getattr(it, "severity", "?")
                    click.echo(
                        f"    {field}: expected={expected!r} "
                        f"actual={actual!r} severity={severity}"
                    )

    if not any_drift:
        click.echo("  no drift")

    return 1 if (any_drift or disk_warn) else 0


def run_per_task(cfg: Config, client, task_id: str) -> int:
    """Per-task doctor: drift diff only.

    Exit codes: 0 clean, 1 drift OR container unavailable, 3 invalid id.
    """
    try:
        validate_task_id(task_id)
    except ValueError as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        return 3
    task_dir = cfg.data_root / "tasks" / task_id
    if not task_dir.is_dir():
        click.echo(f"naiw-tasks: task {task_id!r} not found", err=True)
        return 3

    try:
        container = client.containers.get(f"naiw-task-{task_id}")
    except Exception as exc:
        click.echo(
            f"naiw-tasks: cannot inspect container for {task_id}: {exc}",
            err=True,
        )
        return 1

    attrs = getattr(container, "attrs", {}) or {}
    host_config = attrs.get("HostConfig", {}) or {}
    ctr_config = attrs.get("Config", {}) or {}
    items = drift_mod.compute_drift(
        host_config,
        ctr_config,
        expected_storage_bind=drift_mod.expected_storage_bind(cfg, task_id),
    )
    if not items:
        click.echo(f"{task_id}: no drift")
        return 0
    for it in items:
        field = getattr(it, "field", "?")
        expected = getattr(it, "expected", "?")
        actual = getattr(it, "actual", "?")
        severity = getattr(it, "severity", "?")
        click.echo(
            f"{task_id} {field}: expected={expected!r} "
            f"actual={actual!r} severity={severity}"
        )
    return 1
