"""Tests for naiw_tasks.model — Status (7 vals), TaskKind, FinishPolicy, Task."""

import json
import re
from pathlib import Path

from naiw_tasks.model import FinishPolicy, Status, Task, TaskKind

from naiw_tasks import model as model_mod


def test_status_enum_has_exactly_seven_values() -> None:
    expected = [
        ("CREATED", "created"),
        ("RUNNING", "running"),
        ("INTERRUPTED", "interrupted"),
        ("WAITING_FOR_USER", "waiting_for_user"),
        ("COMPLETED", "completed"),
        ("FAILED", "failed"),
        ("CANCELLED", "cancelled"),
    ]
    assert len(list(Status)) == 7
    for name, value in expected:
        assert hasattr(Status, name), f"Status.{name} missing"
        assert str(getattr(Status, name)) == value, (
            f"Status.{name} string value drift: got {str(getattr(Status, name))!r}, "
            f"expected {value!r}"
        )
    assert {m.name for m in Status} == {n for n, _ in expected}


def test_status_from_str_lenient_returns_enum_for_each_of_seven() -> None:
    for value in (
        "created",
        "running",
        "interrupted",
        "waiting_for_user",
        "completed",
        "failed",
        "cancelled",
    ):
        result = Status.from_str_lenient(value)
        assert result is getattr(Status, value.upper()), (
            f"from_str_lenient({value!r}) returned {result!r}, "
            f"expected Status.{value.upper()}"
        )


def test_status_from_str_lenient_returns_raw_for_unknown() -> None:
    # Forward-compat: unknown statuses (a future controller writes a string this
    # binary does not know) must pass through as the raw string, never raise.
    assert Status.from_str_lenient("future_status_xyz") == "future_status_xyz"


def test_status_round_trips_through_json() -> None:
    encoded = json.dumps({"status": Status.RUNNING})
    decoded = json.loads(encoded)
    assert decoded == {"status": "running"}


def test_unknown_status_string_does_not_raise() -> None:
    # Future statuses (interrupted, waiting_for_user, cancelled) must not crash
    # the controller — status is a string field, not a tagged-union sum type.
    result = Status.from_str_lenient("interrupted")
    assert result == "interrupted"


def test_known_status_string_returns_enum() -> None:
    result = Status.from_str_lenient("running")
    assert result == Status.RUNNING


def test_finish_policy_enum_three_values() -> None:
    assert set(FinishPolicy) == {
        FinishPolicy.ASK,
        FinishPolicy.KEEP_WORKTREE,
        FinishPolicy.DELETE_WORKTREE,
    }
    assert FinishPolicy.ASK.value == "ask"
    assert FinishPolicy.KEEP_WORKTREE.value == "keep_worktree"
    assert FinishPolicy.DELETE_WORKTREE.value == "delete_worktree"


def test_task_kind_enum_two_values() -> None:
    assert set(TaskKind) == {TaskKind.PROJECT, TaskKind.GENERIC}
    assert TaskKind.PROJECT.value == "project"
    assert TaskKind.GENERIC.value == "generic"


