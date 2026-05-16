"""Orchestrate `naiw-tasks list`.

One containers.list call per invocation, indexed by naiw.task-id label.
Per task: read task.json, tail events.jsonl, compute status, update
terminal.log max size, atomically persist (events_offset + status + max_size
+ optional failure_reason) in ONE store.update_task call. Append malformed
event lines to meta/events-error.log OUTSIDE the per-task lock.

On `done`/`fail` + auto_finish=true: advance events_offset ONLY (NOT status),
then call lifecycle._teardown_and_mark directly so the wrapper's
terminal-state short-circuit does not block teardown. The helper writes
the final status itself.
"""

import logging
from pathlib import Path
from typing import Any

import docker.errors
from naiw_common.events import Event

from naiw_tasks import events_tail, reconcile, render, store
from naiw_tasks.config import Config
from naiw_tasks.events_tail import Malformed
from naiw_tasks.lifecycle import _teardown_and_mark
from naiw_tasks.model import Status

_LOG = logging.getLogger("naiw_tasks")

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        str(Status.COMPLETED),
        str(Status.FAILED),
        str(Status.CANCELLED),
    }
)


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
    """Render the CTR column value — `exited(N)` for exited+exit_code, else state literal."""
    if ctr_state == "exited" and exit_code is not None:
        return f"exited({exit_code})"
    return ctr_state


def _append_events_errors(meta_dir: Path, malformed: list[Malformed]) -> None:
    """Plain append OUTSIDE the per-task flock. Duplicates on crash accepted.

    The events-error.log is host-only diagnostic state. Holding it inside the
    flock would couple events-error.log durability to the task.json mutator
    window — undesirable, and the per-line ISO-ts already gives the operator
    the sequencing they need.
    """
    if not malformed:
        return
    error_log = meta_dir / "events-error.log"
    with open(error_log, "a", encoding="utf-8") as f:
        for m in malformed:
            f.write(f"{Event.now_iso()}\t{m.reason}\t{m.raw_line}\n")


def _terminal_log_size(io_dir: Path) -> int:
    """lstat on terminal.log; FileNotFoundError -> 0.

    NOT stat — lstat refuses to follow symlinks. A Pi-side symlink at
    io/terminal.log pointing at a controller-side file would otherwise leak
    that file's size into the monotonic-growth tracker (and the renderer).
    Defense-in-depth alongside output_cmd's O_NOFOLLOW.
    """
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


