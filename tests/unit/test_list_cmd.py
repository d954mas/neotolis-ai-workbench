"""Tests for naiw_tasks.list_cmd — orchestration of `naiw-tasks list`.

Exercises list_cmd.run directly (not via CliRunner) so the click veneer is
out of scope. Docker SDK is mocked at the client level; store.update_task
is exercised against real on-disk task.json (via tmp_naiw_data).

Task 2a covers: default scope, filters, sort order, single containers.list
call, truth-table integration with real compute_status, single
store.update_task call for non-auto-finish, JSON output, unknown-status
passthrough, leaked-ctr marker, column rendering.

Task 2b extends this file with: lazy event apply, malformed-line append to
meta/events-error.log outside flock, idempotent reapply, partial-line policy,
list-only auto_finish pending rows, reap teardown, and DATA-08 monotonic-growth
lstat + shrink marker behaviour.
"""

import sys

import pytest

if sys.platform != "linux":
    pytest.skip(
        "Linux-only (fcntl / O_NOFOLLOW)",
        allow_module_level=True,
    )

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import docker.errors
from naiw_tasks.config import Config

from naiw_tasks import list_cmd, store

# ---------- helpers ---------------------------------------------------------


def _make_task(
    data_root: Path,
    task_id: str,
    status: str = "running",
    project: str | None = "alpha",
    kind: str = "project",
    auto_finish: bool = False,
    updated_at: str = "2026-05-16T10:00:00.000Z",
    started_at: str = "2026-05-16T09:00:00.000Z",
    events_offset: int = 0,
    terminal_log_max_size: int = 0,
    image_digest: str = "sha256:deadbeefcafebabe1234567890",
) -> Path:
    """Create a per-task skeleton with a minimal valid task.json."""
    task_dir = data_root / "tasks" / task_id
    (task_dir / "meta").mkdir(parents=True, exist_ok=True)
    (task_dir / "io" / ".naiw").mkdir(parents=True, exist_ok=True)
    payload = {
        "id": task_id,
        "kind": kind,
        "container_name": f"naiw-task-{task_id}",
        "image_tag": "ghcr.io/d954mas/naiw-task-image:latest",
        "created_at": started_at,
        "updated_at": updated_at,
        "started_at": started_at,
        "status": status,
        "project": project,
        "auto_finish": auto_finish,
        "events_offset": events_offset,
        "terminal_log_max_size": terminal_log_max_size,
        "image_digest": image_digest,
        "labels": {"naiw.managed": "1", "naiw.task-id": task_id},
        "secrets": [],
        "recovery_history": [],
        "schema_version": 1,
    }
    (task_dir / "meta" / "task.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return task_dir


def _mock_container(task_id: str, state: str = "running", exit_code: int | None = None):
    """Build a docker-py-compatible container mock."""
    c = MagicMock()
    c.labels = {"naiw.managed": "1", "naiw.task-id": task_id}
    state_dict: dict = {"Status": state}
    if exit_code is not None:
        state_dict["ExitCode"] = exit_code
    c.attrs = {"State": state_dict}
    return c


def _mock_client(containers: list) -> MagicMock:
    client = MagicMock()
    client.containers.list.return_value = list(containers)
    return client


def _run_list(
    cfg: Config,
    client,
    *,
    limit: int,
    statuses: list[str],
    project_filter: str | None,
    show_all: bool,
    as_json: bool,
    limit_was_explicit: bool,
    include_completed: bool = True,  # legacy kwarg ignored
    apply_auto_finish: bool = False,
    dry_run: bool = False,
) -> list_cmd.ListResult:
    del include_completed
    return list_cmd.run(
        cfg,
        client,
        list_cmd.ListRequest(
            limit=limit,
            statuses=statuses,
            project_filter=project_filter,
            show_all=show_all,
            as_json=as_json,
            limit_was_explicit=limit_was_explicit,
            apply_auto_finish=apply_auto_finish,
            dry_run=dry_run,
        ),
    )


def _cfg(data_root: Path) -> Config:
    return Config(data_root=data_root)


def _capture(capsys) -> str:
    out, _ = capsys.readouterr()
    return out


# ---------- default scope --------------------------------------------------


def test_default_scope_includes_terminal_statuses(tmp_naiw_data, capsys):
    """task.md describes default `naiw-tasks list` as 'the latest 10 tasks
    with their statuses' with completed/failed visible in the example
    output. No implicit terminal-state exclusion."""
    _make_task(tmp_naiw_data, "alpha-001", status="running")
    _make_task(tmp_naiw_data, "alpha-002", status="completed")
    _make_task(tmp_naiw_data, "alpha-003", status="failed")
    _make_task(tmp_naiw_data, "alpha-004", status="cancelled")
    _make_task(tmp_naiw_data, "alpha-005", status="interrupted")
    client = _mock_client([])

    _run_list(
        _cfg(tmp_naiw_data),
        client,
        limit=10,
        statuses=[],
        project_filter=None,
        show_all=False,
        as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)

    for tid in ("alpha-001", "alpha-002", "alpha-003", "alpha-004", "alpha-005"):
        assert tid in out, f"{tid} missing from default `list` output"


# ---------- filters (parametrised) ------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        "include_completed",
        "show_all_no_explicit_limit",
        "show_all_with_explicit_limit",
        "filter_by_status",
        "filter_by_project",
        "default_limit_cap",
    ],
)
def test_filters(scenario, tmp_naiw_data, capsys):
    if scenario == "include_completed":
        # All 5 statuses present; include_completed=True shows everyone.
        _make_task(tmp_naiw_data, "alpha-001", status="running")
        _make_task(tmp_naiw_data, "alpha-002", status="completed")
        _make_task(tmp_naiw_data, "alpha-003", status="failed")
        _make_task(tmp_naiw_data, "alpha-004", status="cancelled")
        _make_task(tmp_naiw_data, "alpha-005", status="interrupted")
        client = _mock_client([])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=True, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        for tid in (
            "alpha-001", "alpha-002", "alpha-003",
            "alpha-004", "alpha-005",
        ):
            assert tid in out

    elif scenario == "show_all_no_explicit_limit":
        for i in range(1, 26):
            _make_task(tmp_naiw_data, f"alpha-{i:03d}", status="running")
        client = _mock_client([])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=True, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        # All 25 task IDs visible (no 10-row cap when --all without explicit --limit).
        for i in range(1, 26):
            assert f"alpha-{i:03d}" in out

    elif scenario == "show_all_with_explicit_limit":
        for i in range(1, 26):
            _make_task(tmp_naiw_data, f"alpha-{i:03d}", status="running")
        client = _mock_client([])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=5, statuses=[], project_filter=None,
            show_all=True, include_completed=False, as_json=False,
            limit_was_explicit=True,
        )
        out = _capture(capsys)
        # Limit honoured when explicitly given.
        count = sum(1 for i in range(1, 26) if f"alpha-{i:03d}" in out)
        assert count == 5

    elif scenario == "filter_by_status":
        # Pre-make tasks AND mock containers so compute_status keeps the
        # stored statuses (otherwise running+notfound would flip to
        # interrupted and break the filter target). Use waiting_for_user
        # + completed which DO NOT auto-flip when their container is
        # also running (waiting stays waiting; completed renders with
        # leaked-ctr marker but status stays completed).
        _make_task(tmp_naiw_data, "alpha-001", status="running")
        _make_task(tmp_naiw_data, "alpha-002", status="interrupted")
        _make_task(tmp_naiw_data, "alpha-003", status="waiting_for_user")
        _make_task(tmp_naiw_data, "alpha-004", status="completed")
        client = _mock_client(
            [
                _mock_container("alpha-001", state="running"),
                _mock_container("alpha-002", state="exited", exit_code=0),
                _mock_container("alpha-003", state="running"),
                _mock_container("alpha-004", state="exited", exit_code=0),
            ]
        )
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=["running", "interrupted"], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        # Strip leading 2 columns to scope the substring check past any
        # ID-fragment false positives in subsequent rows.
        assert "alpha-001  running" in out
        assert "alpha-002  interrupted" in out
        assert "alpha-003  waiting_for_user" not in out
        assert "alpha-004  completed" not in out

    elif scenario == "filter_by_project":
        _make_task(tmp_naiw_data, "alpha-001", status="running", project="alpha")
        _make_task(tmp_naiw_data, "alpha-002", status="running", project="alpha")
        _make_task(tmp_naiw_data, "beta-001", status="running", project="beta")
        client = _mock_client([])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter="alpha",
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        assert "alpha-001" in out
        assert "alpha-002" in out
        assert "beta-001" not in out

    elif scenario == "default_limit_cap":
        for i in range(1, 16):
            _make_task(tmp_naiw_data, f"alpha-{i:03d}", status="running")
        client = _mock_client([])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        count = sum(1 for i in range(1, 16) if f"alpha-{i:03d}" in out)
        assert count == 10


