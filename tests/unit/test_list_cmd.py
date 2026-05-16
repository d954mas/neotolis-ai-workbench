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
auto_finish done/fail → _teardown_and_mark direct call, wait → status flip
only (no container call), 2 store.update_task calls under auto_finish,
DATA-08 monotonic-growth lstat + shrink marker behaviour.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
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


def _cfg(data_root: Path) -> Config:
    return Config(data_root=data_root)


def _capture(capsys) -> str:
    out, _ = capsys.readouterr()
    return out


# ---------- default scope (D-13) -------------------------------------------


def test_default_scope_excludes_terminal_statuses(tmp_naiw_data, capsys):
    _make_task(tmp_naiw_data, "alpha-001", status="running")
    _make_task(tmp_naiw_data, "alpha-002", status="completed")
    _make_task(tmp_naiw_data, "alpha-003", status="failed")
    _make_task(tmp_naiw_data, "alpha-004", status="cancelled")
    _make_task(tmp_naiw_data, "alpha-005", status="interrupted")
    client = _mock_client([])

    list_cmd.run(
        _cfg(tmp_naiw_data),
        client,
        limit=10,
        statuses=[],
        project_filter=None,
        show_all=False,
        include_completed=False,
        as_json=False,
        limit_was_explicit=False,
    )
    out = _capture(capsys)

    assert "alpha-001" in out
    assert "alpha-005" in out
    assert "alpha-002" not in out
    assert "alpha-003" not in out
    assert "alpha-004" not in out


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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
    list_cmd.run(
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


# ---------- single containers.list call -------------------------------------


def test_single_containers_list_call_indexed_by_task_id_label(tmp_naiw_data, capsys):
    for i in range(1, 6):
        _make_task(tmp_naiw_data, f"alpha-{i:03d}", status="running")
    client = _mock_client([])
    list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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

    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
    line = json.dumps(
        {"ts": ts, "kind": kind, "payload": {}, "schema_version": 1}
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
    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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


# ---------- auto_finish path (the Q5 landmine fix) -------------------------


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

    def fake_teardown(cfg, client, task_id, td, *, terminal_status, policy_override):
        teardown_calls.append(
            {
                "task_id": task_id,
                "terminal_status": terminal_status,
                "policy_override": policy_override,
            }
        )

    monkeypatch.setattr(list_cmd, "_teardown_and_mark", fake_teardown)
    list_cmd.run(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )

    assert len(teardown_calls) == 1
    assert teardown_calls[0]["task_id"] == "alpha-001"
    assert teardown_calls[0]["terminal_status"] == Status.COMPLETED
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

    def fake_teardown(cfg, client, task_id, td, *, terminal_status, policy_override):
        teardown_calls.append({"terminal_status": terminal_status})

    monkeypatch.setattr(list_cmd, "_teardown_and_mark", fake_teardown)
    list_cmd.run(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )

    assert len(teardown_calls) == 1
    assert teardown_calls[0]["terminal_status"] == Status.FAILED


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
        "_teardown_and_mark",
        lambda *a, **kw: teardown_calls.append(kw),
    )

    list_cmd.run(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )

    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "waiting_for_user"
    assert teardown_calls == []
    container.stop.assert_not_called()


def test_auto_finish_done_triggers_two_store_update_task_calls(
    tmp_naiw_data, monkeypatch, capsys
):
    """Atomic-write boundary: 2 store.update_task calls for auto_finish + done.
    One in _reconcile_one for offset+max_size advance; one inside
    _teardown_and_mark for terminal status after teardown."""
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

    # Mock _teardown_and_mark to perform exactly one store.update_task call
    # writing terminal status — matches the Plan 04-01 contract.
    def fake_teardown(cfg, client, task_id, td, *, terminal_status, policy_override):
        def _to_terminal(d):
            d = dict(d)
            d["status"] = str(terminal_status)
            d["finished_at"] = "2026-05-16T10:00:00.000Z"
            d["updated_at"] = "2026-05-16T10:00:00.000Z"
            return d
        store.update_task(td, _to_terminal)

    monkeypatch.setattr(list_cmd, "_teardown_and_mark", fake_teardown)

    list_cmd.run(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )

    assert len(calls) == 2, f"expected 2 store.update_task calls, got {len(calls)}"
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "completed"


def test_auto_finish_fail_triggers_two_store_update_task_calls(
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

    def fake_teardown(cfg, client, task_id, td, *, terminal_status, policy_override):
        def _to_terminal(d):
            d = dict(d)
            d["status"] = str(terminal_status)
            d["finished_at"] = "2026-05-16T10:00:00.000Z"
            d["updated_at"] = "2026-05-16T10:00:00.000Z"
            return d
        store.update_task(td, _to_terminal)

    monkeypatch.setattr(list_cmd, "_teardown_and_mark", fake_teardown)

    list_cmd.run(
        _cfg(tmp_naiw_data), client,
        limit=10, statuses=[], project_filter=None,
        show_all=False, include_completed=False, as_json=False,
        limit_was_explicit=False,
    )

    assert len(calls) == 2
    data = json.loads(
        (task_dir / "meta" / "task.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "failed"
    assert data["finished_at"] is not None


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
        list_cmd.run(
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
        list_cmd.run(
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
        list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
    list_cmd.run(
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
