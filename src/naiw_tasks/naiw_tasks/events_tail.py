"""Pure byte-mode tail of a JSONL event journal.

No I/O side effects beyond reading the file. The caller decides what to do
with the malformed list (typically: append to meta/events-error.log outside
the per-task flock). The caller is also responsible for persisting the
returned new_offset (via store.update_task on task.json).

Partial-line policy: a trailing fragment with no \\n is NOT parsed and NOT
counted as malformed. new_offset stays at the byte just past the last full
line. The next call re-reads from that offset and naturally picks up the
completed line once the producer flushes its \\n. This relies on POSIX
O_APPEND atomicity for writes <= PIPE_BUF (4096) — naiw_signal's writer
guarantees that.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from naiw_common.events import SCHEMA_VERSION, Event

_VALID_KINDS: frozenset[str] = frozenset({"done", "fail", "wait"})


@dataclass(frozen=True)
class Malformed:
    """One rejected line. `raw_line` has its trailing \\n stripped (if any)."""

    raw_line: str
    reason: str


def tail_events(
    events_path: Path,
    offset: int,
) -> tuple[int, list[Event], list[Malformed]]:
    """Return (new_offset, valid_events, malformed_lines) from `offset` to EOF.

    Returns (offset, [], []) when the file does not exist or is empty.
    Resets offset to 0 when stored offset exceeds current size (file shrank
    or was recreated — the only safe response is a full re-parse).
    """
    try:
        size = events_path.stat().st_size
    except FileNotFoundError:
        return (offset, [], [])

    if offset > size:
        # File shrank or was recreated. Reset and re-parse from byte 0 — the
        # alternative (returning empty) would silently drop events the producer
        # has already written.
        offset = 0

    if offset == size:
        return (offset, [], [])

    with open(events_path, "rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)

    # Split on \n. If chunk ends with \n, the last element is b"" (consumed).
    # Otherwise, the last element is a partial fragment that we must NOT
    # parse and MUST leave for the next call to pick up.
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

        kind = obj.get("kind")
        if kind not in _VALID_KINDS:
            malformed.append(Malformed(raw_line=text, reason=f"bad kind: {kind!r}"))
            continue

        sv = obj.get("schema_version")
        if sv != SCHEMA_VERSION:
            malformed.append(
                Malformed(raw_line=text, reason=f"bad schema_version: {sv!r}")
            )
            continue

        valid.append(
            Event(
                ts=obj.get("ts", ""),
                kind=kind,
                payload=obj.get("payload", {}),
                schema_version=sv,
            )
        )

    return (new_offset, valid, malformed)
