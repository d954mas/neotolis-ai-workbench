"""naiw-tasks clean — disk reclaim for terminal-state tasks + orphan prune.

Invariants:
  - Only terminal statuses (completed/failed/cancelled) are candidates.
  - For project tasks, `git worktree prune` runs AFTER shutil.rmtree so
    the now-orphan entry actually gets removed from the base repo.
  - Always prompts unless --yes; default-on-Enter is N (cancel).
  - Orphan re-enumeration after per-task removal catches cascades.
"""

import datetime as dt
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import click
import docker.errors

from naiw_tasks import disk as disk_mod
from naiw_tasks import git_ops, store
from naiw_tasks.config import Config
from naiw_tasks.format import humanize_iec_bytes

_LOG = logging.getLogger("naiw_tasks")

_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}
_OLDER_THAN_PATTERN = re.compile(r"^(\d+)([smhdw])$")

_TERMINAL_STATES: frozenset[str] = frozenset({
    "completed", "failed", "cancelled",
})


def parse_older_than(raw: str) -> dt.timedelta:
    """Parse <int><unit> where unit is one of s/m/h/d/w. Single-unit only.

    Raises ValueError on malformed input with a clear hint about units.
    """
    value = raw.strip() if isinstance(raw, str) else ""
    m = _OLDER_THAN_PATTERN.match(value)
    if not m:
        raise ValueError(
            f"--older-than={raw!r}: expected <integer><unit> where "
            f"unit is one of s,m,h,d,w (e.g. '30d', '2w', '1h')"
        )
    n = int(m.group(1))
    unit = m.group(2)
    return dt.timedelta(seconds=n * _UNIT_SECONDS[unit])


@dataclass(frozen=True)
class _Candidate:
    task_id: str
    task_dir: Path
    status: str
    reference_ts: str
    age: dt.timedelta
    size_bytes: int
    kind: str
    project_repo_path: str | None
    worktree_path: str | None


