"""Event schema for /io/.naiw/events.jsonl (D-04, SIG-03)."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional

SCHEMA_VERSION: int = 1

Kind = Literal["done", "fail", "wait"]


@dataclass(frozen=True)
class Event:
    """One event line in /io/.naiw/events.jsonl. D-04 wire format."""

    ts: str
    kind: Kind
    payload: Mapping[str, Any]
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def now_iso() -> str:
        """ISO-8601 UTC with millisecond precision and Z suffix (D-04)."""
        now = datetime.now(timezone.utc)
        ms = now.microsecond // 1000
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"

    @classmethod
    def done(cls, summary: Optional[str] = None) -> "Event":
        """`done` event. D-04: omit `summary` field if not provided."""
        payload: dict[str, Any] = {}
        if summary is not None:
            payload["summary"] = summary
        return cls(ts=cls.now_iso(), kind="done", payload=payload)

    @classmethod
    def fail(cls, reason: str) -> "Event":
        """`fail` event. D-04: `reason` is required and non-empty."""
        if not reason:
            raise ValueError("fail: reason is required")
        return cls(ts=cls.now_iso(), kind="fail", payload={"reason": reason})

    @classmethod
    def wait(cls, reason: str) -> "Event":
        """`wait` event. D-04: `reason` is required and non-empty."""
        if not reason:
            raise ValueError("wait: reason is required")
        return cls(ts=cls.now_iso(), kind="wait", payload={"reason": reason})

    def as_dict(self) -> dict[str, Any]:
        """Materialise for JSON serialisation."""
        return asdict(self)
