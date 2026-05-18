"""Orchestrate `naiw-tasks list`.

One containers.list call per invocation. Each task tails events, computes
status, updates terminal.log max size, and persists task.json atomically.
Malformed event lines are logged outside the per-task lock.
"""

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker.errors
from naiw_common.events import Event

from naiw_tasks import events_tail, reconcile, render, store
from naiw_tasks.config import Config
from naiw_tasks.events_tail import Malformed
from naiw_tasks.lifecycle import teardown_and_mark
from naiw_tasks.model import Status

_LOG = logging.getLogger("naiw_tasks")

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        str(Status.COMPLETED),
        str(Status.FAILED),
        str(Status.CANCELLED),
    }
)


@dataclass(frozen=True)
class ListRequest:
    limit: int | None
    statuses: list[str]
    project_filter: str | None
    show_all: bool
    include_completed: bool
    as_json: bool
    limit_was_explicit: bool
    apply_auto_finish: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ListResult:
    reaped: int = 0
    would_reap: int = 0


def _container_state(container) -> tuple[str, int | None]:
    """Return (state_literal, ExitCode | None).

    Conditionally calls .reload() only when the containers.list summary lacks
    ExitCode for an exited container. The list endpoint sometimes omits
    ExitCode on stopped containers, and the operator-visible exit code is
    the load-bearing detail for the `failed` vs `interrupted` branch in
    compute_status.
    """
    state = container.attrs.get("State", {})
    status = state.get("Status", "notfound")
    if status == "exited" and "ExitCode" not in state:
        try:
            container.reload()
            state = container.attrs.get("State", {})
        except docker.errors.NotFound:
            return ("notfound", None)
    exit_code = state.get("ExitCode") if status == "exited" else None
    return (status, exit_code)


def _format_ctr_cell(ctr_state: str, exit_code: int | None) -> str:
    """Render the CTR column value."""
    if ctr_state == "exited" and exit_code is not None:
        return f"exited({exit_code})"
    return ctr_state


def _append_events_errors(meta_dir: Path, malformed: list[Malformed]) -> None:
    """Plain append outside the per-task flock; duplicates on crash accepted."""
    if not malformed:
        return
    error_log = meta_dir / "events-error.log"
    with open(error_log, "a", encoding="utf-8") as f:
        for m in malformed:
            f.write(f"{Event.now_iso()}\t{m.reason}\t{m.raw_line}\n")


def _terminal_log_size(io_dir: Path) -> int:
    """lstat on terminal.log; FileNotFoundError -> 0."""
    terminal_log = io_dir / "terminal.log"
    try:
        return terminal_log.lstat().st_size
    except FileNotFoundError:
        return 0


def _select_latest_event_kind(events: list) -> str | None:
    """Latest event by list position wins for terminal kinds.

    events_tail returns valid events in file-byte order, so the final element
    is the highest-offset (latest) event. Used as `pending_event_kind` for
    compute_status: a `done` after `wait` flips the row to completed.
    """
    if not events:
        return None
    return events[-1].kind


def _auto_finish_can_run(task_dict: dict) -> bool:
    status = task_dict.get("status")
    if status not in _TERMINAL_STATUSES:
        return True
    return (
        status == str(Status.FAILED)
        and str(task_dict.get("failure_reason") or "").startswith("finish:")
    )


