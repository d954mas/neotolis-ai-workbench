"""Tests for naiw_tasks.docker_client — DockerClient constructor + hardening kwargs."""

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_make_client_constructs_with_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import naiw_tasks.docker_client as docker_client_mod

    mock_client_cls = MagicMock()
    monkeypatch.setattr(docker_client_mod.docker, "DockerClient", mock_client_cls)

    docker_client_mod.make_client("tcp://example:2375")

    assert mock_client_cls.call_count == 1
    kwargs = mock_client_cls.call_args.kwargs
    assert kwargs["base_url"] == "tcp://example:2375"
    # Timeout must be set (non-default) so a hung proxy never blocks the controller
    # forever; finite value is what matters, not the exact number.
    assert "timeout" in kwargs
    assert isinstance(kwargs["timeout"], int | float)
    assert kwargs["timeout"] > 0
    assert kwargs["timeout"] < 120  # SDK default is much higher; we cap small.


def test_hardened_kwargs_match_phase25() -> None:
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    assert K["cap_drop"] == ["ALL"]
    assert K["security_opt"] == ["no-new-privileges"]
    assert K["read_only"] is True
    assert K["tmpfs"] == {
        "/tmp": "rw,size=512m,mode=1777",
        "/run": "rw,size=64m,mode=755",
        "/home/pi": "rw,size=128m,mode=1777",
    }
    assert K["pids_limit"] == 512
    assert K["mem_limit"] == "4g"
    assert K["memswap_limit"] == "4g"
    assert K["nano_cpus"] == 2_000_000_000
    assert K["network"] == "naiw-task-net"
    assert K["restart_policy"] == {"Name": "no"}
    assert K["init"] is True
    assert K["tty"] is True
    assert K["stdin_open"] is True


def test_hardened_kwargs_disallow_ambiguous_and_escalation_keys() -> None:
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    # Ambiguity guard — network_mode and network both define networking; only
    # one wins in docker-py and the conflict is silent. Forbid network_mode here.
    assert "network_mode" not in K
    # Privileged escalation guard.
    assert K.get("privileged", False) is False
    assert K.get("cap_add", []) == [] or "cap_add" not in K


def test_hardened_kwargs_keyset_is_exactly_documented() -> None:
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    expected_keys = {
        "cap_drop",
        "security_opt",
        "read_only",
        "tmpfs",
        "pids_limit",
        "mem_limit",
        "memswap_limit",
        "nano_cpus",
        "network",
        "restart_policy",
        "init",
        "tty",
        "stdin_open",
    }
    assert set(K.keys()) == expected_keys, (
        f"key drift: unexpected={set(K.keys()) - expected_keys}, "
        f"missing={expected_keys - set(K.keys())}"
    )


def test_hardened_kwargs_stable_across_reimport() -> None:
    # The constant should hold the same content across reloads — guards against
    # accidental mutation by a consumer at module import time.
    import naiw_tasks.docker_client as docker_client_mod

    first = dict(docker_client_mod.HARDENED_HOST_CONFIG_KWARGS)
    first_tmpfs = dict(docker_client_mod.HARDENED_HOST_CONFIG_KWARGS["tmpfs"])

    importlib.reload(docker_client_mod)

    assert dict(docker_client_mod.HARDENED_HOST_CONFIG_KWARGS) == first
    assert dict(docker_client_mod.HARDENED_HOST_CONFIG_KWARGS["tmpfs"]) == first_tmpfs


def test_no_docker_from_env_in_module() -> None:
    # docker.from_env() bypasses the proxy and grants direct host-socket access —
    # forbidden by the trust model. Static check on the source text.
    source_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "docker_client.py"
    )
    source = source_path.read_text(encoding="utf-8")
    assert "from_env" not in source, "docker.from_env() is forbidden — must go through proxy"