def _reconcile_one(
    cfg: Config,
    client,
    task_dir: Path,
    task_dict: dict,
    by_task_id: dict[str, Any],
) -> tuple[reconcile.ComputedRow, str, int | None]:
    """Process one task: tail events, compute status, update task.json atomically.

    Returns (computed_row, container_state_for_render, exit_code_for_render).
    Handles the auto_finish inline-teardown path: when a `done`/`fail` event
    is pending AND auto_finish=true, the mutator advances events_offset ONLY
    (no status flip), then `_teardown_and_mark` is called directly so the
    helper writes the terminal status after teardown succeeds. This bypasses
    the operator-facing finish wrapper's terminal-state short-circuit, which
    would otherwise block teardown if the disk status happened to already be
    `failed` from a previous run.
    """
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
    # Append malformed BEFORE the store.update_task call so the diagnostic
    # write happens outside the per-task flock window held by the mutator.
    _append_events_errors(task_dir / "meta", malformed)

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

    # Inline auto_finish gate. When auto_finish=true AND a terminal event is
    # pending AND disk is not already terminal: advance events_offset ONLY
    # (no status flip) so `_teardown_and_mark` writes the terminal status
    # itself after teardown succeeds. Disk-status guard avoids double-write
    # if a previous list pass already advanced offset + the helper wrote the
    # status.
    auto_finish = bool(task_dict.get("auto_finish", False))
    will_inline_teardown = (
        auto_finish
        and latest_event_kind in ("done", "fail")
        and task_dict.get("status") not in _TERMINAL_STATUSES
    )

    ts_now = Event.now_iso()

    def _mutator(d: dict) -> dict:
        d = dict(d)
        d["events_offset"] = new_offset
        d["terminal_log_max_size"] = new_max_size
        if not will_inline_teardown and computed.transitioned:
            d["status"] = computed.status
            d["updated_at"] = ts_now
            if computed.failure_reason is not None:
                d["failure_reason"] = computed.failure_reason
        return d

    store.update_task(task_dir, _mutator)

    if will_inline_teardown:
        terminal_status = (
            Status.COMPLETED if latest_event_kind == "done" else Status.FAILED
        )
        try:
            _teardown_and_mark(
                cfg, client, task_id, task_dir,
                terminal_status=terminal_status,
                policy_override=None,
            )
        except SystemExit:
            # _teardown_and_mark raises SystemExit when verify-NotFound fails;
            # _mark_finish_failed has already written task.json. Swallow and
            # continue rendering — operator sees the failed row.
            pass

    computed = reconcile.ComputedRow(
        status=computed.status,
        transitioned=computed.transitioned,
        notes=tuple(notes),
        failure_reason=computed.failure_reason,
    )
    return (computed, ctr_state, exit_code)


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

    Two-pass stable sort: Python's Timsort preserves the id-asc order from
    the first pass when the second pass encounters equal updated_at values.
    A single `sorted(...)[::-1]` against a tuple key reverses id ordering
    within ties — that gave id-desc instead of id-asc.
    """
    by_id = sorted(rows, key=lambda r: r[1].get("id", ""))
    return sorted(by_id, key=lambda r: r[1].get("updated_at", ""), reverse=True)


def run(
    cfg: Config,
    client,
    limit: int,
    statuses: list[str],
    project_filter: str | None,
    show_all: bool,
    include_completed: bool,
    as_json: bool,
    limit_was_explicit: bool,
) -> None:
    """Entry point called by cli.py.

    Performs exactly ONE containers.list call, then iterates tasks on disk,
    reconciling each row and rendering the resulting table (or JSON payload).
    """
    managed = client.containers.list(
        all=True, filters={"label": "naiw.managed=1"},
    )
    by_task_id = {c.labels.get("naiw.task-id"): c for c in managed}

    all_tasks = _enumerate_tasks(cfg.data_root)
    rows: list[tuple[Path, dict, reconcile.ComputedRow, str, int | None]] = []
    for task_dir, task_dict in all_tasks:
        computed, ctr_state, exit_code = _reconcile_one(
            cfg, client, task_dir, task_dict, by_task_id,
        )
        # Re-read task.json: _teardown_and_mark may have mutated it. Falling
        # back to the original task_dict on read error keeps the row visible
        # even if the post-teardown read fails (e.g. concurrent delete).
        try:
            task_dict = store.read_task(task_dir)
        except (FileNotFoundError, store.UnsupportedSchemaError):
            pass
        rows.append((task_dir, task_dict, computed, ctr_state, exit_code))

    filtered = _apply_filters(
        rows, statuses, project_filter, show_all, include_completed,
    )
    filtered = _sort_rows(filtered)

    if show_all and not limit_was_explicit:
        effective_rows = filtered
    else:
        effective_rows = filtered[:limit]

    if as_json:
        payload = render.to_json_payload(
            [(r[1], r[2], r[3], r[4]) for r in effective_rows]
        )
        print(render.to_json_string(payload))
        return

    ts_now = Event.now_iso()
    table_rows: list[list[str]] = []
    for _task_dir, task_dict, computed, ctr_state, exit_code in effective_rows:
        table_rows.append(
            [
                task_dict.get("id", ""),
                computed.status,
                _format_ctr_cell(ctr_state, exit_code),
                task_dict.get("project") or "—",
                render._humanize_delta(ts_now, task_dict.get("started_at")),
                render._short_image_digest(task_dict.get("image_digest")),
                render._format_notes(computed.notes),
            ]
        )
    print(render.render_table(list(render.COLUMNS), table_rows), end="")