# ---------- sort order (D-13) -----------------------------------------------


def test_sort_by_updated_at_desc_id_asc(tmp_naiw_data, capsys):
    # Two tasks share updated_at; alpha-001 is alphabetically first.
    # Each task is paired with a running container so compute_status does
    # NOT transition the row (transitioned=False keeps updated_at fixed
    # to the value pre-baked in task.json — the tie-break test is the
    # whole point of this case).
    _make_task(
        tmp_naiw_data, "alpha-002", status="running",
        updated_at="2026-05-16T10:00:00.000Z",
    )
    _make_task(
        tmp_naiw_data, "alpha-001", status="running",
        updated_at="2026-05-16T10:00:00.000Z",
    )
    _make_task(
        tmp_naiw_data, "zeta-001", status="running",
        updated_at="2026-05-16T11:00:00.000Z",
    )
    _make_task(
        tmp_naiw_data, "omega-001", status="running",
        updated_at="2026-05-16T09:00:00.000Z",
    )
    client = _mock_client(
        [
            _mock_container("alpha-002", state="running"),
            _mock_container("alpha-001", state="running"),
            _mock_container("zeta-001", state="running"),
            _mock_container("omega-001", state="running"),
        ]
    )
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    # Expected order: zeta-001, alpha-001, alpha-002, omega-001.
    idx_zeta = out.index("zeta-001")
    idx_a1 = out.index("alpha-001")
    idx_a2 = out.index("alpha-002")
    idx_omega = out.index("omega-001")
    assert idx_zeta < idx_a1 < idx_a2 < idx_omega


