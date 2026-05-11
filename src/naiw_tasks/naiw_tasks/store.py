"""Atomic flock-protected read/write of meta/task.json.

Single recipe used everywhere task.json is mutated:
  open(meta/.task.lock) -> flock(LOCK_EX) -> json read -> mutator(d) ->
  tempfile.NamedTemporaryFile(dir=meta) -> write -> fsync -> close ->
  os.replace(tmp, task.json)

Tempfile MUST live in the same directory as task.json so os.replace is an
intra-FS rename (atomic on POSIX). fsync MUST happen before close so a power
loss after the rename never publishes a zero-byte file.
"""

import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from naiw_tasks.model import SCHEMA_VERSION, Task

TASK_JSON_NAME = "task.json"
LOCK_NAME = ".task.lock"


class UnsupportedSchemaError(ValueError):
    """task.json on disk has a schema_version the controller cannot read."""


def _meta_dir(task_dir: Path) -> Path:
    md = task_dir / "meta"
    md.mkdir(parents=True, exist_ok=True)
    return md


def _open_lock(task_dir: Path) -> int:
    lock_path = _meta_dir(task_dir) / LOCK_NAME
    return os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)


def write_task(task_dir: Path, task: Task) -> None:
    """Write task.json atomically under per-task flock."""
    update_task(task_dir, lambda _: task.as_dict())


def read_task(task_dir: Path) -> dict[str, Any]:
    """Read task.json under shared flock. Raises UnsupportedSchemaError on schema bump."""
    meta = _meta_dir(task_dir)
    tj = meta / TASK_JSON_NAME
    lock_fd = _open_lock(task_dir)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        if not tj.exists():
            raise FileNotFoundError(tj)
        data = json.loads(tj.read_text(encoding="utf-8"))
    finally:
        os.close(lock_fd)

    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"task.json at {tj}: schema_version={sv} not supported "
            f"(controller supports schema_version={SCHEMA_VERSION})"
        )
    return data


def update_task(
    task_dir: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Read-modify-write task.json under exclusive flock. Returns new state."""
    meta = _meta_dir(task_dir)
    tj = meta / TASK_JSON_NAME
    lock_fd = _open_lock(task_dir)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        if tj.exists():
            data = json.loads(tj.read_text(encoding="utf-8"))
        else:
            data = {}

        new_data = mutator(data)

        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            # Same FS as final — os.replace is atomic only intra-FS.
            dir=str(meta),
            delete=False,
            encoding="utf-8",
            prefix=".task.json.",
            suffix=".tmp",
        )
        try:
            json.dump(new_data, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            # Without fsync, replace can publish a 0-byte file on power loss.
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, str(tj))
        except Exception:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except FileNotFoundError:
                pass
            raise

        return new_data
    finally:
        os.close(lock_fd)
