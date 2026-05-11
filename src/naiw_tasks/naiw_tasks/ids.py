"""Task ID format + per-project monotonic counter under flock.

Counter is monotonic — clean does not reset it. <project>-001 does not come
back after the original task is deleted; log entries, container names, and
external references stay unambiguous over time.
"""

import fcntl
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

# Container-friendly DNS label shape — usable as both task id and the container
# name suffix (`naiw-task-<id>`) without further escaping.
TASK_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Project alias is the same shape but caps at 60 chars so `<project>-NNN` (id)
# always fits the 64-char container-name budget after the `naiw-task-` prefix.
PROJECT_ALIAS_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")


def validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.match(task_id):
        raise ValueError(
            f"invalid task id: {task_id!r} (must match {TASK_ID_RE.pattern})"
        )


def validate_project_alias(alias: str) -> None:
    """Reject project aliases the controller cannot turn into a valid task id.

    Called at the CLI boundary so `naiw-tasks start "Bad Name!"` produces a
    clean error instead of a raw ValueError from allocate_task_id mid-flow.
    """
    if not alias or not PROJECT_ALIAS_RE.match(alias):
        raise ValueError(
            f"invalid project alias: {alias!r} (must be DNS-label shape, "
            f"a-z/0-9/-, length 1-60, start with alphanumeric)"
        )


def _atomic_write_counter(counter_path: Path, value: int) -> None:
    counters_dir = counter_path.parent
    project_stem = counter_path.stem
    # `delete=False` + explicit close + os.replace is the atomic-write recipe.
    # A `with NamedTemporaryFile(...)` context manager would auto-close (and on
    # some platforms also delete) at scope exit, racing with the os.replace
    # publication step. Manual lifecycle here is intentional and correct.
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        dir=str(counters_dir),
        delete=False,
        encoding="utf-8",
        prefix=f".{project_stem}.",
        suffix=".tmp",
    )
    try:
        tmp.write(str(value))
        tmp.flush()
        # Without fsync, os.replace can publish a 0-byte counter file on power loss.
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, str(counter_path))
    except Exception:
        tmp.close()
        with suppress(FileNotFoundError):
            os.unlink(tmp.name)
        raise


def allocate_task_id(data_root: Path, project: str) -> str:
    """Allocate next <project>-NNN under per-project flock. Monotonic.

    Generic tasks pass project='task' so ids look like task-001.
    Steps:
      1. Read .counters/<project>.txt (missing -> 0).
      2. Candidate = counter + 1.
      3. If tasks/<project>-NNN matching this or higher exists, bump candidate.
      4. Atomically publish new counter, return id.
    """
    # Cheap shape check — full id check happens after candidate composition.
    validate_project_alias(project)

    locks_dir = data_root / ".locks"
    counters_dir = data_root / ".counters"
    tasks_dir = data_root / "tasks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    counters_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir.mkdir(parents=True, exist_ok=True)

    lock_path = locks_dir / f"{project}.lock"
    counter_path = counters_dir / f"{project}.txt"

    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            counter = int(counter_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            counter = 0

        candidate = counter + 1
        existing_ids: list[int] = []
        for p in tasks_dir.glob(f"{project}-*"):
            suffix = p.name.rsplit("-", 1)[-1]
            if suffix.isdigit():
                existing_ids.append(int(suffix))
        if existing_ids:
            candidate = max(candidate, max(existing_ids) + 1)

        new_id = f"{project}-{candidate:03d}"
        validate_task_id(new_id)

        _atomic_write_counter(counter_path, candidate)
        return new_id
    finally:
        os.close(lock_fd)