def test_sort_falls_back_to_created_at_when_updated_at_missing(
    tmp_naiw_data, capsys
):
    """task.md: 'Sort by updated_at descending. If updated_at is missing,
    fall back to created_at.' A legacy task without updated_at must not
    sink to the bottom of the default latest-10 view."""
    # Newer task — has only created_at (legacy shape).
    legacy_dir = tmp_naiw_data / "tasks" / "legacy-001"
    (legacy_dir / "meta").mkdir(parents=True)
    (legacy_dir / "io" / ".naiw").mkdir(parents=True)
    legacy_payload = {
        "id": "legacy-001",
        "kind": "project",
        "container_name": "naiw-task-legacy-001",
        "image_tag": "ghcr.io/d954mas/naiw-task-image:latest",
        "created_at": "2026-05-16T12:00:00.000Z",  # newest by created_at
        "status": "running",
        "project": "alpha",
        "labels": {"naiw.managed": "1", "naiw.task-id": "legacy-001"},
        "secrets": [],
        "recovery_history": [],
        "schema_version": 1,
    }
    (legacy_dir / "meta" / "task.json").write_text(
        json.dumps(legacy_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Older task with both timestamps; updated_at predates legacy's created_at.
    _make_task(
        tmp_naiw_data, "alpha-001",
        updated_at="2026-05-16T10:00:00.000Z",
        started_at="2026-05-16T09:00:00.000Z",
    )
    client = _mock_client([])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, as_json=False, limit_was_explicit=False,
    )
    out = _capture(capsys)
    assert out.index("legacy-001") < out.index("alpha-001")


# ---------- single containers.list call -------------------------------------


def test_single_containers_list_call_indexed_by_task_id_label(tmp_naiw_data, capsys):
    for i in range(1, 6):
        _make_task(tmp_naiw_data, f"alpha-{i:03d}", status="running")
    client = _mock_client([])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    assert client.containers.list.call_count == 1
    _, kwargs = client.containers.list.call_args
    assert kwargs.get("all") is True
    assert kwargs.get("filters") == {"label": "naiw.managed=1"}


# ---------- truth-table integration (parametrised) --------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        "running_task_running_container",
        "running_task_missing_container",
        "running_task_exited_nonzero",
        "running_task_exited_no_exit_code_triggers_reload",
        "running_task_running_no_reload",
    ],
)
def test_truth_table_integration_with_real_compute_status(
    scenario, tmp_naiw_data, capsys
):
    if scenario == "running_task_running_container":
        _make_task(tmp_naiw_data, "alpha-001", status="running")
        client = _mock_client([_mock_container("alpha-001", state="running")])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        # Status remains running; CTR column shows running.
        assert "running" in out
        # On-disk status unchanged.
        data = json.loads(
            (tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json").read_text()
        )
        assert data["status"] == "running"

    elif scenario == "running_task_missing_container":
        _make_task(tmp_naiw_data, "alpha-001", status="running")
        client = _mock_client([])  # No container in containers.list
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        assert "interrupted" in out
        data = json.loads(
            (tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json").read_text()
        )
        assert data["status"] == "interrupted"

    elif scenario == "running_task_exited_nonzero":
        _make_task(tmp_naiw_data, "alpha-001", status="running")
        client = _mock_client(
            [_mock_container("alpha-001", state="exited", exit_code=137)]
        )
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        data = json.loads(
            (tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json").read_text()
        )
        assert data["status"] == "failed"

    elif scenario == "running_task_exited_no_exit_code_triggers_reload":
        _make_task(tmp_naiw_data, "alpha-001", status="running")
        # Container state shows exited but ExitCode missing on summary attrs.
        c = MagicMock()
        c.labels = {"naiw.managed": "1", "naiw.task-id": "alpha-001"}
        c.attrs = {"State": {"Status": "exited"}}  # No ExitCode

        # reload() populates ExitCode=0.
        def _reload():
            c.attrs = {"State": {"Status": "exited", "ExitCode": 0}}

        c.reload.side_effect = _reload
        client = _mock_client([c])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        c.reload.assert_called_once()
        data = json.loads(
            (tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json").read_text()
        )
        # exit_code=0 + no done event → interrupted.
        assert data["status"] == "interrupted"

    elif scenario == "running_task_running_no_reload":
        _make_task(tmp_naiw_data, "alpha-001", status="running")
        c = _mock_container("alpha-001", state="running")
        client = _mock_client([c])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        c.reload.assert_not_called()


# ---------- atomic-write boundary (Pattern 3, non-auto-finish path) --------


def test_single_store_update_task_call_for_normal_path(
    tmp_naiw_data, monkeypatch, capsys
):
    """auto_finish=False + no pending events → exactly 1 store.update_task call."""
    _make_task(tmp_naiw_data, "alpha-001", status="running", auto_finish=False)
    client = _mock_client([_mock_container("alpha-001", state="running")])

    real_update = store.update_task
    calls = []

    def spy(task_dir, mutator):
        calls.append(task_dir.name)
        return real_update(task_dir, mutator)

    monkeypatch.setattr(store, "update_task", spy)
    # Also patch the imported alias in list_cmd.
    monkeypatch.setattr(list_cmd.store, "update_task", spy)

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    # Exactly one call for the single task — atomic mutation of
    # events_offset + terminal_log_max_size (+ optional status).
    assert calls == ["alpha-001"]


# ---------- JSON output schema ----------------------------------------------


def test_json_output_schema(tmp_naiw_data, capsys):
    _make_task(tmp_naiw_data, "alpha-001", status="running")
    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=True,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    payload = json.loads(out)
    assert "as_of" in payload
    assert "tasks" in payload
    assert len(payload["tasks"]) == 1
    task = payload["tasks"][0]
    assert task["id"] == "alpha-001"
    assert task["status"] == "running"
    assert "container_state" in task
    assert "notes" in task
    assert "task_json" in task
    raw = task["task_json"]
    assert "schema_version" in raw
    assert "events_offset" in raw
    assert "terminal_log_max_size" in raw


# ---------- unknown status passthrough (D-04) -------------------------------


def test_unknown_status_passthrough_with_warning(tmp_naiw_data, capsys):
    _make_task(tmp_naiw_data, "alpha-001", status="future_xyz_status")
    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=True, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    assert "future_xyz_status" in out
    assert "(unknown status)" in out
    # On-disk status unchanged.
    data = json.loads(
        (tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json").read_text()
    )
    assert data["status"] == "future_xyz_status"


# ---------- leaked container marker (D-05) ---------------------------------


def test_leaked_container_marker(tmp_naiw_data, capsys):
    _make_task(tmp_naiw_data, "alpha-001", status="completed")
    # Container still alive even though task completed → leak.
    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=True, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    assert "(leaked ctr)" in out
    data = json.loads(
        (tmp_naiw_data / "tasks" / "alpha-001" / "meta" / "task.json").read_text()
    )
    # No auto-removal — that is Phase 5's CLEAN-03.
    assert data["status"] == "completed"


# ---------- render: column header order matches D-12 -----------------------


def test_render_table_columns_match_d12(tmp_naiw_data, capsys):
    _make_task(tmp_naiw_data, "alpha-001", status="running")
    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    header = out.splitlines()[0]
    # All 7 column names appear in order.
    for col in ("ID", "STATUS", "CTR", "PROJECT", "STARTED", "IMAGE", "NOTES"):
        assert col in header
    # Order check: ID precedes STATUS precedes CTR ... precedes NOTES.
    positions = [
        header.index(c)
        for c in ("ID", "STATUS", "CTR", "PROJECT", "STARTED", "IMAGE", "NOTES")
    ]
    assert positions == sorted(positions)


# =============================================================================
# Task 2b extensions: lazy event apply, auto_finish, DATA-08, events-error.log
# =============================================================================


def _write_event_line(task_dir: Path, kind: str, ts: str = "2026-05-16T10:00:00.000Z") -> int:
    """Append a single valid JSONL event line; return bytes written."""
    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    payload = {"reason": "x"} if kind in ("fail", "wait") else {}
    line = json.dumps(
        {"ts": ts, "kind": kind, "payload": payload, "schema_version": 1}
    ) + "\n"
    encoded = line.encode("utf-8")
    with open(events_path, "ab") as f:
        f.write(encoded)
    return len(encoded)


# ---------- lazy event apply (LIST-06 / SIG-05/06/07) ----------------------


def test_lazy_event_apply_advances_offset(tmp_naiw_data, capsys):
    """auto_finish=false path: a `done` event flips status, leaves container."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=False
    )
    bytes_written = _write_event_line(task_dir, "done")
    # Container is still running; status flip to completed.
    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=True, as_json=False,
        limit_was_explicit=False,
    )
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["events_offset"] == bytes_written
    assert data["status"] == "completed"


def test_malformed_line_appended_to_events_error_log(tmp_naiw_data, capsys):
    task_dir = _make_task(tmp_naiw_data, "alpha-001", status="running")
    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    # One valid done, one malformed, one valid wait.
    valid_done = json.dumps(
        {"ts": "2026-05-16T10:00:00.000Z", "kind": "done", "payload": {}, "schema_version": 1}
    ) + "\n"
    bad = "not valid json\n"
    valid_wait = json.dumps(
        {
            "ts": "2026-05-16T10:00:01.000Z",
            "kind": "wait",
            "payload": {"reason": "x"},
            "schema_version": 1,
        }
    ) + "\n"
    payload = (valid_done + bad + valid_wait).encode("utf-8")
    with open(events_path, "ab") as f:
        f.write(payload)

    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=True, as_json=False,
        limit_was_explicit=False,
    )

    error_log = task_dir / "meta" / "events-error.log"
    assert error_log.exists()
    lines = error_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\tjson:.*\tnot valid json$",
        lines[0],
    ), lines[0]
    # Offset advanced past all 3 lines (full bytes consumed).
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["events_offset"] == len(payload)


def test_events_error_log_appended_outside_flock():
    """Source-text guard: _append_events_errors must NOT be inside the
    mutator closure passed to store.update_task. Holding the diagnostic
    write inside the per-task flock window would couple events-error.log
    durability to the task.json mutation cadence."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "list_cmd.py"
    ).read_text(encoding="utf-8")
    # Locate _reconcile_one body. Verify _append_events_errors call comes
    # BEFORE the store.update_task line in source order.
    body = src
    idx_append = body.find("_append_events_errors(")
    idx_store = body.find("store.update_task(task_dir, _mutator)")
    assert idx_append != -1, "missing _append_events_errors call"
    assert idx_store != -1, "missing store.update_task call"
    assert idx_append < idx_store, (
        "events-error.log write must precede store.update_task (outside flock)"
    )
    # Also: the literal file-open for events-error.log must not appear inside
    # the _mutator function body.
    mutator_start = body.find("def _mutator(d: dict)")
    mutator_end = body.find("return d", mutator_start)
    if mutator_start != -1 and mutator_end != -1:
        mutator_body = body[mutator_start:mutator_end]
        assert "open(" not in mutator_body, (
            "open() found inside mutator — events-error.log must not be "
            "written under the flock"
        )


def test_idempotent_done_reapply_no_status_change(tmp_naiw_data, capsys):
    task_dir = _make_task(tmp_naiw_data, "alpha-001", status="running")
    _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    # First pass: flips to completed.
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=True, as_json=False,
        limit_was_explicit=False,
    )
    data_after_first = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data_after_first["status"] == "completed"
    updated_at_first = data_after_first["updated_at"]
    offset_first = data_after_first["events_offset"]

    # Second pass: offset is already past the event; compute_status receives
    # pending_event_kind=None; container still running → leaked-ctr but
    # status stays completed. updated_at should NOT advance because
    # transitioned=False on the second pass for terminal statuses.
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=True, as_json=False,
        limit_was_explicit=False,
    )
    data_after_second = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data_after_second["status"] == "completed"
    assert data_after_second["events_offset"] == offset_first
    assert data_after_second["updated_at"] == updated_at_first


def test_partial_line_no_offset_advance(tmp_naiw_data, capsys):
    """A partial trailing fragment (no \\n) is not parsed, not malformed,
    and the byte offset stays at the last full-line boundary."""
    task_dir = _make_task(tmp_naiw_data, "alpha-001", status="running")
    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    fragment = json.dumps(
        {"ts": "2026-05-16T10:00:00.000Z", "kind": "done", "payload": {}, "schema_version": 1}
    )  # NO \n
    events_path.write_bytes(fragment.encode("utf-8"))

    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["events_offset"] == 0
    assert data["status"] == "running"
    error_log = task_dir / "meta" / "events-error.log"
    assert not error_log.exists(), "partial line must NOT be classified as malformed"


# ---------- auto_finish / reap path -----------------------------------------


def test_list_does_not_teardown_auto_finish_terminal_event(
    tmp_naiw_data, monkeypatch, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    bytes_written = _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    teardown_calls = []
    monkeypatch.setattr(
        list_cmd,
        "teardown_and_mark",
        lambda *a, **kw: teardown_calls.append(kw),
    )

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert teardown_calls == []
    assert data["status"] == "running"
    assert data["events_offset"] == 0
    assert bytes_written > 0
    assert "auto_finish pending" in _capture(capsys)


def test_list_auto_finish_pending_defers_malformed_diagnostics_to_reap(
    tmp_naiw_data, monkeypatch, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    valid_done = json.dumps(
        {
            "ts": "2026-05-16T10:00:00.000Z",
            "kind": "done",
            "payload": {},
            "schema_version": 1,
        },
        separators=(",", ":"),
    )
    events_path.write_bytes(("{bad json\n" + valid_done + "\n").encode("utf-8"))
    client = _mock_client([_mock_container("alpha-001", state="running")])
    error_log = task_dir / "meta" / "events-error.log"

    for _ in range(2):
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        _capture(capsys)

    assert not error_log.exists()

    def fake_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        pass

    monkeypatch.setattr(list_cmd, "teardown_and_mark", fake_teardown)
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True,
    )

    lines = error_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "json:" in lines[0]


def test_reap_dry_run_does_not_teardown_or_advance_offset(
    tmp_naiw_data, monkeypatch, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])
    teardown_calls = []
    monkeypatch.setattr(
        list_cmd,
        "teardown_and_mark",
        lambda *a, **kw: teardown_calls.append(kw),
    )

    result = _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True, dry_run=True,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert teardown_calls == []
    assert data["events_offset"] == 0
    assert result.would_reap == 1
    assert result.reaped == 0
    assert "auto_finish pending" in _capture(capsys)


def test_reap_does_not_rewind_events_offset_on_stale_task_read(
    tmp_naiw_data, monkeypatch, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])
    teardown_calls = []
    monkeypatch.setattr(
        list_cmd,
        "teardown_and_mark",
        lambda *a, **kw: teardown_calls.append(kw),
    )
    real_update_task = store.update_task

    def stale_update_task(td, mutator):
        def wrapper(d):
            d = dict(d)
            d["events_offset"] = 999
            return mutator(d)

        return real_update_task(td, wrapper)

    monkeypatch.setattr(list_cmd.store, "update_task", stale_update_task)

    result = _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert teardown_calls == []
    assert data["events_offset"] == 999
    assert result.reaped == 0
    _capture(capsys)


def test_reap_dry_run_is_fully_read_only_for_non_auto_finish_tasks(
    tmp_naiw_data, monkeypatch, capsys
):
    """A non-auto_finish task with unread events must NOT be mutated by
    `reap --dry-run`. Previously the hold_offset gate only fired when
    `terminal_auto_pending` was true, so plain done/fail events on
    auto_finish=false tasks would advance offset + flip status to terminal
    on disk even though the operator asked for read-only behavior."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=False
    )
    bytes_written = _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    teardown_calls = []
    monkeypatch.setattr(
        list_cmd,
        "teardown_and_mark",
        lambda *a, **kw: teardown_calls.append(kw),
    )

    before = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True, dry_run=True,
    )
    after = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )

    # Nothing on disk has changed.
    assert before == after
    assert before["events_offset"] == 0
    assert before["status"] == "running"
    assert teardown_calls == []
    assert bytes_written > 0
    _capture(capsys)


def test_reap_dry_run_does_not_write_events_error_log(
    tmp_naiw_data, capsys
):
    """`reap --dry-run` must be fully read-only — including NOT touching
    meta/events-error.log when the unread range contains malformed lines.
    The dry_run_pending gate alone misses this case for non-auto_finish
    tasks; the diagnostic write needs its own dry_run guard."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=False
    )
    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    # One malformed line that would normally land in events-error.log.
    events_path.write_bytes(b"{bad json\n")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True, dry_run=True,
    )

    error_log = task_dir / "meta" / "events-error.log"
    assert not error_log.exists()
    _capture(capsys)