def _humanize_age(age: dt.timedelta) -> str:
    total = int(age.total_seconds())
    if total >= 86400:
        return f"{total // 86400}d"
    if total >= 3600:
        return f"{total // 3600}h"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def _dir_size_bytes(p: Path) -> int:
    """Apparent size of a task dir, in bytes. Used only for the preview
    line in the clean prompt (`tX  finished_at=... 1.2GiB`).

    Delegates to disk.du_sb for consistency with `naiw-tasks disk` — that
    way the size printed by `disk` for tasks/ as a whole and the per-task
    size printed by `clean` agree on apparent-size semantics. partial=True
    (du returned non-zero) is treated as 0 here: the preview line is
    informational, the real risk is `disk` understating the total, which
    is already handled with a loud NOTE.
    """
    size, _partial = disk_mod.du_sb(p)
    return size


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_iso(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _enumerate_candidates(
    cfg: Config, threshold: dt.timedelta,
) -> list[_Candidate]:
    tasks_dir = cfg.data_root / "tasks"
    if not tasks_dir.is_dir():
        return []
    now = _now()
    out: list[_Candidate] = []
    for entry in sorted(tasks_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            data = store.read_task(entry)
        except (FileNotFoundError, store.UnsupportedSchemaError, ValueError) as exc:
            _LOG.warning(
                "clean: skipping %s: cannot read task.json: %s",
                entry.name, exc,
            )
            continue
        status = str(data.get("status", ""))
        if status not in _TERMINAL_STATES:
            continue
        ref_ts = data.get("finished_at") or data.get("updated_at")
        if not ref_ts:
            _LOG.warning(
                "clean: skipping %s: terminal status %r but no "
                "finished_at/updated_at",
                entry.name, status,
            )
            continue
        ref_dt = _parse_iso(ref_ts)
        if ref_dt is None:
            _LOG.warning(
                "clean: skipping %s: bad ISO timestamp %r",
                entry.name, ref_ts,
            )
            continue
        # Treat naive timestamps as UTC so comparison against the timezone-aware
        # `now` never raises TypeError on legacy task.json files.
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=dt.UTC)
        age = now - ref_dt
        if age < threshold:
            continue
        out.append(_Candidate(
            task_id=entry.name,
            task_dir=entry,
            status=status,
            reference_ts=ref_ts,
            age=age,
            size_bytes=_dir_size_bytes(entry),
            kind=str(data.get("kind", "")),
            project_repo_path=data.get("project_repo_path"),
            worktree_path=data.get("worktree_path"),
        ))
    return out


def _container_state(c) -> str:
    """Return the docker State.Status string ('running', 'exited', etc.) or
    '' on missing/malformed attrs.
    """
    return str(c.attrs.get("State", {}).get("Status") or "").lower()


def _enumerate_orphans(client, data_root: Path) -> list:
    """Return docker.models.containers.Container instances labelled
    naiw.managed=1 whose task folder is missing.

    `client=None` returns []: graceful-degraded mode used when Docker is
    unreachable and the caller still wants to do disk-side cleanup.
    """
    if client is None:
        return []
    try:
        all_managed = client.containers.list(
            all=True, filters={"label": "naiw.managed=1"},
        )
    except docker.errors.DockerException as exc:
        _LOG.warning("clean: cannot list managed containers: %s", exc)
        return []
    orphans = []
    tasks_dir = data_root / "tasks"
    for c in all_managed:
        labels = c.attrs.get("Config", {}).get("Labels") or {}
        tid = labels.get("naiw.task-id")
        if not tid:
            continue
        if not (tasks_dir / tid).exists():
            orphans.append(c)
    return orphans


def run(
    cfg: Config,
    client,
    older_than: dt.timedelta,
    dry_run: bool = False,
    skip_prompt: bool = False,
) -> int:
    """Execute clean. Returns exit code (0 = success, 1 = >=1 per-task failure).

    `client=None` skips the orphan-container scan + prune (graceful-degrade
    when Docker is unreachable) and prints a clear notice so the operator
    knows orphans were not checked. Disk-side per-task removal still runs.
    """
    candidates = _enumerate_candidates(cfg, older_than)
    orphans = _enumerate_orphans(client, cfg.data_root)
    docker_skipped = client is None

    if not candidates and not orphans:
        if docker_skipped:
            click.echo("no terminal-state tasks older than threshold "
                       "(orphan-container scan skipped: docker unreachable)")
        else:
            click.echo("no tasks to clean")
        return 0

    if dry_run:
        click.echo(
            f"DRY RUN -- would remove {len(candidates)} tasks "
            f"(no changes made):"
        )
        for c in candidates:
            click.echo(
                f"  {c.task_id}  finished_at={c.reference_ts} "
                f"({_humanize_age(c.age)} ago)  "
                f"{humanize_iec_bytes(c.size_bytes)}"
            )
        if orphans:
            running_count = sum(1 for c in orphans if _container_state(c) == "running")
            warn_suffix = (
                f" — {running_count} STILL RUNNING; force-removal will kill them"
                if running_count > 0 else ""
            )
            click.echo(
                f"DRY RUN -- would prune {len(orphans)} orphan "
                f"container(s){warn_suffix}:"
            )
            for c in orphans:
                labels = c.attrs.get("Config", {}).get("Labels") or {}
                state = _container_state(c)
                state_tag = "  [RUNNING]" if state == "running" else f"  [{state}]"
                click.echo(
                    f"  {c.name} (task-id={labels.get('naiw.task-id')}){state_tag}"
                )
        elif docker_skipped:
            click.echo(
                "  (orphan-container scan skipped: docker unreachable)"
            )
        return 0

    if candidates:
        click.echo(f"Will remove {len(candidates)} tasks:")
        for c in candidates:
            click.echo(
                f"  {c.task_id}  finished_at={c.reference_ts} "
                f"({_humanize_age(c.age)} ago)  "
                f"{humanize_iec_bytes(c.size_bytes)}"
            )

    if orphans:
        running_count = sum(
            1 for c in orphans if _container_state(c) == "running"
        )
        warn_suffix = (
            f" — {running_count} STILL RUNNING; force-removal will kill them"
            if running_count > 0 else ""
        )
        click.echo(
            f"Will prune {len(orphans)} orphan container(s){warn_suffix}:"
        )
        for c in orphans:
            labels = c.attrs.get("Config", {}).get("Labels") or {}
            state = _container_state(c)
            state_tag = "  [RUNNING]" if state == "running" else f"  [{state}]"
            click.echo(
                f"  {c.name} (task-id={labels.get('naiw.task-id')}){state_tag}"
            )

    # Single prompt covers BOTH tasks and orphans. Previous version only
    # gated on candidates — an empty-candidate run with orphan containers
    # would force-kill them without confirmation, contradicting the
    # "always prompts unless --yes" policy.
    if not skip_prompt:
        actions: list[str] = []
        if candidates:
            actions.append(f"remove {len(candidates)} tasks")
        if orphans:
            actions.append(f"prune {len(orphans)} orphan container(s)")
        if not click.confirm(
            "proceed to " + " AND ".join(actions) + "?",
            default=False,
        ):
            click.echo("aborted")
            return 0

    failures = 0
    for c in candidates:
        try:
            shutil.rmtree(c.task_dir)
            click.echo(f"removed {c.task_id}")
        except OSError as exc:
            failures += 1
            _LOG.warning(
                "clean: rmtree failed for %s: %s",
                c.task_dir, exc,
            )
            click.echo(
                f"naiw-tasks: clean: failed to remove "
                f"{c.task_id}: {exc}",
                err=True,
            )
            # Skip prune when rmtree failed: the worktree dir is still on
            # disk, so prune would be a no-op anyway, and we want a retry
            # of clean to attempt the rmtree path again before disturbing
            # repo metadata.
            continue
        if c.kind == "project" and c.project_repo_path:
            # Prune AFTER rmtree, not before: `git worktree prune` removes
            # entries whose worktree dir is gone. If we prune first the
            # dir is still there and prune is a no-op; the result is a
            # stale entry in the base repo pointing at a non-existent
            # path. Pruning after means the operator's `git worktree list`
            # in the base repo stays clean.
            try:
                git_ops.worktree_prune(Path(c.project_repo_path))
            except OSError as exc:
                # OSError covers FileNotFoundError (git binary missing) and
                # PermissionError (repo not readable). worktree_prune itself
                # uses subprocess check=False, so non-zero exits don't raise.
                _LOG.info(
                    "clean: worktree prune failed for %s: %s "
                    "(rmtree already succeeded; stale repo metadata possible)",
                    c.task_id, exc,
                )

    # Orphan prune AFTER per-task removal so just-removed tasks become
    # orphans for this same invocation. Re-enumerate to capture both.
    final_orphans = _enumerate_orphans(client, cfg.data_root)
    for c in final_orphans:
        state = _container_state(c)
        labels = c.attrs.get("Config", {}).get("Labels") or {}
        # Loud notice on stderr BEFORE the force-remove for a running orphan:
        # this is destructive (kills a running process) and the operator may
        # have intended to recover the task by recreating its folder.
        if state == "running":
            click.echo(
                f"naiw-tasks: clean: orphan container {c.name} is still "
                f"running — force-killing (task folder missing)",
                err=True,
            )
        try:
            c.remove(force=True)
            click.echo(
                f"removed orphan container {c.name} "
                f"(task-id={labels.get('naiw.task-id')})"
            )
        except docker.errors.DockerException as exc:
            failures += 1
            _LOG.warning(
                "clean: cannot remove orphan container %s: %s",
                c.name, exc,
            )
            click.echo(
                f"naiw-tasks: clean: failed to remove orphan "
                f"{c.name}: {exc}",
                err=True,
            )

    return 1 if failures > 0 else 0
