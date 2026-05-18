"""Pure byte-mode tail of a JSONL event journal.

The reader has no write side effects: callers persist new_offset and decide
where to log malformed lines. A trailing fragment without \\n is left unread
so the next call can pick it up after the producer completes the line.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from naiw_common.events import SCHEMA_VERSION, Event

_VALID_KINDS: frozenset[str] = frozenset({"done", "fail", "wait"})


@dataclass(frozen=True)
class Malformed:
    """One rejected line. `raw_line` has its trailing \\n stripped (if any)."""

    raw_line: str
    reason: str


def _is_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_event(obj: dict, raw_line: str) -> Event | Malformed:
    kind = obj.get("kind")
    if kind not in _VALID_KINDS:
        return Malformed(raw_line=raw_line, reason=f"bad kind: {kind!r}")

    sv = obj.get("schema_version")
    if sv != SCHEMA_VERSION:
        return Malformed(raw_line=raw_line, reason=f"bad schema_version: {sv!r}")

    ts = obj.get("ts")
    if not isinstance(ts, str) or not _is_iso_datetime(ts):
        return Malformed(raw_line=raw_line, reason=f"bad ts: {ts!r}")

    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return Malformed(raw_line=raw_line, reason="bad payload: not an object")

    if kind in ("fail", "wait"):
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            return Malformed(raw_line=raw_line, reason=f"bad reason: {reason!r}")

    return Event(ts=ts, kind=kind, payload=payload, schema_version=sv)


def tail_events(
    events_path: Path,
    offset: int,
) -> tuple[int, list[Event], list[Malformed]]:
    """Return (new_offset, valid_events, malformed_lines) from `offset` to EOF."""
    try:
        size = events_path.stat().st_size
    except FileNotFoundError:
        return (offset, [], [])

    if offset > size:
        # File shrank/recreated; re-parse so existing events are not dropped.
        offset = 0

    if offset == size:
        return (offset, [], [])

    with open(events_path, "rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)

    # Leave a trailing partial line for the next pass.
    lines = chunk.split(b"\n")
    if chunk.endswith(b"\n"):
        complete = lines[:-1]
        bytes_consumed = len(chunk)
    else:
        complete = lines[:-1]
        bytes_consumed = len(chunk) - len(lines[-1])

    new_offset = offset + bytes_consumed

    valid: list[Event] = []
    malformed: list[Malformed] = []

    for raw in complete:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            malformed.append(
                Malformed(
                    raw_line=raw.decode("utf-8", errors="replace"),
                    reason=f"utf-8: {exc.reason}",
                )
            )
            continue

        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            malformed.append(Malformed(raw_line=text, reason=f"json: {exc.msg}"))
            continue

        if not isinstance(obj, dict):
            malformed.append(Malformed(raw_line=text, reason="not an object"))
            continue

        event = _validate_event(obj, text)
        if isinstance(event, Malformed):
            malformed.append(event)
            continue
        valid.append(event)

    return (new_offset, valid, malformed)