def test_concurrent_finish_does_not_get_overwritten_by_list(
    tmp_naiw_data, monkeypatch, capsys
):
    """CAS over status too: a concurrent `finish` may transition a task to
    `completed` between list's read and its mutator-protected write. The
    mutator must detect the status change and refuse to overwrite the
    fresh terminal state with a stale computed transition."""
    # Disk: running. Reconcile would compute "interrupted" because the
    # container is no longer in the listing.
    task_dir = _make_task(tmp_naiw_data, "alpha-001", status="running")
    client = _mock_client([])  # container not in listing

    real_update_task = store.update_task

    def concurrent_finish_update_task(td, mutator):
        # Simulate another process marking the task `completed` while we
        # hold the read — the wrapped mutator sees the updated status.
        def wrapper(d):
            d = dict(d)
            d["status"] = "completed"
            d["finished_at"] = "2026-05-16T10:00:00.000Z"
            d["updated_at"] = "2026-05-16T10:00:00.000Z"
            return mutator(d)

        return real_update_task(td, wrapper)

    monkeypatch.setattr(list_cmd.store, "update_task", concurrent_finish_update_task)

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, as_json=False, limit_was_explicit=False,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    # Fresh terminal status survives — we did NOT overwrite with "interrupted".
    assert data["status"] == "completed"
    _capture(capsys)


