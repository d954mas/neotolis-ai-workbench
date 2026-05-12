"""Task value objects: Task dataclass, Status (4 vals), TaskKind, FinishPolicy."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION: int = 1


class Status(StrEnum):
    # Controller writes only these four. Future statuses (interrupted,
    # waiting_for_user, cancelled) can arrive without a schema bump because
    # status is stored as a string field and reads tolerate unknown values.
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def from_str_lenient(cls, value: str) -> "Status | str":
        """Return Status enum if known, else the raw string. Never raises."""
        try:
            return cls(value)
        except ValueError:
            return value


class TaskKind(StrEnum):
    PROJECT = "project"
    GENERIC = "generic"


class FinishPolicy(StrEnum):
    ASK = "ask"
    KEEP_WORKTREE = "keep_worktree"
    DELETE_WORKTREE = "delete_worktree"


@dataclass(frozen=True)
class Task:
    # Required for every task
    id: str
    kind: TaskKind
    container_name: str
    image_tag: str
    created_at: str
    updated_at: str
    # Lifecycle writes these
    status: Status = Status.CREATED
    failure_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    image_digest: str | None = None
    finish_policy: FinishPolicy = FinishPolicy.ASK
    auto_finish: bool = False
    # Project-only fields (None for generic)
    project: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    base_branch: str | None = None
    base_commit: str | None = None
    # Resolved project repo path at start-time. Recorded so `finish` does not
    # need to re-load projects.yaml — that decouples teardown from config drift
    # between start and finish (rename alias, edit path, remove entry).
    project_repo_path: str | None = None
    # Defaults for fields later subsystems will write
    labels: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    events_offset: int = 0
    recovery_count: int = 0
    recovery_history: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Materialise for JSON serialisation. StrEnum values become their str."""
        d = asdict(self)
        # asdict already preserves StrEnum string value, but be explicit so json.dumps
        # always sees a plain str even if the dataclass machinery changes.
        d["kind"] = str(self.kind)
        d["status"] = str(self.status)
        d["finish_policy"] = str(self.finish_policy)
        return d
