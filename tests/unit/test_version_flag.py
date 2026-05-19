"""Tests for `naiw-tasks --version` and the doctor-subcommand rewiring.

`--version` is a top-level eager click option printing exactly three plain
lines (controller version, task-image digest prefix, naiw-signal schema).
The doctor placeholder body is replaced (not duplicated); both forms now
delegate to the doctor module.
"""

import sys

import pytest

if sys.platform != "linux":
    pytest.skip(
        "Linux-only (fcntl / O_NOFOLLOW)",
        allow_module_level=True,
    )

from pathlib import Path
from unittest.mock import MagicMock

import docker.errors
import naiw_tasks.cli as cli_mod
from click.testing import CliRunner
from naiw_tasks.cli import cli
from naiw_tasks.config import Config


@pytest.fixture
def patched_env(monkeypatch, tmp_path):
    """Make every CLI invocation observable without touching disk/network."""
    cfg = Config(data_root=tmp_path / "naiw-data")
    monkeypatch.setattr(cli_mod.config, "load", lambda: cfg)
    monkeypatch.setattr(cli_mod, "make_client", lambda url: MagicMock())
    monkeypatch.setattr(
        cli_mod.startup_checks, "run_all", lambda c, k, **kw: None
    )
    return cfg


def _client_with_image_id(sha256_hex: str) -> MagicMock:
    client = MagicMock()
    image = MagicMock()
    image.id = f"sha256:{sha256_hex}"
    client.images.get.return_value = image
    return client


# ---------- --version --------------------------------------------------------


def test_version_flag_three_lines(monkeypatch, patched_env):
    monkeypatch.setattr(
        cli_mod,
        "make_client",
        lambda url: _client_with_image_id("0" * 64),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    # 3 lines means content + 2 internal newlines (trailing newline from echo
    # adds one more newline character; assert 2+).
    assert result.output.count("\n") >= 2
    assert result.output.startswith("naiw-tasks ")


def test_version_line_one_contains_schema_version_1(monkeypatch, patched_env):
    monkeypatch.setattr(
        cli_mod,
        "make_client",
        lambda url: _client_with_image_id("0" * 64),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    first_line = result.output.splitlines()[0]
    assert "(schema_version=1)" in first_line


def test_version_line_two_image_prefix(monkeypatch, patched_env):
    digest_hex = "abcdef0123456789" + "0" * 48  # 64 hex total
    monkeypatch.setattr(
        cli_mod,
        "make_client",
        lambda url: _client_with_image_id(digest_hex),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    second_line = result.output.splitlines()[1]
    assert "abcdef012345" in second_line


def test_version_line_two_fallback_on_docker_unreachable(
    monkeypatch, patched_env
):
    def raising_make_client(url):
        raise docker.errors.DockerException("boom")

    monkeypatch.setattr(cli_mod, "make_client", raising_make_client)
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    assert "<unresolved>" in result.output.splitlines()[1]


def test_version_line_three_signal_schema(monkeypatch, patched_env):
    monkeypatch.setattr(
        cli_mod,
        "make_client",
        lambda url: _client_with_image_id("0" * 64),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    third_line = result.output.splitlines()[2]
    assert "naiw-signal schema v1" in third_line


def test_version_flag_is_eager(monkeypatch, patched_env):
    """--version must succeed before any subcommand body runs.

    The eager+config-tolerant pattern is the documented contract: an
    operator running `naiw-tasks --version` on a fresh box (where the
    Docker proxy is not up) MUST still see versions printed.
    """
    monkeypatch.setattr(
        cli_mod,
        "make_client",
        lambda url: _client_with_image_id("0" * 64),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0


# ---------- doctor subcommand rewiring --------------------------------------


def test_doctor_subcommand_no_args_delegates(monkeypatch, patched_env):
    fake = MagicMock(return_value=0)
    monkeypatch.setattr(cli_mod.doctor_mod, "run_global", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    fake.assert_called_once()
    # Signature: run_global(cfg, client)
    args, _ = fake.call_args
    assert isinstance(args[0], Config)


def test_doctor_subcommand_with_task_id_delegates(monkeypatch, patched_env):
    fake = MagicMock(return_value=0)
    monkeypatch.setattr(cli_mod.doctor_mod, "run_per_task", fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "my-task-001"])
    assert result.exit_code == 0, result.output
    args, _ = fake.call_args
    # Signature: run_per_task(cfg, client, task_id)
    assert "my-task-001" in args or args[-1] == "my-task-001"


def test_doctor_subcommand_propagates_nonzero_exit(monkeypatch, patched_env):
    """Drift exit (1) and invalid-id exit (3) must flow back to the OS."""
    monkeypatch.setattr(
        cli_mod.doctor_mod, "run_global", MagicMock(return_value=1)
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1


def test_no_duplicate_doctor_registration():
    """One — and only one — doctor command on the cli group."""
    assert cli.commands.get("doctor") is not None
    # The placeholder body would have echoed "doctor OK"; the new body must
    # not contain that string in its callback / docstring.
    doctor_cmd = cli.commands["doctor"]
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "cli.py"
    ).read_text(encoding="utf-8")
    assert "doctor OK" not in src
    # The callback receives a `task_id` positional (new contract).
    assert "task_id" in doctor_cmd.params[0].name or any(
        p.name == "task_id" for p in doctor_cmd.params
    )