def test_limit_none_renders_all_rows(tmp_naiw_data, capsys):
    for i in range(12):
        _make_task(
            tmp_naiw_data,
            f"alpha-{i + 1:03d}",
            updated_at=f"2026-05-16T10:{i:02d}:00.000Z",
        )
    client = _mock_client(
        [_mock_container(f"alpha-{i + 1:03d}", state="running") for i in range(12)]
    )

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False,
    )

    out = _capture(capsys)
    assert out.count("alpha-") == 12


def test_done_event_with_auto_finish_true_triggers_teardown(
    tmp_naiw_data, monkeypatch, capsys
):
    from naiw_tasks.model import Status

    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    teardown_calls = []

    def fake_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        teardown_calls.append(
            {
                "task_id": task_id,
                "terminal_status": terminal_status,
                "policy_override": policy_override,
                "allow_prompt": allow_prompt,
            }
        )

    monkeypatch.setattr(list_cmd, "teardown_and_mark", fake_teardown)
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
        apply_auto_finish=True,
    )

    assert len(teardown_calls) == 1
    assert teardown_calls[0]["task_id"] == "alpha-001"
    assert teardown_calls[0]["terminal_status"] == Status.COMPLETED
    assert teardown_calls[0]["allow_prompt"] is False
    # Pre-call, events_offset was advanced but status was NOT flipped to
    # completed — the helper writes status itself.
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["events_offset"] > 0
    assert data["status"] == "running"


