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

from naiw_tasks import drift as drift_mod
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

    `attrs["State"]` is the INSPECT shape (dict with Status/ExitCode/...) here
    even though we got the container from `client.containers.list()`. docker-py
    7.x ContainerCollection.list() with the default `sparse=False` does
    `self.get(r['Id'])` per row, which is an inspect call — so `attrs` is the
    inspect payload, not the bare `GET /containers/json` summary where State
    would be a plain string. We rely on this throughout.

    Conditionally calls .reload() only when ExitCode is missing for an exited
    container. The list endpoint sometimes omits ExitCode on stopped
    containers, and the operator-visible exit code is the load-bearing detail
    for the `failed` vs `interrupted` branch in compute_status.
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


def _latest_fail_reason(events: list) -> str | None:
    """Return the latest `fail` event's payload.reason, or None.

    Scans from the end and stops at the first transition-causing kind. If
    that kind is `fail`, returns its reason; if it is `done`/`wait`, the
    operator's most recent intent was not failure, so no reason. Status-
    neutral kinds (currently `log`) are transparent — they never mask a
    prior fail's reason from this lookup.
    """
    for ev in reversed(events):
        kind = getattr(ev, "kind", None)
        if kind not in reconcile.TRANSITION_KINDS:
            continue
        if kind != "fail":
            return None
        payload = ev.payload
        reason = payload.get("reason") if isinstance(payload, dict) else None
        return reason if isinstance(reason, str) and reason else None
    return None


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
    seen_drift_warnings: set[tuple[str, str]],
) -> tuple[reconcile.ComputedRow, str, int | None, int, int]:
    """Process one task and return row status/container data for rendering.

    `seen_drift_warnings` is owned by `run()` and shared across all rows
    within a single list invocation; per-invocation set guarantees one
    WARNING line per (task_id, field) without leaking dedup state across
    separate list calls.
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

    latest_event_kind = reconcile.select_latest_transition_kind(valid_events)
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

    # Hardening drift audit. Reuses container.attrs from the existing
    # containers.list enumeration — no second proxy round-trip. Warn-only:
    # appends `(drift)` to NOTES and logs a deduplicated WARNING per
    # (task_id, field). attach/finish/recover are NOT gated by drift; the
    # operator may use them to remediate.
    if (
        computed.status == str(Status.RUNNING)
        and container is not None
        and ctr_state == "running"
    ):
        expected_storage_bind = (
            f"{(cfg.host_root / 'tasks' / task_id / 'storage')}"
            f":/home/pi:rw"
        )
        drift_items = drift_mod.compute_drift(
            container.attrs.get("HostConfig") or {},
            container.attrs.get("Config") or {},
            expected_storage_bind=expected_storage_bind,
        )
        if drift_items:
            if "drift" not in notes:
                notes.append("drift")
            for it in drift_items:
                key = (task_id, it.field)
                if key in seen_drift_warnings:
                    continue
                seen_drift_warnings.add(key)
                _LOG.warning(
                    "drift: task=%s field=%s expected=%r actual=%r "
                    "severity=%s",
                    task_id, it.field, it.expected, it.actual, it.severity,
                )

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

    # Skip the diagnostic write under two conditions:
    #   - defer_terminal_auto_finish: plain `list` is deferring this event
    #     for reap to consume; reap will log the malformed diagnostic itself
    #     (avoid duplicate entries across the two passes).
    #   - any dry_run: `reap --dry-run` must be fully read-only — including
    #     not touching meta/events-error.log (covers the case where the
    #     task is auto_finish=false but a malformed line is in the unread
    #     range; dry_run_pending alone wouldn't catch it).
    if not (defer_terminal_auto_finish or dry_run):
        _append_events_errors(task_dir / "meta", malformed)

    ts_now = Event.now_iso()
    stale_state = False
    original_status = task_dict.get("status", "")
    # `naiw-signal fail --reason ...` payload reason is required by the
    # signal CLI but invisible to reconcile (which only sees event KIND).
    # Capture it once and feed it to both the non-auto_finish persistence
    # path (mutator below) and the reap inline-teardown path. Without this,
    # auto_finish=false tasks lose the diagnostic the moment offset advances
    # past the fail event.
    fail_reason = (
        _latest_fail_reason(valid_events)
        if latest_event_kind == "fail"
        else None
    )

    # Offset is held back when the event still needs out-of-band handling:
    #   - defer_terminal_auto_finish: plain list, let reap consume it
    #   - dry_run_pending: reap --dry-run, must not consume
    #   - will_inline_teardown: reap, advance ONLY after teardown_and_mark
    #     returns cleanly. Holding back here removes a class of bugs where an
    #     unexpected exception inside teardown_and_mark (not SystemExit) would
    #     leave the offset advanced and the task stuck running forever.
    hold_offset = (
        defer_terminal_auto_finish or dry_run_pending or will_inline_teardown
    )

    def _mutator(d: dict) -> dict:
        nonlocal stale_state
        d = dict(d)
        disk_offset = int(d.get("events_offset", 0))
        disk_status = d.get("status", "")
        # CAS over BOTH offset and status: a concurrent `finish` may have
        # written a terminal status between our read and this locked update;
        # without the status leg we'd overwrite the fresh terminal value
        # with a stale `interrupted`/`running` computed from pre-finish input.
        stale_state = (
            disk_offset != current_offset or disk_status != original_status
        )
        d["terminal_log_max_size"] = max(
            int(d.get("terminal_log_max_size", 0)), new_max_size
        )
        if stale_state:
            return d
        if not hold_offset:
            d["events_offset"] = new_offset
        if not hold_offset and computed.transitioned:
            d["status"] = computed.status
            d["updated_at"] = ts_now
            if computed.failure_reason is not None:
                d["failure_reason"] = computed.failure_reason
            elif fail_reason and computed.status == str(Status.FAILED):
                d["failure_reason"] = fail_reason
        return d

    # `reap --dry-run` MUST be fully read-only — no offset advancement,
    # no status transitions, no terminal_log_max_size write. Otherwise a
    # non-auto_finish task with unread events (where terminal_auto_pending
    # is False and dry_run_pending therefore False too) would still get
    # its offset+status persisted, breaking the dry-run contract.
    if not dry_run:
        store.update_task(task_dir, _mutator)

    if stale_state:
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
                failure_reason=fail_reason,
            )
        except SystemExit:
            teardown_failed = True
        # Re-read so rendering and filters reflect the actual teardown result.
        with contextlib.suppress(FileNotFoundError, store.UnsupportedSchemaError):
            task_dict = store.read_task(task_dir)
        if teardown_failed:
            # Offset was held back in the mutator above, so retry on next reap
            # is automatic — no rollback write needed.
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
            # Teardown succeeded; safe to advance the offset so the consumed
            # event is not re-evaluated on the next reap.
            def _advance_offset(d: dict) -> dict:
                d = dict(d)
                d["events_offset"] = new_offset
                return d

            task_dict = store.update_task(task_dir, _advance_offset)
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
) -> list[tuple[Path, dict, reconcile.ComputedRow, str, int | None]]:
    """Filter by status set (OR within) and by project alias.

    No implicit terminal-state exclusion: per task.md the default `list`
    shows the latest tasks regardless of status (`completed`/`failed`/etc.
    are visible by default). Operator narrows with `--status` if needed.
    """
    out = rows
    if statuses:
        allowed = frozenset(statuses)
        out = [r for r in out if r[2].status in allowed]
    if project_filter is not None:
        out = [r for r in out if r[1].get("project") == project_filter]
    return out


def _sort_key(d: dict) -> str:
    """task.md: sort by updated_at desc, fall back to created_at when missing.

    A legacy task.json that has `created_at` but not `updated_at` would
    otherwise sort to the bottom (empty string < every real ISO timestamp),
    hiding newer tasks from the default latest-10 view.
    """
    return d.get("updated_at") or d.get("created_at", "")


def _sort_rows(rows):
    """Sort updated_at desc (created_at fallback), tie-break id asc.

    Two-pass stable sort keeps id asc within equal sort-key buckets.
    """
    by_id = sorted(rows, key=lambda r: r[1].get("id", ""))
    return sorted(by_id, key=lambda r: _sort_key(r[1]), reverse=True)


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
    # Per-invocation drift-WARNING dedup set. Owned by run() so the same
    # (task_id, field) pair is logged at most once across all rows within
    # a single list call; NOT a module-level global (would silently dedup
    # across separate invocations and suppress legitimate repeat warnings).
    seen_drift_warnings: set[tuple[str, str]] = set()
    rows: list[tuple[Path, dict, reconcile.ComputedRow, str, int | None]] = []
    for task_dir, task_dict in all_tasks:
        computed, ctr_state, exit_code, row_reaped, row_would_reap = _reconcile_one(
            cfg, client, task_dir, task_dict, by_task_id,
            request.apply_auto_finish, request.dry_run,
            seen_drift_warnings,
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
