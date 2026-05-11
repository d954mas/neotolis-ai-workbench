"""Tests for naiw_tasks.cli — click subcommand wiring + exit codes.

Patches `naiw_tasks.cli.config.load`, `naiw_tasks.cli.make_client`,
`naiw_tasks.cli.startup_checks.run_all`, and the lifecycle/attach helpers
so the CliRunner exercises wiring only — no real filesystem, no real Docker.
"""

from pathlib import Path
from unittest.mock import MagicMock

import naiw_tasks.cli as cli_mod
import pytest
from click.testing import CliRunner
from naiw_tasks.cli import cli
from naiw_tasks.config import Config
from naiw_tasks.startup_checks import StartupCheckFailed

# ---------- shared fixtures --------------------------------------------------


@pytest.fixture
def patched_env(monkeypatch, tmp_path):
    """Make every CLI invocation observable without touching disk/network."""
    cfg = Config(data_root=tmp_path / "naiw-data")
    monkeypatch.setattr(cli_mod.config, "load", lambda: cfg)
    monkeypatch.setattr(cli_mod, "make_client", lambda url: MagicMock())
    monkeypatch.setattr(cli_mod.startup_checks, "run_all", lambda c, k: None)
    return cfg


# ---------- wiring -----------------------------------------------------------


def test_cli_top_level_help_lists_three_subcommands():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "start" in result.output
    assert "attach" in result.output
    assert "finish" in result.output


def test_cli_runs_startup_checks_before_subcommand(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_run_all(cfg, client):
        calls.append("startup_checks")

    def fake_start(*a, **kw):
        calls.append("lifecycle.start")

    cfg = Config(data_root=tmp_path / "naiw-data")
    monkeypatch.setattr(cli_mod.config, "load", lambda: cfg)
    monkeypatch.setattr(cli_mod, "make_client", lambda url: MagicMock())
    monkeypatch.setattr(cli_mod.startup_checks, "run_all", fake_run_all)
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake_start)

    runner = CliRunner()
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 0, result.output
    assert calls == ["startup_checks", "lifecycle.start"]


def test_cli_propagates_startup_check_failure(monkeypatch, tmp_path):
    cfg = Config(data_root=tmp_path / "naiw-data")
    monkeypatch.setattr(cli_mod.config, "load", lambda: cfg)
    monkeypatch.setattr(cli_mod, "make_client", lambda url: MagicMock())

    def boom(cfg, client):
        raise StartupCheckFailed("nope")

    monkeypatch.setattr(cli_mod.startup_checks, "run_all", boom)
    runner = CliRunner()
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 2
    assert "naiw-tasks: nope" in result.stderr


def test_cli_config_load_value_error_exits_2_with_clean_message(monkeypatch):
    """Bad config.yaml must surface as exit 2 + naiw-tasks: prefix, NOT a raw
    Python traceback."""
    def bad_load():
        raise ValueError("naiw-tasks: config.yaml has unknown key ['weird']")

    monkeypatch.setattr(cli_mod.config, "load", bad_load)
    runner = CliRunner()
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 2
    assert "naiw-tasks: " in result.stderr
    assert "unknown key" in result.stderr


def test_start_invalid_project_alias_exits_3_with_clean_message(
    monkeypatch, patched_env
):
    """Bad project alias must surface as exit 3 + naiw-tasks: prefix, NOT a
    raw ValueError from allocate_task_id deep inside lifecycle.start."""
    fake_lifecycle_start = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake_lifecycle_start)
    runner = CliRunner()

    result = runner.invoke(cli, ["start", "Bad Project!"])

    assert result.exit_code == 3
    assert "naiw-tasks: " in result.stderr
    assert "invalid project alias" in result.stderr
    # lifecycle.start MUST NOT be called for an invalid alias
    fake_lifecycle_start.assert_not_called()


def test_start_with_uppercase_alias_rejected(monkeypatch, patched_env):
    """DNS-label shape is lowercase-only — uppercase is rejected at CLI boundary."""
    fake_lifecycle_start = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake_lifecycle_start)
    runner = CliRunner()

    result = runner.invoke(cli, ["start", "Alpha"])

    assert result.exit_code == 3
    fake_lifecycle_start.assert_not_called()


def test_start_with_too_long_alias_rejected(monkeypatch, patched_env):
    """Alias capped at 60 chars (DNS-label budget for naiw-task-<id>)."""
    fake_lifecycle_start = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake_lifecycle_start)
    runner = CliRunner()

    result = runner.invoke(cli, ["start", "a" * 61])

    assert result.exit_code == 3
    fake_lifecycle_start.assert_not_called()


# ---------- start subcommand -------------------------------------------------


def test_start_calls_lifecycle_with_project(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["start", "alpha"])
    assert result.exit_code == 0, result.output
    args, kwargs = fake.call_args
    assert kwargs["project"] == "alpha"
    assert kwargs["base_ref"] is None
    assert kwargs["finish_policy"] == "ask"
    assert kwargs["secrets"] == []