def test_fail_event_with_auto_finish_true_triggers_teardown_failed(
    tmp_naiw_data, monkeypatch, capsys
):
    from naiw_tasks.model import Status

    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "fail")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    teardown_calls = []

    def fake_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        teardown_calls.append({"terminal_status": terminal_status})

    monkeypatch.setattr(list_cmd, "teardown_and_mark", fake_teardown)
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
        apply_auto_finish=True,
    )

    assert len(teardown_calls) == 1
    assert teardown_calls[0]["terminal_status"] == Status.FAILED


def test_reap_propagates_fail_event_reason_to_teardown(
    tmp_naiw_data, monkeypatch, capsys
):
    """`naiw-signal fail --reason ...` carries a required reason. reap must
    surface it to teardown_and_mark so task.json.failure_reason is set —
    otherwise the operator sees `failed` with no diagnostic and has to grep
    events.jsonl by hand."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    line = json.dumps(
        {
            "ts": "2026-05-16T10:00:00.000Z",
            "kind": "fail",
            "payload": {"reason": "smoke test exited 1"},
            "schema_version": 1,
        }
    ) + "\n"
    events_path.write_bytes(line.encode("utf-8"))
    client = _mock_client([_mock_container("alpha-001", state="running")])

    teardown_calls = []

    def fake_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        teardown_calls.append({
            "terminal_status": terminal_status,
            "failure_reason": failure_reason,
        })

    monkeypatch.setattr(list_cmd, "teardown_and_mark", fake_teardown)
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True,
    )

    assert len(teardown_calls) == 1
    assert teardown_calls[0]["failure_reason"] == "smoke test exited 1"


def test_reap_done_event_does_not_set_failure_reason(
    tmp_naiw_data, monkeypatch, capsys
):
    """`done` events have no reason; teardown_and_mark must be called with
    failure_reason=None so we don't accidentally label a completed task as
    failed."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    teardown_calls = []

    def fake_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        teardown_calls.append({"failure_reason": failure_reason})

    monkeypatch.setattr(list_cmd, "teardown_and_mark", fake_teardown)
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True,
    )

    assert len(teardown_calls) == 1
    assert teardown_calls[0]["failure_reason"] is None


def test_non_auto_finish_fail_event_preserves_payload_reason(
    tmp_naiw_data, capsys
):
    """An `auto_finish=false` task whose Pi writes `naiw-signal fail
    --reason ...` must still record that reason in task.json.failure_reason.
    Reconcile only sees event KIND; the reason payload travels through
    list_cmd directly into the mutator's status-transition write.
    Otherwise the operator sees `failed` with no diagnostic and the
    offset is already past the event."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=False
    )
    events_path = task_dir / "io" / ".naiw" / "events.jsonl"
    line = json.dumps(
        {
            "ts": "2026-05-16T10:00:00.000Z",
            "kind": "fail",
            "payload": {"reason": "smoke test exited 1"},
            "schema_version": 1,
        }
    ) + "\n"
    events_path.write_bytes(line.encode("utf-8"))
    client = _mock_client([_mock_container("alpha-001", state="running")])

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, as_json=False, limit_was_explicit=False,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "failed"
    assert data["failure_reason"] == "smoke test exited 1"


def test_wait_event_flips_to_waiting_for_user_no_container_stop(
    tmp_naiw_data, monkeypatch, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "wait")
    container = _mock_container("alpha-001", state="running")
    client = _mock_client([container])

    teardown_calls = []
    monkeypatch.setattr(
        list_cmd,
        "teardown_and_mark",
        lambda *a, **kw: teardown_calls.append(kw),
    )

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
        apply_auto_finish=True,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "waiting_for_user"
    assert teardown_calls == []
    container.stop.assert_not_called()


def test_auto_finish_done_triggers_three_store_update_task_calls(
    tmp_naiw_data, monkeypatch, capsys
):
    """Atomic-write boundary: 3 store.update_task calls for auto_finish + done.

    Sequence:
      1. _reconcile_one mutator — advance terminal_log_max_size only
         (events_offset held back because will_inline_teardown is True).
      2. teardown_and_mark — write terminal status after stop+remove succeeded.
      3. _advance_offset — advance events_offset only after teardown succeeded.

    The 3-call shape is the cost of crash-safe ordering: if the controller
    dies between teardown and offset advancement, the next reap re-evaluates
    the event but observes status terminal and exits cleanly. Conversely if
    we advanced offset before teardown (the old 2-call shape) and teardown
    raised an unexpected exception, the task would be stuck running forever.
    """
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    real_update = store.update_task
    calls = []

    def spy(td, mutator):
        calls.append("update_task")
        return real_update(td, mutator)

    monkeypatch.setattr(store, "update_task", spy)
    monkeypatch.setattr(list_cmd.store, "update_task", spy)

    # Mock teardown_and_mark to perform exactly one store.update_task call
    # writing terminal status — matches the Plan 04-01 contract.
    def fake_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        def _to_terminal(d):
            d = dict(d)
            d["status"] = str(terminal_status)
            d["finished_at"] = "2026-05-16T10:00:00.000Z"
            d["updated_at"] = "2026-05-16T10:00:00.000Z"
            return d
        store.update_task(td, _to_terminal)

    monkeypatch.setattr(list_cmd, "teardown_and_mark", fake_teardown)

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
        apply_auto_finish=True,
    )

    assert len(calls) == 3, f"expected 3 store.update_task calls, got {len(calls)}"
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "completed"


def test_auto_finish_fail_triggers_three_store_update_task_calls(
    tmp_naiw_data, monkeypatch, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "fail")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    real_update = store.update_task
    calls = []

    def spy(td, mutator):
        calls.append("update_task")
        return real_update(td, mutator)

    monkeypatch.setattr(store, "update_task", spy)
    monkeypatch.setattr(list_cmd.store, "update_task", spy)

    def fake_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        def _to_terminal(d):
            d = dict(d)
            d["status"] = str(terminal_status)
            d["finished_at"] = "2026-05-16T10:00:00.000Z"
            d["updated_at"] = "2026-05-16T10:00:00.000Z"
            return d
        store.update_task(td, _to_terminal)

    monkeypatch.setattr(list_cmd, "teardown_and_mark", fake_teardown)

    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
        apply_auto_finish=True,
    )

    assert len(calls) == 3
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "failed"
    assert data["finished_at"] is not None


def test_auto_finish_teardown_failure_renders_actual_status_not_intent(
    tmp_naiw_data, monkeypatch, capsys
):
    """Render the status lifecycle actually wrote after teardown failure."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "done")
    container = _mock_container("alpha-001", state="running")
    client = _mock_client([container])

    # Match lifecycle's failure path: write failed, then raise SystemExit.
    def failing_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        def _to_failed(d):
            d = dict(d)
            d["status"] = "failed"
            d["failure_reason"] = "finish: container still present after rm"
            d["updated_at"] = "2026-05-16T10:00:00.000Z"
            return d
        store.update_task(td, _to_failed)
        raise SystemExit(1)

    monkeypatch.setattr(list_cmd, "teardown_and_mark", failing_teardown)

    # --all keeps either terminal bucket visible.
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=True, include_completed=False, as_json=True,
        limit_was_explicit=False,
        apply_auto_finish=True,
    )
    payload = json.loads(_capture(capsys))
    assert len(payload["tasks"]) == 1
    rendered = payload["tasks"][0]
    assert rendered["status"] == "failed", (
        "rendered status must reflect on-disk reality, not pre-teardown intent"
    )
    assert "leaked ctr" in rendered["notes"]
    on_disk = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert on_disk["status"] == "failed"


