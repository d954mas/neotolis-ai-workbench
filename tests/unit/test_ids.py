"""Tests for naiw_tasks.ids — task-id format + per-project monotonic counter."""

import multiprocessing
from pathlib import Path

import pytest

from naiw_tasks.ids import (
    PROJECT_ALIAS_RE,
    TASK_ID_RE,
    allocate_task_id,
    validate_project_alias,
    validate_task_id,
)


def test_task_id_regex_literal() -> None:
    assert TASK_ID_RE.pattern == r"^[a-z0-9][a-z0-9-]{0,63}$"


def test_project_alias_regex_literal() -> None:
    """Project alias is capped at 60 chars so `<alias>-NNN` fits the 64-char
    container-name budget after the `naiw-task-` prefix."""
    assert PROJECT_ALIAS_RE.pattern == r"^[a-z0-9][a-z0-9-]{0,59}$"


def test_validate_project_alias_accepts_dns_label_shape() -> None:
    validate_project_alias("alpha")
    validate_project_alias("a")
    validate_project_alias("a0-9b-3")
    validate_project_alias("neotolis-engine")
    # 60 chars — boundary
    validate_project_alias("a" + "0" * 59)


def test_validate_project_alias_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_project_alias("Alpha")  # uppercase
    assert "invalid project alias" in str(excinfo.value)
    with pytest.raises(ValueError):
        validate_project_alias("-alpha")  # leading dash
    with pytest.raises(ValueError):
        validate_project_alias("")  # empty
    with pytest.raises(ValueError):
        validate_project_alias("a" + "0" * 60)  # 61 chars — over budget
    with pytest.raises(ValueError):
        validate_project_alias("bad name!")  # space + punctuation


def test_validate_task_id_accepts_dns_label_shape() -> None:
    validate_task_id("foo-001")
    validate_task_id("a")
    validate_task_id("a0-9b-3")
    validate_task_id("neotolis-engine-001")
    # 64 chars total — boundary.
    validate_task_id("a" + "0" * 63)


def test_validate_task_id_rejects_uppercase_or_leading_dash() -> None:
    with pytest.raises(ValueError):
        validate_task_id("Foo")
    with pytest.raises(ValueError):
        validate_task_id("-foo")
    with pytest.raises(ValueError):
        validate_task_id("")
    with pytest.raises(ValueError):
        validate_task_id("a" * 65)


def test_allocate_first_id_for_project(tmp_naiw_data: Path) -> None:
    new_id = allocate_task_id(tmp_naiw_data, "myproj")
    assert new_id == "myproj-001"
    counter = (tmp_naiw_data / ".counters" / "myproj.txt").read_text(encoding="utf-8").strip()
    assert counter == "1"


def test_allocate_is_monotonic(tmp_naiw_data: Path) -> None:
    a = allocate_task_id(tmp_naiw_data, "myproj")
    b = allocate_task_id(tmp_naiw_data, "myproj")
    c = allocate_task_id(tmp_naiw_data, "myproj")
    assert (a, b, c) == ("myproj-001", "myproj-002", "myproj-003")


def test_allocate_for_generic_task(tmp_naiw_data: Path) -> None:
    new_id = allocate_task_id(tmp_naiw_data, "task")
    assert new_id == "task-001"


def test_counter_skips_existing_dirs(tmp_naiw_data: Path) -> None:
    # Operator hand-deleted .counters/myproj.txt but tasks/myproj-005/ still exists.
    (tmp_naiw_data / "tasks" / "myproj-005").mkdir()
    new_id = allocate_task_id(tmp_naiw_data, "myproj")
    assert new_id == "myproj-006"


def test_counter_starts_at_one_when_no_existing_dirs(tmp_naiw_data: Path) -> None:
    # Empty counters + empty tasks → first id is -001.
    new_id = allocate_task_id(tmp_naiw_data, "fresh")
    assert new_id == "fresh-001"


def _allocate_many(data_root_str: str, project: str, n: int, q: object) -> None:
    """Worker for the concurrency test — allocate n ids and put each on the queue."""
    from pathlib import Path as P

    from naiw_tasks.ids import allocate_task_id as alloc

    for _ in range(n):
        q.put(alloc(P(data_root_str), project))


@pytest.mark.skipif(
    not hasattr(multiprocessing, "get_start_method"),
    reason="multiprocessing.get_start_method unavailable",
)
def test_concurrent_allocate_no_collision(tmp_naiw_data: Path) -> None:
    ctx = multiprocessing.get_context("spawn")  # 'spawn' is portable across platforms
    q: multiprocessing.Queue[str] = ctx.Queue()
    procs = [
        ctx.Process(
            target=_allocate_many, args=(str(tmp_naiw_data), "myproj", 4, q)
        )
        for _ in range(5)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"

    ids: list[str] = []
    while not q.empty():
        ids.append(q.get())

    assert len(ids) == 20
    assert len(set(ids)) == 20, f"duplicate ids found: {sorted(ids)}"
    # Every id must match TASK_ID_RE and resolve to a number 1..20.
    nums = sorted(int(i.rsplit("-", 1)[-1]) for i in ids)
    assert nums == list(range(1, 21))