def _reconcile_one(
    cfg: Config,
    client,
    task_dir: Path,
    task_dict: dict,
    by_task_id: dict[str, Any],
    apply_auto_finish: bool,
    dry_run: bool,
) -> tuple[reconcile.ComputedRow, str, int | None, int, int]:
    """Process one task and return row status/container data for rendering."""
    task_id = task_dict.get("id", task_dir.name)
    container = by_task_id.get(task_id)
    if container is None:
        ctr_state, exit_code = ("notfound", None)
    else:
        ctr_state, exit_code = _container_state(container)

    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    current_offset = int(task_dict.get("events_offset", 0))
    new_offset, valid_events, malformed = events_tail.tail_events(
        events_path, current_offset,
    )

    latest_event_kind = _select_latest_event_kind(valid_events)
    computed = reconcile.compute_status(
        task_dict=task_dict,
        ctr_state=ctr_state,
        ctr_exit_code=exit_code,
        pending_event_kind=latest_event_kind,
    )

    # Monotonic-growth check on terminal.log. lstat refuses to follow a
    # symlink at io/terminal.log so a Pi-side symlink to a controller-side
    # file does not leak that file's size into the tracker.
    current_log_size = _terminal_log_size(task_dir / "io")
    stored_max_size = int(task_dict.get("terminal_log_max_size", 0))
    notes = list(computed.notes)
    if current_log_size < stored_max_size:
        if "log shrunk" not in notes:
            notes.append("log shrunk")
        _LOG.warning(
            "task %s: terminal.log shrank (current=%d, stored_max=%d)",
            task_id, current_log_size, stored_max_size,
        )
        new_max_size = stored_max_size
    else:
        new_max_size = current_log_size

    # Plain list leaves terminal auto_finish events unread so reap can close them.
    auto_finish = bool(task_dict.get("auto_finish", False))
    terminal_auto_pending = (
        auto_finish
        and latest_event_kind in ("done", "fail")
        and _auto_finish_can_run(task_dict)
    )
    will_inline_teardown = (
        apply_auto_finish and terminal_auto_pending and not dry_run
    )
    defer_terminal_auto_finish = terminal_auto_pending and not apply_auto_finish
    dry_run_pending = apply_auto_finish and terminal_auto_pending and dry_run

    # Keep unread-range diagnostics for reap to avoid duplicate list logs.
    if not (defer_terminal_auto_finish or dry_run_pending):
        _append_events_errors(task_dir / "meta", malformed)

    ts_now = Event.now_iso()
    stale_events_offset = False

    def _mutator(d: dict) -> dict:
        nonlocal stale_events_offset
        d = dict(d)
        disk_offset = int(d.get("events_offset", 0))
        stale_events_offset = disk_offset != current_offset
        d["terminal_log_max_size"] = max(
            int(d.get("terminal_log_max_size", 0)), new_max_size
        )
        if stale_events_offset:
            return d
        d["events_offset"] = (
            current_offset
            if defer_terminal_auto_finish or dry_run_pending
            else new_offset
        )
        if (
            not will_inline_teardown
            and not defer_terminal_auto_finish
            and not dry_run_pending
            and computed.transitioned
        ):
            d["status"] = computed.status
            d["updated_at"] = ts_now
            if computed.failure_reason is not None:
                d["failure_reason"] = computed.failure_reason
        return d

    store.update_task(task_dir, _mutator)

    if stale_events_offset:
        with contextlib.suppress(FileNotFoundError, store.UnsupportedSchemaError):
            task_dict = store.read_task(task_dir)
        computed = reconcile.compute_status(
            task_dict=task_dict,
            ctr_state=ctr_state,
            ctr_exit_code=exit_code,
            pending_event_kind=None,
        )
        return (computed, ctr_state, exit_code, 0, 0)

    if defer_terminal_auto_finish or dry_run_pending:
        if "auto_finish pending" not in notes:
            notes.append("auto_finish pending")
        computed = reconcile.ComputedRow(
            status=str(task_dict.get("status", computed.status)),
            transitioned=False,
            notes=(),
            failure_reason=None,
        )

    reaped = 0
    if will_inline_teardown:
        terminal_status = (
            Status.COMPLETED if latest_event_kind == "done" else Status.FAILED
        )
        # On failure, lifecycle has already written task.json.status=failed.
        teardown_failed = False
        try:
            teardown_and_mark(
                cfg, client, task_id, task_dir,
                terminal_status=terminal_status,
                policy_override=None,
                allow_prompt=False,
            )
        except SystemExit:
            teardown_failed = True
        # Re-read so rendering and filters reflect the actual teardown result.
        with contextlib.suppress(FileNotFoundError, store.UnsupportedSchemaError):
            task_dict = store.read_task(task_dir)
        if teardown_failed:
            def _restore_offset(d: dict) -> dict:
                d = dict(d)
                d["events_offset"] = current_offset
                return d

            task_dict = store.update_task(task_dir, _restore_offset)
            try:
                container = by_task_id.get(task_id)
                if container is not None:
                    container.reload()
                    ctr_state, exit_code = _container_state(container)
                else:
                    ctr_state, exit_code = ("notfound", None)
            except docker.errors.NotFound:
                ctr_state, exit_code = ("notfound", None)
            except docker.errors.APIError:
                pass
        else:
            ctr_state, exit_code = ("notfound", None)
            reaped = 1
        computed = reconcile.compute_status(
            task_dict=task_dict,
            ctr_state=ctr_state,
            ctr_exit_code=exit_code,
            pending_event_kind=None,
        )
        # Preserve pre-teardown notes such as "log shrunk".
        for n in computed.notes:
            if n not in notes:
                notes.append(n)

    computed = reconcile.ComputedRow(
        status=computed.status,
        transitioned=computed.transitioned,
        notes=tuple(notes),
        failure_reason=computed.failure_reason,
    )
    would_reap = 1 if dry_run_pending else 0
    return (computed, ctr_state, exit_code, reaped, would_reap)