def test_auto_finish_teardown_failure_can_be_retried_by_reap(
    tmp_naiw_data, monkeypatch, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])
    calls = []

    def flaky_teardown(
        cfg, client, task_id, td, *, terminal_status,
        policy_override, allow_prompt, failure_reason=None
    ):
        calls.append(terminal_status)
        if len(calls) == 1:
            def _to_failed(d):
                d = dict(d)
                d["status"] = "failed"
                d["failure_reason"] = "finish: container still present after rm"
                d["updated_at"] = "2026-05-16T10:00:00.000Z"
                return d

            store.update_task(td, _to_failed)
            raise SystemExit(1)

        def _to_terminal(d):
            d = dict(d)
            d["status"] = str(terminal_status)
            d["finished_at"] = "2026-05-16T10:01:00.000Z"
            d["updated_at"] = "2026-05-16T10:01:00.000Z"
            return d

        store.update_task(td, _to_terminal)

    monkeypatch.setattr(list_cmd, "teardown_and_mark", flaky_teardown)

    first = _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True,
    )
    _capture(capsys)
    after_first = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert first.reaped == 0
    assert after_first["events_offset"] == 0
    assert after_first["status"] == "failed"

    def reopen_for_retry(d):
        d = dict(d)
        d["status"] = "running"
        return d

    store.update_task(task_dir, reopen_for_retry)
    second = _run_list(
        _cfg(tmp_naiw_data), client,
        limit=None, statuses=[], project_filter=None,
        show_all=True, include_completed=True, as_json=False,
        limit_was_explicit=False, apply_auto_finish=True,
    )
    _capture(capsys)

    assert len(calls) == 2
    assert second.reaped == 1
    assert json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )["status"] == "completed"


def test_auto_finish_unexpected_exception_does_not_consume_event(
    tmp_naiw_data, monkeypatch, capsys
):
    """If teardown_and_mark raises an unexpected exception (not SystemExit,
    which is the controlled-failure path), the event offset must NOT be
    advanced — otherwise the task is stuck running forever.

    Holding the offset back in the pre-teardown mutator (rather than rolling
    back after the fact) means this guarantee survives controller kill +
    arbitrary exception classes, not just SystemExit."""
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running", auto_finish=True
    )
    bytes_written = _write_event_line(task_dir, "done")
    client = _mock_client([_mock_container("alpha-001", state="running")])

    def explosive_teardown(*args, **kwargs):
        raise RuntimeError("simulated controller killed mid-teardown")

    monkeypatch.setattr(list_cmd, "teardown_and_mark", explosive_teardown)

    with pytest.raises(RuntimeError):
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=None, statuses=[], project_filter=None,
            show_all=True, as_json=False,
            limit_was_explicit=False, apply_auto_finish=True,
        )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    # Offset must still be 0 so the next reap re-evaluates the event.
    assert data["events_offset"] == 0
    assert bytes_written > 0


# ---------- DATA-08 monotonic-growth (lstat-enforced) ----------------------


