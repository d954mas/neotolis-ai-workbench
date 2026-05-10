"""Event schema for /io/.naiw/events.jsonl."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

SCHEMA_VERSION: int = 1

Kind = Literal["done", "fail", "wait"]


@dataclass(frozen=True)
class Event:
    """One event line in /io/.naiw/events.jsonl."""

    ts: str
    kind: Kind
    payload: Mapping[str, Any]
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def now_iso() -> str:
        """ISO-8601 UTC with millisecond precision and Z suffix."""
        now = datetime.now(UTC)
        ms = now.microsecond // 1000
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"

    @classmethod
    def done(cls, summary: str | None = None) -> "Event":
        """`done` event. `summary` field is omitted when not provided."""
        payload: dict[str, Any] = {}
        if summary is not None:
            payload["summary"] = summary
        return cls(ts=cls.now_iso(), kind="done", payload=payload)

    @classmethod
    def fail(cls, reason: str) -> "Event":
        """`fail` event. `reason` is required and non-empty."""
        if not reason:
            raise ValueError("fail: reason is required")
        return cls(ts=cls.now_iso(), kind="fail", payload={"reason": reason})

    @classmethod
    def wait(cls, reason: str) -> "Event":
        """`wait` event. `reason` is required and non-empty."""
        if not reason:
            raise ValueError("wait: reason is required")
        return cls(ts=cls.now_iso(), kind="wait", payload={"reason": reason})

    def as_dict(self) -> dict[str, Any]:
        """Materialise for JSON serialisation."""
        return asdict(self)
