"""Tests for naiw_signal.writer (atomic append behaviour)."""

import json
import os

import pytest
from naiw_common.events import Event

from naiw_signal import writer as writer_mod


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Redirect EVENTS_PATH to a tmpdir for the duration of the test.

    We patch writer_mod.EVENTS_PATH (not naiw_common.paths.EVENTS_PATH) because
    the writer did `from naiw_common.paths import EVENTS_PATH` at import time,
    which copied the binding into writer_mod's namespace.
    """
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(writer_mod, "EVENTS_PATH", str(path))
    return path


def test_three_sequential_writes_produce_three_lines(journal):
    writer_mod.append_event(Event.done().as_dict())
    writer_mod.append_event(Event.fail(reason="x").as_dict())
    writer_mod.append_event(Event.wait(reason="y").as_dict())

    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        d = json.loads(line)
        assert {"ts", "kind", "payload", "schema_version"}.issubset(d.keys())


def test_writer_uses_os_open_with_correct_flags(journal, monkeypatch):
    captured: dict = {}
    original_open = os.open

    def spy_open(path, flags, mode=0o777, *a, **kw):
        captured.setdefault("calls", []).append({"path": path, "flags": flags, "mode": mode})
        return original_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    writer_mod.append_event(Event.done().as_dict())

    assert captured["calls"], "os.open was never called"
    call = captured["calls"][0]
    assert call["flags"] & os.O_APPEND, "missing O_APPEND"
    assert call["flags"] & os.O_WRONLY, "missing O_WRONLY"
    assert call["flags"] & os.O_CREAT, "missing O_CREAT"
    assert call["mode"] == 0o644, f"mode is {call['mode']:o}, expected 0o644"


def test_fsync_called_before_close(journal, monkeypatch):
    order: list[str] = []
    original_fsync = os.fsync
    original_close = os.close

    def spy_fsync(fd):
        order.append("fsync")
        return original_fsync(fd)

    def spy_close(fd):
        order.append("close")
        return original_close(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "close", spy_close)
    writer_mod.append_event(Event.done().as_dict())

    assert order == ["fsync", "close"], f"call order was {order}, expected [fsync, close]"


def test_single_write_per_event(journal, monkeypatch):
    writes: list[bytes] = []
    original_write = os.write

    def spy_write(fd, buf):
        writes.append(buf)
        return original_write(fd, buf)

    monkeypatch.setattr(os, "write", spy_write)
    writer_mod.append_event(Event.done(summary="x").as_dict())

    # Exactly one os.write call for this fd; the writer MUST NOT split into multiple syscalls.
    assert len(writes) == 1, f"expected 1 write, got {len(writes)}"


def test_oversized_event_exits_nonzero(journal, capsys):
    big_payload = {
        "ts": "2026-05-10T00:00:00.000Z",
        "kind": "done",
        "schema_version": 1,
        "payload": {"summary": "z" * 5000},
    }
    with pytest.raises(SystemExit) as exc:
        writer_mod.append_event(big_payload)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "naiw-signal:" in captured.err
    assert "too large" in captured.err


def test_io_error_during_write_exits_nonzero(journal, monkeypatch, capsys):
    def boom_write(fd, buf):
        raise OSError("disk full simulation")

    monkeypatch.setattr(os, "write", boom_write)
    with pytest.raises(SystemExit) as exc:
        writer_mod.append_event(Event.done().as_dict())
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "naiw-signal:" in captured.err
