"""Tests for naiw_common.events."""

import json
import re

import pytest
from naiw_common.events import SCHEMA_VERSION, Event

PIPE_BUF_LIMIT = 4096
ISO_MS_Z_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_schema_version_constant():
    assert SCHEMA_VERSION == 1
    assert isinstance(SCHEMA_VERSION, int)


def test_now_iso_format():
    ts = Event.now_iso()
    assert ISO_MS_Z_PATTERN.match(ts), (
        f"timestamp {ts!r} is not ISO-8601 UTC with ms precision and Z suffix"
    )


def test_done_without_summary_omits_field():
    ev = Event.done()
    d = ev.as_dict()
    assert d["kind"] == "done"
    assert d["schema_version"] == 1
    assert d["payload"] == {}, "summary field MUST be omitted when not provided"


def test_done_with_summary_sets_field():
    ev = Event.done(summary="completed alpha")
    d = ev.as_dict()
    assert d["kind"] == "done"
    assert d["payload"] == {"summary": "completed alpha"}


def test_fail_requires_reason():
    ev = Event.fail(reason="syntax error")
    d = ev.as_dict()
    assert d["kind"] == "fail"
    assert d["payload"] == {"reason": "syntax error"}

    with pytest.raises(ValueError):
        Event.fail(reason="")


def test_wait_requires_reason():
    ev = Event.wait(reason="awaiting input")
    d = ev.as_dict()
    assert d["kind"] == "wait"
    assert d["payload"] == {"reason": "awaiting input"}

    with pytest.raises(ValueError):
        Event.wait(reason="")


def test_round_trip_through_json():
    ev = Event.done(summary="round trip")
    encoded = json.dumps(ev.as_dict(), separators=(",", ":"), ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded == ev.as_dict()


def test_line_under_pipe_buf_for_typical_events():
    # Single-line size must stay within the writer's atomic-append budget.
    for ev in [
        Event.done(),
        Event.done(summary="x" * 256),
        Event.fail(reason="y" * 256),
        Event.wait(reason="z" * 256),
    ]:
        line = json.dumps(ev.as_dict(), separators=(",", ":"), ensure_ascii=False) + "\n"
        encoded_len = len(line.encode("utf-8"))
        assert encoded_len < PIPE_BUF_LIMIT, f"line too large for atomic append: {encoded_len}"


def test_kind_discriminator_round_trips():
    for kind, ev in [
        ("done", Event.done()),
        ("fail", Event.fail(reason="x")),
        ("wait", Event.wait(reason="y")),
    ]:
        d = json.loads(json.dumps(ev.as_dict()))
        assert d["kind"] == kind
        assert d["schema_version"] == 1
        assert "ts" in d and "payload" in d
