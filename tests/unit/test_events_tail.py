"""Unit tests for naiw_tasks.events_tail — byte-mode JSONL reader.

The reader's contract is intentionally narrow: read bytes from `offset` to
current EOF, parse only complete lines (ending in \\n), classify malformed lines
without raising, and return (new_offset, valid, malformed). The trailing
partial-line policy (D-08) is the load-bearing detail — tests pin it so a
future refactor cannot silently advance past a half-written event.
"""

import json
import os
import re
from pathlib import Path

from naiw_tasks.events_tail import Malformed, tail_events

# ---------- helpers ----------------------------------------------------------


def _valid_event_bytes(kind: str = "done", ts: str = "2026-05-16T10:00:00.000Z") -> bytes:
    """Build one newline-terminated event line that the reader will accept."""
    if kind in ("fail", "wait"):
        payload: dict = {"reason": "x"}
    elif kind == "log":
        payload = {"message": "x"}
    else:
        payload = {}
    obj = {"ts": ts, "kind": kind, "payload": payload, "schema_version": 1}
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


# ---------- empty / file-not-found ------------------------------------------


def test_tail_events_returns_empty_when_file_missing(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    # File does NOT exist — must not raise.
    result = tail_events(events_path, offset=0)
    assert result == (0, [], [])


def test_tail_events_returns_empty_when_file_empty(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(b"")
    result = tail_events(events_path, offset=0)
    assert result == (0, [], [])


def test_tail_events_returns_empty_when_offset_equals_size(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    line = _valid_event_bytes()
    events_path.write_bytes(line)
    n = len(line)
    result = tail_events(events_path, offset=n)
    assert result == (n, [], [])


# ---------- valid event parsing ---------------------------------------------


def test_tail_events_parses_single_valid_event(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    line = _valid_event_bytes(kind="done", ts="2026-05-16T10:00:00.000Z")
    events_path.write_bytes(line)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(line)
    assert len(events) == 1
    assert events[0].kind == "done"
    assert events[0].ts == "2026-05-16T10:00:00.000Z"
    assert events[0].schema_version == 1
    assert malformed == []


def test_tail_events_parses_three_events_in_one_read(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    line1 = _valid_event_bytes(kind="done")
    line2 = _valid_event_bytes(kind="fail")
    line3 = _valid_event_bytes(kind="wait")
    events_path.write_bytes(line1 + line2 + line3)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(line1) + len(line2) + len(line3)
    assert [e.kind for e in events] == ["done", "fail", "wait"]
    assert malformed == []


# ---------- partial-line policy (D-08) --------------------------------------


def test_tail_events_does_not_parse_partial_trailing_line(tmp_path: Path):
    # One full line + one partial fragment (no trailing newline). The reader
    # must consume only the full line; the partial fragment is left for the
    # next call to pick up once the producer flushes its \n.
    events_path = tmp_path / "events.jsonl"
    full_line = _valid_event_bytes(kind="done")
    partial = b'{"ts": "2026-05-16T'
    events_path.write_bytes(full_line + partial)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(full_line)
    assert len(events) == 1
    assert events[0].kind == "done"
    # The partial fragment is NOT counted as malformed — that would burn the
    # bytes (offset would advance past the missing tail, losing the event).
    assert malformed == []


def test_tail_events_partial_line_is_recovered_on_next_call(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    full_line = _valid_event_bytes(kind="done")
    partial = b'{"ts":"2026-05-16T10:00:01.000Z","kind":"fail",'
    events_path.write_bytes(full_line + partial)

    # First call: stops at the boundary of the partial fragment.
    new_offset, events, malformed = tail_events(events_path, offset=0)
    assert new_offset == len(full_line)
    assert len(events) == 1
    assert malformed == []

    # Producer finishes the line, appending the rest + \n.
    completion = b'"payload":{"reason":"x"},"schema_version":1}\n'
    # Append by writing the full byte stream (test harness rewrites the file).
    events_path.write_bytes(full_line + partial + completion)

    completed_line_len = len(partial) + len(completion)
    new_offset2, events2, malformed2 = tail_events(events_path, offset=new_offset)
    assert new_offset2 == new_offset + completed_line_len
    assert len(events2) == 1
    assert events2[0].kind == "fail"
    assert malformed2 == []


# ---------- offset > size (file shrank or was recreated) --------------------


def test_tail_events_resets_offset_when_offset_exceeds_size(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    line = _valid_event_bytes(kind="done")
    events_path.write_bytes(line)
    n = len(line)

    # Caller's stored offset is far beyond the current file size. The reader
    # treats this as "file shrank or was recreated" and re-parses from byte 0.
    new_offset, events, malformed = tail_events(events_path, offset=n + 1000)

    assert new_offset == n
    assert len(events) == 1
    assert events[0].kind == "done"


# ---------- malformed line classification -----------------------------------


def test_tail_events_rejects_invalid_json(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(b"{not valid json\n")

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(b"{not valid json\n")
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason.startswith("json:")
    # `raw_line` has the trailing newline stripped.
    assert malformed[0].raw_line == "{not valid json"


def test_tail_events_rejects_non_object_json(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(b"42\n")

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == 3
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason == "not an object"


def test_tail_events_rejects_bad_kind(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    bad = b'{"ts":"2026-05-16T10:00:00.000Z","kind":"celebrate","payload":{},"schema_version":1}\n'
    events_path.write_bytes(bad)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(bad)
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason.startswith("bad kind:")


def test_tail_events_rejects_bad_schema_version(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    bad = b'{"ts":"2026-05-16T10:00:00.000Z","kind":"done","payload":{},"schema_version":99}\n'
    events_path.write_bytes(bad)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(bad)
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason.startswith("bad schema_version:")


def test_tail_events_rejects_missing_or_bad_ts(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    bad = b'{"kind":"done","payload":{},"schema_version":1}\n'
    events_path.write_bytes(bad)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(bad)
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason.startswith("bad ts:")


def test_tail_events_rejects_non_object_payload(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    bad = b'{"ts":"2026-05-16T10:00:00.000Z","kind":"done","payload":[],"schema_version":1}\n'
    events_path.write_bytes(bad)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(bad)
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason == "bad payload: not an object"


def test_tail_events_rejects_fail_or_wait_without_reason(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    bad = b'{"ts":"2026-05-16T10:00:00.000Z","kind":"fail","payload":{},"schema_version":1}\n'
    events_path.write_bytes(bad)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(bad)
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason.startswith("bad reason:")


def test_tail_events_rejects_invalid_utf8(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    # Bytes that cannot decode as UTF-8. The reader must report the failure
    # via Malformed rather than raise UnicodeDecodeError.
    events_path.write_bytes(b"\xff\xfe not utf-8\n")

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(b"\xff\xfe not utf-8\n")
    assert events == []
    assert len(malformed) == 1
    assert malformed[0].reason.startswith("utf-8:")


# ---------- mixed valid + malformed -----------------------------------------


def test_tail_events_processes_valid_after_malformed(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    line1 = _valid_event_bytes(kind="done")
    line2 = b"{garbage\n"
    line3 = _valid_event_bytes(kind="fail")
    events_path.write_bytes(line1 + line2 + line3)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(line1) + len(line2) + len(line3)
    assert [e.kind for e in events] == ["done", "fail"]
    assert len(malformed) == 1
    assert malformed[0].reason.startswith("json:")


# ---------- idempotent reapply (SIG-05) -------------------------------------


def test_tail_events_idempotent_reapply(tmp_path: Path):
    # Crash scenario: caller read events but crashed before persisting the
    # new offset. On restart, calling again with the old offset must produce
    # byte-identical results — same offset, same events, same malformed.
    events_path = tmp_path / "events.jsonl"
    line = _valid_event_bytes(kind="done")
    events_path.write_bytes(line)

    first = tail_events(events_path, offset=0)
    second = tail_events(events_path, offset=0)

    assert first[0] == second[0]
    assert first[1] == second[1]
    assert first[2] == second[2]


# ---------- purity / no side effects ----------------------------------------


def test_tail_events_does_not_modify_file(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    line1 = _valid_event_bytes(kind="done")
    line2 = b"{garbage\n"
    contents = line1 + line2
    events_path.write_bytes(contents)
    before = events_path.read_bytes()

    tail_events(events_path, offset=0)

    after = events_path.read_bytes()
    assert before == after


def test_tail_events_does_not_write_events_error_log(tmp_path: Path):
    # The reader returns Malformed objects; the CALLER decides where to write
    # the error log (host-only meta/events-error.log). The reader itself must
    # NOT create that file.
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(b"{garbage\n")

    tail_events(events_path, offset=0)

    # No file other than the input should exist.
    sibling = tmp_path / "events-error.log"
    assert not sibling.exists()
    parent_meta = tmp_path / "meta"
    assert not parent_meta.exists()


def test_tail_events_refuses_symlink_events_journal(tmp_path: Path):
    """Pi could symlink events.jsonl to a host file; lstat + S_ISREG + O_NOFOLLOW
    must refuse to read it, surfacing a single Malformed line and leaving
    offset unchanged so no host bytes ever reach events-error.log."""
    target = tmp_path / "host-secret"
    target.write_bytes(b'{"ts":"2026-05-16T10:00:00.000Z","kind":"done","payload":{},"schema_version":1}\n')
    events_path = tmp_path / "events.jsonl"
    try:
        os.symlink(target, events_path)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this filesystem")

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == 0
    assert events == []
    assert len(malformed) == 1
    assert "refused" in malformed[0].reason
    assert "host-secret" not in malformed[0].raw_line
    assert "done" not in malformed[0].raw_line


# ---------- module hygiene --------------------------------------------------


_EVENTS_TAIL_SRC = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "naiw_tasks"
    / "naiw_tasks"
    / "events_tail.py"
).read_text(encoding="utf-8")


def test_events_tail_module_has_no_gsd_refs():
    bad = re.search(
        r"\bD-[0-9]+|\bPhase [0-9]+|\bPlan [0-9]+|\bRESEARCH\b|"
        r"\bCTRL-[0-9]+|\bHARD-[0-9]+|\bDATA-[0-9]+|\bGIT-[0-9]+|"
        r"\bLIST-[0-9]+|\bSIG-[0-9]+|\bPROJ-[0-9]+|\bPROXY-[0-9]+|"
        r"\bIMG-[0-9]+",
        _EVENTS_TAIL_SRC,
    )
    assert bad is None, (
        f"forbidden token in events_tail.py: {bad.group(0) if bad else None}"
    )


def test_events_tail_returns_malformed_dataclass():
    # Sanity: the public Malformed type is a frozen dataclass.
    m = Malformed(raw_line="x", reason="y")
    assert m.raw_line == "x"
    assert m.reason == "y"


# ---------- log kind acceptance (regression guard for the runtime whitelist) -


def test_log_kind_accepted(tmp_path: Path):
    """The runtime whitelist MUST accept 'log' — otherwise every Pi-emitted log
    event would be classified malformed and silently routed to events-error.log.
    """
    events_path = tmp_path / "events.jsonl"
    line = (
        b'{"ts":"2026-05-19T10:00:00.000Z","kind":"log",'
        b'"payload":{"message":"hello"},"schema_version":1}\n'
    )
    events_path.write_bytes(line)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(line)
    assert len(events) == 1
    assert events[0].kind == "log"
    assert events[0].payload == {"message": "hello"}
    assert malformed == []


def test_log_kind_missing_message_classified_malformed(tmp_path: Path):
    events_path = tmp_path / "events.jsonl"
    bad = (
        b'{"ts":"2026-05-19T10:00:00.000Z","kind":"log",'
        b'"payload":{},"schema_version":1}\n'
    )
    events_path.write_bytes(bad)

    new_offset, events, malformed = tail_events(events_path, offset=0)

    assert new_offset == len(bad)
    assert events == []
    assert len(malformed) == 1
    assert "message" in malformed[0].reason


def test_log_kind_does_not_routed_to_events_error(tmp_path: Path):
    """Regression guard: a well-formed log event MUST NOT be classified
    malformed. Without the runtime-whitelist expansion, this test fails — the
    line would be routed to events-error.log on the controller side.
    """
    events_path = tmp_path / "events.jsonl"
    line = (
        b'{"ts":"2026-05-19T10:00:00.000Z","kind":"log",'
        b'"payload":{"message":"streamed status update"},"schema_version":1}\n'
    )
    events_path.write_bytes(line)

    _new_offset, events, malformed = tail_events(events_path, offset=0)

    assert malformed == [], (
        "log events must NOT be routed to events-error.log; "
        "the events_tail runtime whitelist must include 'log'"
    )
    assert len(events) == 1 and events[0].kind == "log"
