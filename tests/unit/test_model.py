"""Tests for naiw_tasks.model — Status (4 vals), TaskKind, FinishPolicy, Task."""

import json

from naiw_tasks import model as model_mod
from naiw_tasks.model import FinishPolicy, Status, Task, TaskKind


def test_status_enum_has_exactly_four_values() -> None:
    assert set(Status) == {
        Status.CREATED,
        Status.RUNNING,
        Status.COMPLETED,
        Status.FAILED,
    }
    assert Status.CREATED.value == "created"
    assert Status.RUNNING.value == "running"
    assert Status.COMPLETED.value == "completed"
    assert Status.FAILED.value == "failed"


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
        "events_offset", "recovery_count", "recovery_history",
    }
    assert set(d.keys()) == expected