@pytest.mark.parametrize(
    "subcase",
    ["first_run", "growth", "missing_file"],
)
def test_data08_terminal_log_max_size_tracked(subcase, tmp_naiw_data, capsys):
    if subcase == "first_run":
        task_dir = _make_task(
            tmp_naiw_data, "alpha-001", status="running",
            terminal_log_max_size=0,
        )
        (task_dir / "io" / "terminal.log").write_bytes(b"x" * 500)
        client = _mock_client([_mock_container("alpha-001", state="running")])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        data = json.loads(
            (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
        )
        assert data["terminal_log_max_size"] == 500

    elif subcase == "growth":
        task_dir = _make_task(
            tmp_naiw_data, "alpha-001", status="running",
            terminal_log_max_size=500,
        )
        (task_dir / "io" / "terminal.log").write_bytes(b"x" * 800)
        client = _mock_client([_mock_container("alpha-001", state="running")])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        data = json.loads(
            (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
        )
        assert data["terminal_log_max_size"] == 800

    elif subcase == "missing_file":
        task_dir = _make_task(
            tmp_naiw_data, "alpha-001", status="running",
            terminal_log_max_size=0,
        )
        # No terminal.log created.
        client = _mock_client([_mock_container("alpha-001", state="running")])
        _run_list(
            _cfg(tmp_naiw_data), client,
            limit=10, statuses=[], project_filter=None,
            show_all=False, include_completed=False, as_json=False,
            limit_was_explicit=False,
        )
        out = _capture(capsys)
        assert "(log shrunk)" not in out
        data = json.loads(
            (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
        )
        assert data["terminal_log_max_size"] == 0


def test_data08_uses_lstat_not_stat():
    """Source-text guard: list_cmd MUST call .lstat() on terminal.log, NEVER
    a symlink-following .stat() on it. A Pi-side symlink at io/terminal.log
    pointing to a controller-side file would otherwise leak that file's size
    into the monotonic-growth tracker."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "list_cmd.py"
    ).read_text(encoding="utf-8")
    # At least one lstat call on terminal_log path.
    assert re.search(r"terminal_log\.lstat\(\)", src), (
        "expected terminal_log.lstat() in list_cmd.py"
    )
    # No symlink-following .stat() on terminal_log.
    assert not re.search(r"terminal_log\.stat\(\)", src), (
        "list_cmd must NOT call terminal_log.stat() (symlink-following)"
    )


def test_data08_log_shrunk_marker_appears_on_shrink(tmp_naiw_data, capsys):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running",
        terminal_log_max_size=1000,
    )
    (task_dir / "io" / "terminal.log").write_bytes(b"x" * 300)
    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    assert "(log shrunk)" in out
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["terminal_log_max_size"] == 1000  # not lowered


def test_data08_marker_persists_until_size_returns_above_prior_max(
    tmp_naiw_data, capsys
):
    task_dir = _make_task(
        tmp_naiw_data, "alpha-001", status="running",
        terminal_log_max_size=1000,
    )
    # Grow to 1500 — no shrink marker; max updates.
    (task_dir / "io" / "terminal.log").write_bytes(b"x" * 1500)
    client = _mock_client([_mock_container("alpha-001", state="running")])
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    assert "(log shrunk)" not in out
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["terminal_log_max_size"] == 1500

    # Shrink to 800 — marker appears; max stays 1500.
    (task_dir / "io" / "terminal.log").write_bytes(b"x" * 800)
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    assert "(log shrunk)" in out
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["terminal_log_max_size"] == 1500

    # Grow back to 1600 — marker clears; max updates.
    (task_dir / "io" / "terminal.log").write_bytes(b"x" * 1600)
    _run_list(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)
    assert "(log shrunk)" not in out
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["terminal_log_max_size"] == 1600


# ---------- module hygiene --------------------------------------------------


def test_list_cmd_module_has_no_gsd_refs():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "list_cmd.py"
    ).read_text(encoding="utf-8")
    bad = re.search(
        r"\bD-[0-9]+|\bPhase [0-9]+|\bPlan [0-9]+|\bRESEARCH\b|"
        r"\bCTRL-[0-9]+|\bHARD-[0-9]+|\bDATA-[0-9]+|\bGIT-[0-9]+|"
        r"\bPROJ-[0-9]+|\bPROXY-[0-9]+|\bIMG-[0-9]+|\bSIG-[0-9]+|"
        r"\bLIST-[0-9]+",
        src,
    )
    assert bad is None, (
        f"forbidden token in list_cmd.py: {bad.group(0) if bad else None}"
    )


# ---------- auto_finish artifact capture inheritance --------------------------


def _make_running_task_with_done_event(
    tmp_path,
    task_id: str = "alpha-001",
    kind: str = "generic",
):
    """Seed a running auto_finish task with a `done` event on disk.

    Returns (cfg, task_dir). Reuses the module-top _make_task fixture; adds
    a Pi-side terminal.log + summary.md so the real capture helper has
    source content to read via its O_NOFOLLOW-defended path.
    """
    project = None if kind == "generic" else "alpha"
    task_dir = _make_task(
        tmp_path, task_id, status="running",
        auto_finish=True, kind=kind, project=project,
    )
    (task_dir / "io" / "terminal.log").write_text("seeded log\n")
    (task_dir / "io" / "summary.md").write_text("# done\n")
    _write_event_line(task_dir, "done")
    return _cfg(tmp_path), task_dir


def test_reap_auto_finish_captures_artifacts(tmp_naiw_data):
    """End-to-end through list_cmd.run(apply_auto_finish=True) WITHOUT
    monkeypatching teardown_and_mark — so the real capture helper fires
    and writes meta/artifacts/ during the reap path.
    """
    cfg, task_dir = _make_running_task_with_done_event(
        tmp_naiw_data, "alpha-001", kind="generic",
    )
    # Build a client that mirrors real Docker semantics: containers.list
    # returns the alive container for the LIST query; containers.get for
    # the teardown path returns a stoppable container the first time and
    # raises NotFound after .remove() is called (so the survivor verify
    # inside teardown_and_mark passes).
    listed_ctr = _mock_container("alpha-001", state="running")
    teardown_ctr = MagicMock()
    teardown_ctr.attrs = {"State": {"Status": "running"}}

    client = MagicMock()
    client.containers.list.return_value = [listed_ctr]

    def _get(name):
        if teardown_ctr.remove.called:
            raise docker.errors.NotFound(f"{name} removed")
        return teardown_ctr

    client.containers.get = MagicMock(side_effect=_get)

    _run_list(
        cfg, client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
        apply_auto_finish=True,
    )

    artifacts_dir = task_dir / "meta" / "artifacts"
    assert artifacts_dir.exists(), "auto_finish must create meta/artifacts/"
    # Generic-task contract: terminal.log + summary.md always; the three
    # git-driven captures only fire for project tasks.
    assert (artifacts_dir / "terminal.log").exists()
    assert (artifacts_dir / "terminal.log").read_text() == "seeded log\n"
    assert (artifacts_dir / "summary.md").exists()
    assert not (artifacts_dir / "git-status.txt").exists()
    assert not (artifacts_dir / "diff.patch").exists()
    assert not (artifacts_dir / "changed-files.txt").exists()

    # Status was flipped via teardown_and_mark.
    from naiw_tasks.model import Status
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8"),
    )
    assert data["status"] == str(Status.COMPLETED)