def _enumerate_tasks(data_root: Path) -> list[tuple[Path, dict]]:
    """Read every task.json under data_root/tasks/. Skip bad/missing with WARNING."""
    tasks_dir = data_root / "tasks"
    if not tasks_dir.exists():
        return []
    out: list[tuple[Path, dict]] = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        try:
            d = store.read_task(task_dir)
        except (FileNotFoundError, store.UnsupportedSchemaError, ValueError) as exc:
            _LOG.warning("skipping task at %s: %s", task_dir, exc)
            continue
        out.append((task_dir, d))
    return out


def _apply_filters(
    rows: list[tuple[Path, dict, reconcile.ComputedRow, str, int | None]],
    statuses: list[str],
    project_filter: str | None,
    show_all: bool,
    include_completed: bool,
) -> list[tuple[Path, dict, reconcile.ComputedRow, str, int | None]]:
    out = rows
    if statuses:
        allowed = frozenset(statuses)
        out = [r for r in out if r[2].status in allowed]
    elif not show_all:
        # Default scope: exclude terminal states unless --completed.
        if not include_completed:
            out = [r for r in out if r[2].status not in _TERMINAL_STATUSES]
    if project_filter is not None:
        out = [r for r in out if r[1].get("project") == project_filter]
    return out


def _sort_rows(rows):
    """Sort updated_at desc, tie-break id asc.

    Two-pass stable sort keeps id asc within equal updated_at buckets.
    """
    by_id = sorted(rows, key=lambda r: r[1].get("id", ""))
    return sorted(by_id, key=lambda r: r[1].get("updated_at", ""), reverse=True)


def run(
    cfg: Config,
    client,
    request: ListRequest,
) -> ListResult:
    """Entry point called by cli.py.

    Performs exactly ONE containers.list call, then iterates tasks on disk,
    reconciling each row and rendering the resulting table (or JSON payload).
    """
    managed = client.containers.list(
        all=True, filters={"label": "naiw.managed=1"},
    )
    by_task_id = {c.labels.get("naiw.task-id"): c for c in managed}

    all_tasks = _enumerate_tasks(cfg.data_root)
    reaped = 0
    would_reap = 0
    rows: list[tuple[Path, dict, reconcile.ComputedRow, str, int | None]] = []
    for task_dir, task_dict in all_tasks:
        computed, ctr_state, exit_code, row_reaped, row_would_reap = _reconcile_one(
            cfg, client, task_dir, task_dict, by_task_id,
            request.apply_auto_finish, request.dry_run,
        )
        reaped += row_reaped
        would_reap += row_would_reap
        # Re-read task.json: teardown_and_mark may have mutated it. Falling
        # back to the original task_dict on read error keeps the row visible
        # even if the post-teardown read fails (e.g. concurrent delete).
        with contextlib.suppress(FileNotFoundError, store.UnsupportedSchemaError):
            task_dict = store.read_task(task_dir)
        rows.append((task_dir, task_dict, computed, ctr_state, exit_code))

    filtered = _apply_filters(
        rows,
        request.statuses,
        request.project_filter,
        request.show_all,
        request.include_completed,
    )
    filtered = _sort_rows(filtered)

    effective_rows = (
        filtered
        if request.limit is None
        or (request.show_all and not request.limit_was_explicit)
        else filtered[: request.limit]
    )

    if request.as_json:
        payload = render.to_json_payload(
            [(r[1], r[2], r[3], r[4]) for r in effective_rows]
        )
        print(render.to_json_string(payload))
        return ListResult(reaped=reaped, would_reap=would_reap)

    ts_now = Event.now_iso()
    table_rows: list[list[str]] = []
    for _task_dir, task_dict, computed, ctr_state, exit_code in effective_rows:
        table_rows.append(
            [
                task_dict.get("id", ""),
                computed.status,
                _format_ctr_cell(ctr_state, exit_code),
                task_dict.get("project") or "-",
                render.humanize_delta(ts_now, task_dict.get("started_at")),
                render.short_image_digest(task_dict.get("image_digest")),
                render.format_notes(computed.notes),
            ]
        )
    print(render.render_table(list(render.COLUMNS), table_rows), end="")
    return ListResult(reaped=reaped, would_reap=would_reap)
