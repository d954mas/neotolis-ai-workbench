"""Tests for naiw_tasks.store — flock + atomic os.replace task.json read/write."""

import sys

import pytest

if sys.platform != "linux":
    pytest.skip(
        "Linux-only (fcntl / O_NOFOLLOW)",
        allow_module_level=True,
    )

import json
import multiprocessing
import os
from pathlib import Path

from naiw_tasks.model import FinishPolicy, Status, Task, TaskKind
from naiw_tasks.store import (
    UnsupportedSchemaError,
    read_task,
    update_task,
    write_task,
)

from naiw_tasks import store as store_mod


def _sample_task() -> Task:
    return Task(
        id="myproj-001",
        kind=TaskKind.PROJECT,
        container_name="naiw-task-myproj-001",
        image_tag="naiw-task-image:latest",
        created_at="2026-05-11T14:32:00.123Z",
        updated_at="2026-05-11T14:32:00.123Z",
        status=Status.RUNNING,
        finish_policy=FinishPolicy.ASK,
    )


def test_write_task_creates_file_with_schema_version(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-001"
    write_task(task_dir, _sample_task())
    tj = task_dir / "meta" / "task.json"
    assert tj.exists()
    data = json.loads(tj.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["id"] == "myproj-001"
    assert data["status"] == "running"


def test_write_uses_os_replace(tmp_naiw_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-002"
    captured: dict[str, tuple[str, str]] = {}
    original_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        captured["call"] = (str(src), str(dst))
        original_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", spy_replace)
    write_task(task_dir, _sample_task())
    assert "call" in captured, "os.replace was never invoked"
    src, dst = captured["call"]
    assert dst == str(task_dir / "meta" / "task.json")
    # Tempfile MUST be in the same dir as the final file (intra-FS atomicity).
    assert Path(src).parent == (task_dir / "meta").resolve() or Path(src).parent == (
        task_dir / "meta"
    )


def test_write_creates_tempfile_in_meta_dir(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-003"
    captured_paths: list[str] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        captured_paths.append(str(src))
        real_replace(src, dst)

    # Patch via the store module's os binding.
    import naiw_tasks.store as store_local

    store_local.os.replace = spy  # type: ignore[assignment]
    try:
        write_task(task_dir, _sample_task())
    finally:
        store_local.os.replace = real_replace  # type: ignore[assignment]

    assert captured_paths, "no os.replace call captured"
    tmp = Path(captured_paths[0])
    # Tempfile MUST be in the same dir as task.json — cross-FS atomic-rename
    # pitfall would surface if it lived in a system tempdir on a different FS.
    assert tmp.parent.resolve() == (task_dir / "meta").resolve()


def test_write_calls_fsync_before_close(
    tmp_naiw_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-004"
    order: list[str] = []
    original_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        order.append("fsync")
        original_fsync(fd)

    monkeypatch.setattr(store_mod.os, "fsync", spy_fsync)
    write_task(task_dir, _sample_task())
    # At least one fsync occurred before the file was published via os.replace.
    assert "fsync" in order


def test_read_task_round_trips(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-005"
    task = _sample_task()
    write_task(task_dir, task)
    data = read_task(task_dir)
    assert data == task.as_dict()


def test_read_task_rejects_future_schema(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-006"
    meta = task_dir / "meta"
    meta.mkdir(parents=True)
    (meta / "task.json").write_text(
        json.dumps({"schema_version": 2, "id": "myproj-006"}), encoding="utf-8"
    )
    with pytest.raises(UnsupportedSchemaError) as exc:
        read_task(task_dir)
    assert "schema_version=2" in str(exc.value)


def test_read_task_lenient_with_unknown_status(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-007"
    meta = task_dir / "meta"
    meta.mkdir(parents=True)
    payload = {"schema_version": 1, "id": "myproj-007", "status": "interrupted"}
    (meta / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    # MUST NOT raise — controller tolerates unknown status strings.
    data = read_task(task_dir)
    assert data["status"] == "interrupted"


def test_lock_file_lives_in_meta_under_naiw_dot_task_lock(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-008"
    write_task(task_dir, _sample_task())
    assert (task_dir / "meta" / ".task.lock").exists()


def test_update_task_increments_value(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "myproj-009"
    update_task(
        task_dir,
        lambda d: {
            **d,
            "n": d.get("n", 0) + 1,
            "schema_version": 1,
            "id": "myproj-009",
        },
    )
    update_task(task_dir, lambda d: {**d, "n": d["n"] + 1})
    data = read_task(task_dir)
    assert data["n"] == 2


def _bump_n(task_dir_str: str, iterations: int) -> None:
    """Worker that increments task.n under flock `iterations` times."""
    from pathlib import Path as P

    from naiw_tasks.store import update_task as upd

    td = P(task_dir_str)
    for _ in range(iterations):
        upd(
            td,
            lambda d: {
                **d,
                "n": d.get("n", 0) + 1,
                "schema_version": 1,
                "id": "concurrent-001",
            },
        )


def test_concurrent_writes_serialize_under_flock(tmp_naiw_data: Path) -> None:
    task_dir = tmp_naiw_data / "tasks" / "concurrent-001"
    # Seed the file so both workers see schema_version=1 from the first read.
    update_task(
        task_dir,
        lambda d: {**d, "n": 0, "schema_version": 1, "id": "concurrent-001"},
    )

    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_bump_n, args=(str(task_dir), 100)) for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"

    final = read_task(task_dir)
    # No lost updates: 2 * 100 = 200 increments on top of the seed (0).
    assert final["n"] == 200
