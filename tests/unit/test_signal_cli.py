"""Tests for naiw_signal.cli."""

import json

import pytest
from click.testing import CliRunner
from naiw_signal.cli import cli

from naiw_signal import writer as writer_mod


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(writer_mod, "EVENTS_PATH", str(path))
    return path


@pytest.fixture
def runner():
    return CliRunner()


def _read_events(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_done_no_summary(runner, journal):
    result = runner.invoke(cli, ["done"])
    assert result.exit_code == 0, result.output
    events = _read_events(journal)
    assert len(events) == 1
    assert events[0]["kind"] == "done"
    assert events[0]["payload"] == {}


def test_done_with_summary(runner, journal):
    result = runner.invoke(cli, ["done", "--summary", "hi"])
    assert result.exit_code == 0
    events = _read_events(journal)
    assert len(events) == 1
    assert events[0]["payload"] == {"summary": "hi"}


def test_fail_requires_reason(runner, journal):
    result = runner.invoke(cli, ["fail"])
    assert result.exit_code == 2  # click default for missing required option
    # No event written.
    assert not journal.exists() or journal.read_text() == ""


def test_fail_with_reason(runner, journal):
    result = runner.invoke(cli, ["fail", "--reason", "boom"])
    assert result.exit_code == 0
    events = _read_events(journal)
    assert len(events) == 1
    assert events[0]["kind"] == "fail"
    assert events[0]["payload"] == {"reason": "boom"}


def test_wait_with_reason(runner, journal):
    result = runner.invoke(cli, ["wait", "--reason", "user"])
    assert result.exit_code == 0
    events = _read_events(journal)
    assert len(events) == 1
    assert events[0]["kind"] == "wait"
    assert events[0]["payload"] == {"reason": "user"}


def test_help_lists_subcommands(runner):
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for sub in ("done", "fail", "wait", "log"):
        assert sub in result.output


def test_unknown_subcommand_fails(runner):
    result = runner.invoke(cli, ["nope"])
    assert result.exit_code != 0


def test_log_subcommand_positional_message(runner, journal):
    result = runner.invoke(cli, ["log", "hello"])
    assert result.exit_code == 0, result.output
    events = _read_events(journal)
    assert len(events) == 1
    assert events[0]["kind"] == "log"
    assert events[0]["payload"] == {"message": "hello"}
    assert events[0]["schema_version"] == 1


def test_log_subcommand_missing_argument(runner, journal):
    result = runner.invoke(cli, ["log"])
    assert result.exit_code != 0
    assert not journal.exists() or journal.read_text() == ""


def test_log_subcommand_empty_string_arg(runner, journal):
    result = runner.invoke(cli, ["log", ""])
    assert result.exit_code != 0
    assert not journal.exists() or journal.read_text() == ""