def test_start_no_arg_calls_lifecycle_with_project_none(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 0, result.output
    args, kwargs = fake.call_args
    assert kwargs["project"] is None


def test_start_passes_base_flag(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["start", "alpha", "--base", "feature/x"])
    assert result.exit_code == 0, result.output
    _, kwargs = fake.call_args
    assert kwargs["base_ref"] == "feature/x"


def test_start_passes_finish_policy(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["start", "alpha", "--finish-policy", "delete_worktree"]
    )
    assert result.exit_code == 0, result.output
    _, kwargs = fake.call_args
    assert kwargs["finish_policy"] == "delete_worktree"


def test_start_invalid_finish_policy_exits_2(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["start", "alpha", "--finish-policy", "foo"])
    assert result.exit_code == 2
    assert fake.call_count == 0


def test_start_passes_repeated_secrets(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "start", fake)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["start", "alpha", "--secret", "tok1", "--secret", "tok2"]
    )
    assert result.exit_code == 0, result.output
    _, kwargs = fake.call_args
    assert kwargs["secrets"] == ["tok1", "tok2"]


def test_start_runtime_failure_exits_1(monkeypatch, patched_env):
    def boom(*a, **kw):
        raise cli_mod.lifecycle.StartFailed("git worktree add failed")

    monkeypatch.setattr(cli_mod.lifecycle, "start", boom)
    runner = CliRunner()
    result = runner.invoke(cli, ["start", "alpha"])
    assert result.exit_code == 1


# ---------- attach subcommand ------------------------------------------------


def test_attach_validates_id_and_exits_3_on_invalid(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.attach_mod, "attach_to_task", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["attach", "Foo!"])
    assert result.exit_code == 3
    assert "invalid task id" in result.stderr
    assert fake.call_count == 0


def test_attach_calls_attach_to_task(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.attach_mod, "attach_to_task", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["attach", "foo-001"])
    assert result.exit_code == 0, result.output
    args, kwargs = fake.call_args
    # attach_to_task(client, proxy_url, task_id) — task_id positional third.
    # proxy_url comes from cfg.docker_proxy_url so docker CLI uses -H against
    # the same proxy as the SDK (NOT the host /var/run/docker.sock).
    assert args[2] == "foo-001" or kwargs.get("task_id") == "foo-001"
    assert args[0] is not patched_env, (
        "first positional must be the docker client, not cfg"
    )
    assert args[1] == patched_env.docker_proxy_url, (
        "second positional must be cfg.docker_proxy_url so docker CLI -H "
        "routes through the proxy"
    )


# ---------- finish subcommand ------------------------------------------------


def test_finish_validates_id_and_exits_3_on_invalid(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "finish", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["finish", "BAD"])
    assert result.exit_code == 3
    assert fake.call_count == 0


def test_finish_calls_lifecycle_finish(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "finish", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["finish", "foo-001"])
    assert result.exit_code == 0, result.output
    args, kwargs = fake.call_args
    # Accept positional or kw
    flat = list(args) + [kwargs.get("task_id")]
    assert "foo-001" in flat
    assert kwargs.get("policy_override") is None


def test_finish_keep_worktree_flag(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "finish", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["finish", "foo-001", "--keep-worktree"])
    assert result.exit_code == 0, result.output
    _, kwargs = fake.call_args
    assert kwargs["policy_override"] == "keep_worktree"


def test_finish_delete_worktree_flag(monkeypatch, patched_env):
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "finish", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["finish", "foo-001", "--delete-worktree"])
    assert result.exit_code == 0, result.output
    _, kwargs = fake.call_args
    assert kwargs["policy_override"] == "delete_worktree"


def test_finish_mutually_exclusive_flags(monkeypatch, patched_env):
    """Passing both flags: the second one wins (click flag_value semantics).

    Either behaviour (second-wins OR explicit error) is acceptable per the plan.
    We assert the chosen behaviour: second-wins, so call succeeds with the
    last-flag's value.
    """
    fake = MagicMock()
    monkeypatch.setattr(cli_mod.lifecycle, "finish", fake)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["finish", "foo-001", "--keep-worktree", "--delete-worktree"]
    )
    assert result.exit_code == 0, result.output
    _, kwargs = fake.call_args
    assert kwargs["policy_override"] == "delete_worktree"


# ---------- source-policy guard ---------------------------------------------


def test_cli_module_has_no_gsd_refs():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "cli.py"
    ).read_text(encoding="utf-8")
    import re

    bad = re.search(
        r"\bD-[0-9]+|\bPhase [0-9]+|\bPlan [0-9]+|\bRESEARCH\b|"
        r"\bCTRL-[0-9]+|\bHARD-[0-9]+|\bDATA-[0-9]+|\bGIT-[0-9]+|"
        r"\bPROJ-[0-9]+|\bPROXY-[0-9]+|\bIMG-[0-9]+|\bSIG-[0-9]+",
        src,
    )
    assert bad is None, f"forbidden token in cli.py: {bad.group(0) if bad else None}"