def _minimal_task(**overrides: object) -> Task:
    base = {
        "id": "neotolis-engine-001",
        "kind": TaskKind.PROJECT,
        "container_name": "naiw-task-neotolis-engine-001",
        "image_tag": "naiw-task-image:latest",
        "created_at": "2026-05-11T14:32:00.123Z",
        "updated_at": "2026-05-11T14:32:00.123Z",
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def test_task_dataclass_round_trips_through_asdict() -> None:
    task = _minimal_task()
    d = task.as_dict()
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded == d


def test_task_default_fields() -> None:
    task = _minimal_task()
    assert task.events_offset == 0
    assert task.recovery_count == 0
    assert task.recovery_history == []
    assert task.secrets == []
    assert task.labels == {}
    assert task.status == Status.CREATED
    assert task.finish_policy == FinishPolicy.ASK
    assert task.auto_finish is False
    assert task.failure_reason is None
    assert task.image_digest is None


def test_task_as_dict_emits_strings_for_enums() -> None:
    task = _minimal_task(status=Status.RUNNING, finish_policy=FinishPolicy.DELETE_WORKTREE)
    d = task.as_dict()
    assert d["status"] == "running"
    assert d["finish_policy"] == "delete_worktree"
    assert d["kind"] == "project"


def test_schema_version_constant() -> None:
    assert model_mod.SCHEMA_VERSION == 1
    task = _minimal_task()
    assert task.as_dict()["schema_version"] == 1


def test_task_full_field_set_present_in_asdict() -> None:
    # Locked task.json schema: every field present from day one.
    task = _minimal_task()
    d = task.as_dict()
    expected = {
        "schema_version", "id", "kind", "project", "container_name",
        "image_tag", "image_digest", "status", "failure_reason",
        "created_at", "started_at", "updated_at", "finished_at",
        "finish_policy", "auto_finish", "branch", "worktree_path",
        "base_branch", "base_commit", "project_repo_path",
        "labels", "secrets",
        "events_offset", "terminal_log_max_size",
        "recovery_count", "recovery_history",
    }
    assert set(d.keys()) == expected


def test_task_terminal_log_max_size_defaults_to_zero() -> None:
    t = _minimal_task()
    assert t.terminal_log_max_size == 0
    assert t.as_dict()["terminal_log_max_size"] == 0


def test_task_terminal_log_max_size_round_trips() -> None:
    t = _minimal_task(terminal_log_max_size=12345)
    assert t.terminal_log_max_size == 12345
    assert t.as_dict()["terminal_log_max_size"] == 12345


def test_task_constructs_from_legacy_dict_without_terminal_log_max_size() -> None:
    # Legacy task.json written by Phase 3 controllers will not carry the
    # terminal_log_max_size key. Constructing Task(**legacy) must succeed via
    # the dataclass default, NOT raise TypeError — that preserves the
    # "updates must not break in-flight task.json state" invariant.
    legacy = {
        "id": "neotolis-engine-001",
        "kind": TaskKind.PROJECT,
        "container_name": "naiw-task-neotolis-engine-001",
        "image_tag": "naiw-task-image:latest",
        "created_at": "2026-05-11T14:32:00.123Z",
        "updated_at": "2026-05-11T14:32:00.123Z",
        "status": Status.RUNNING,
        "failure_reason": None,
        "started_at": "2026-05-11T14:32:01.000Z",
        "finished_at": None,
        "image_digest": "sha256:abc",
        "finish_policy": FinishPolicy.ASK,
        "auto_finish": False,
        "project": "neotolis-engine",
        "branch": "agent/neotolis-engine-001",
        "worktree_path": "/work",
        "base_branch": "origin/main",
        "base_commit": "abc1234",
        "project_repo_path": "/repo",
        "labels": {},
        "secrets": [],
        "events_offset": 0,
        # terminal_log_max_size intentionally OMITTED — legacy schema.
        "recovery_count": 0,
        "recovery_history": [],
        "schema_version": 1,
    }
    t = Task(**legacy)
    assert t.terminal_log_max_size == 0


def test_model_module_has_no_gsd_refs() -> None:
    # No planning artifacts may leak into source. Allowed-doc files
    # (CLAUDE.md, AGENTS.md, README.md, task.md) are exempt; model.py is not.
    src = Path(model_mod.__file__).read_text(encoding="utf-8")
    forbidden = [
        r"\bD-\d{2}\b",
        r"\bPhase [0-9]",
        r"\bRESEARCH\b",
        r"\bPlan [0-9]",
        r"\bLIST-\d{2}\b",
        r"\bSIG-\d{2}\b",
        r"\bDATA-\d{2}\b",
        r"\bCTRL-\d{2}\b",
    ]
    for pattern in forbidden:
        match = re.search(pattern, src)
        assert match is None, (
            f"GSD/planning ref {match.group()!r} leaked into model.py — "
            f"strip the comment (allowed-doc files only)"
        )
