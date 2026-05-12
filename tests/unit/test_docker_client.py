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


def test_make_client_pins_api_version_to_skip_negotiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docker-py with version=None (default) calls GET /version at construction
    time. The locked proxy has VERSION=0 → 403 → DockerException at startup.
    Pinning version=<str> skips the negotiation entirely; the value must be
    a non-empty string so APIClient takes the no-network branch."""
    import naiw_tasks.docker_client as docker_client_mod

    mock_client_cls = MagicMock()
    monkeypatch.setattr(docker_client_mod.docker, "DockerClient", mock_client_cls)

    docker_client_mod.make_client("tcp://example:2375")

    kwargs = mock_client_cls.call_args.kwargs
    # Must be present, not None, and a string — that combination is what
    # bypasses _retrieve_server_version() inside docker-py's APIClient init.
    assert "version" in kwargs, (
        "version kwarg missing — constructor will GET /version (blocked by proxy)"
    )
    assert kwargs["version"] is not None
    assert isinstance(kwargs["version"], str)
    assert kwargs["version"] != "auto"
    # The exact value should match other proxy-version references in the codebase.
    assert kwargs["version"] == docker_client_mod.PINNED_DOCKER_API_VERSION


def test_startup_checks_uses_pinned_api_version_constant() -> None:
    """startup_checks must IMPORT PINNED_DOCKER_API_VERSION (not hardcode the
    version literal) so a future bump of the constant cannot leave the
    proxy-allowlist probe URL drifting. The previous design hardcoded
    `/v1.43/exec/fakeid/start` and relied on a source-grep guard; the import
    makes drift impossible by construction."""
    startup_src = (
        Path(__file__).resolve().parent.parent.parent
        / "src" / "naiw_tasks" / "naiw_tasks" / "startup_checks.py"
    ).read_text(encoding="utf-8")
    assert "from naiw_tasks.docker_client import PINNED_DOCKER_API_VERSION" in startup_src, (
        "startup_checks must import PINNED_DOCKER_API_VERSION from docker_client"
    )
    # And the literal-hardcoded `/v<digits>.<digits>/` form must be absent
    # from the source — any future regression that hardcodes a version
    # number in a URL path here will trip this check.
    import re
    hardcoded = re.findall(r"/v\d+\.\d+/", startup_src)
    assert hardcoded == [], (
        f"startup_checks contains hardcoded API-version paths {hardcoded}; "
        f"use f'/v{{PINNED_DOCKER_API_VERSION}}/' instead"
    )


def test_hardened_kwargs_match_phase25() -> None:
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    # cap_drop / security_opt are tuples (not lists) so they cannot be mutated
    # via .append() / .pop() / item-assignment.
    assert K["cap_drop"] == ("ALL",)
    assert K["security_opt"] == ("no-new-privileges",)
    assert K["read_only"] is True
    # tmpfs / restart_policy are MappingProxyType wrappers; equality with a
    # plain dict still works because MappingProxyType delegates __eq__ to the
    # underlying mapping.
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


def test_hardened_kwargs_top_level_is_immutable() -> None:
    """The security-critical hardening map must reject mutation at runtime.
    `HARDENED_HOST_CONFIG_KWARGS["read_only"] = False` etc. is a class of
    typo / 'temporary debug edit' that would silently disable container
    isolation — wrapping in MappingProxyType makes those edits raise TypeError."""
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    with pytest.raises(TypeError):
        K["read_only"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        K["new_key"] = "danger"  # type: ignore[index]
    with pytest.raises(TypeError):
        del K["cap_drop"]  # type: ignore[arg-type]


def test_hardened_kwargs_nested_tmpfs_is_immutable() -> None:
    """Nested tmpfs MUST also be immutable — otherwise an attacker (or careless
    test fixture) could enlarge a tmpfs to defeat the pids/memory limits or
    add a writable mount over a path the read-only rootfs is supposed to protect."""
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    with pytest.raises(TypeError):
        K["tmpfs"]["/tmp"] = "rw,size=999g"  # type: ignore[index]
    with pytest.raises(TypeError):
        K["tmpfs"]["/etc"] = "rw,size=64m"  # type: ignore[index]


def test_hardened_kwargs_nested_restart_policy_is_immutable() -> None:
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    with pytest.raises(TypeError):
        K["restart_policy"]["Name"] = "always"  # type: ignore[index]


def test_hardened_kwargs_cap_drop_is_tuple_not_list() -> None:
    """cap_drop=['ALL'] could be mutated via .append('SYS_ADMIN'). A tuple
    forbids that path at the language level."""
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    assert isinstance(K["cap_drop"], tuple)
    assert isinstance(K["security_opt"], tuple)
    with pytest.raises(AttributeError):
        K["cap_drop"].append("SYS_ADMIN")  # type: ignore[attr-defined]


def test_hardened_kwargs_unpacks_via_double_star() -> None:
    """The whole structure is consumed via `**HARDENED_HOST_CONFIG_KWARGS` in
    lifecycle.start — verify that double-star unpacking works through the
    MappingProxyType wrapper (it implements the mapping protocol)."""
    from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS as K

    def _accept(**kwargs):
        return kwargs

    unpacked = _accept(**K)
    assert unpacked["cap_drop"] == ("ALL",)
    assert unpacked["read_only"] is True
    assert unpacked["tmpfs"]["/tmp"] == "rw,size=512m,mode=1777"


def test_hardened_kwargs_function_returns_sdk_compatible_types() -> None:
    """docker-py 7.1's HostConfig.__init__ runs strict isinstance checks:
        restart_policy MUST be `dict` (not Mapping/MappingProxyType)
        security_opt   MUST be `list` (not tuple/Sequence)
    The canonical constant uses MappingProxyType + tuple for immutability;
    hardened_kwargs() must return a fresh dict with SDK-compatible types
    so `containers.run(**hardened_kwargs())` does not TypeError before
    Docker is contacted."""
    from naiw_tasks.docker_client import hardened_kwargs

    k = hardened_kwargs()
    assert isinstance(k, dict)
    assert type(k["restart_policy"]) is dict, (
        f"restart_policy must be `dict` (HostConfig strict isinstance), "
        f"got {type(k['restart_policy']).__name__}"
    )
    assert type(k["security_opt"]) is list, (
        f"security_opt must be `list` (HostConfig strict isinstance), "
        f"got {type(k['security_opt']).__name__}"
    )
    assert type(k["cap_drop"]) is list
    assert type(k["tmpfs"]) is dict


def test_hardened_kwargs_function_passes_real_hostconfig_validation() -> None:
    """End-to-end guard: feed the function's output through docker-py's
    actual HostConfig constructor. This catches any future incompatibility
    with docker-py upgrades (e.g., new isinstance checks) without waiting
    for the live operator gate to surface a TypeError."""
    from docker.types import HostConfig
    from naiw_tasks.docker_client import (
        PINNED_DOCKER_API_VERSION,
        hardened_kwargs,
    )

    # containers.run() splits its kwargs between HostConfig (cap_drop,
    # security_opt, restart_policy, tmpfs, mem_limit, …) and ContainerConfig /
    # NetworkSettings (network, tty, stdin_open). Strip the non-HostConfig
    # fields so we can feed the rest directly into HostConfig() and verify
    # the strict isinstance checks pass on OUR types specifically.
    NOT_HOST_CONFIG = {"network", "tty", "stdin_open"}
    k = hardened_kwargs()
    hc_kwargs = {key: v for key, v in k.items() if key not in NOT_HOST_CONFIG}
    hc = HostConfig(version=PINNED_DOCKER_API_VERSION, **hc_kwargs)
    # The strict-isinstance checks are what bit the previous design — assert
    # the post-validation HostConfig produced the expected canonical values.
    assert hc["RestartPolicy"] == {"Name": "no"}
    assert hc["SecurityOpt"] == ["no-new-privileges"]
    assert hc["CapDrop"] == ["ALL"]
    assert hc["ReadonlyRootfs"] is True


def test_hardened_kwargs_function_returns_fresh_copy_each_call() -> None:
    """Each call returns an independent dict — mutating one MUST NOT leak
    into the next call or the canonical constant. Defends against docker-py
    or other consumers mutating the values they receive."""
    from naiw_tasks.docker_client import (
        HARDENED_HOST_CONFIG_KWARGS,
        hardened_kwargs,
    )

    k1 = hardened_kwargs()
    k1["restart_policy"]["Name"] = "always"
    k1["security_opt"].append("seccomp=unconfined")
    k1["cap_drop"].clear()

    k2 = hardened_kwargs()
    assert k2["restart_policy"] == {"Name": "no"}
    assert k2["security_opt"] == ["no-new-privileges"]
    assert k2["cap_drop"] == ["ALL"]

    # And the constant itself stays unchanged.
    assert HARDENED_HOST_CONFIG_KWARGS["restart_policy"] == {"Name": "no"}
    assert HARDENED_HOST_CONFIG_KWARGS["security_opt"] == ("no-new-privileges",)
    assert HARDENED_HOST_CONFIG_KWARGS["cap_drop"] == ("ALL",)


def test_hardened_kwargs_function_value_parity_with_constant() -> None:
    """Every field in hardened_kwargs() must match the canonical constant
    (just with mutable container types)."""
    from naiw_tasks.docker_client import (
        HARDENED_HOST_CONFIG_KWARGS,
        hardened_kwargs,
    )

    k = hardened_kwargs()
    assert set(k.keys()) == set(HARDENED_HOST_CONFIG_KWARGS.keys())
    for key, v in HARDENED_HOST_CONFIG_KWARGS.items():
        # Equality regardless of container type (dict == MappingProxyType,
        # list == tuple at value level).
        if hasattr(v, "__iter__") and not isinstance(v, str):
            assert list(v) == list(k[key])
        else:
            assert v == k[key]


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
