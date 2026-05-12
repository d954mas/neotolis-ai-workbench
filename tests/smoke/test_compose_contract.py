"""Compose contract tests — file-only, no Docker required.

Parses deploy/docker-compose.yml with yaml.safe_load and asserts hardening
keys, proxy port absence, controller network isolation, and a regression
sentinel for the locked proxy allowlist. Runs in <100 ms on any platform.
"""

from pathlib import Path

import pytest
import yaml

# tests/smoke/<this>.py -> parents[2] is repo root.
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


@pytest.mark.smoke
def test_proxy_no_published_port(compose):
    proxy = compose["services"]["naiw-docker-proxy"]
    assert "ports" not in proxy, (
        f"naiw-docker-proxy must NOT publish a host port; "
        f"got ports={proxy.get('ports')!r}"
    )


@pytest.mark.smoke
def test_controller_service_exists(compose):
    assert "naiw-controller" in compose["services"], (
        "naiw-controller service missing from deploy/docker-compose.yml"
    )


@pytest.mark.smoke
def test_controller_service_hardened(compose):
    ctrl = compose["services"]["naiw-controller"]
    assert ctrl["cap_drop"] == ["ALL"], (
        f"cap_drop must be [ALL]; got {ctrl.get('cap_drop')!r}"
    )
    assert ctrl["security_opt"] == ["no-new-privileges"], (
        f"security_opt must be [no-new-privileges]; got {ctrl.get('security_opt')!r}"
    )
    assert ctrl["read_only"] is True, (
        f"read_only must be True; got {ctrl.get('read_only')!r}"
    )
    assert ctrl["pids_limit"] == 256, (
        f"pids_limit must be 256; got {ctrl.get('pids_limit')!r}"
    )
    assert ctrl["mem_limit"] == "1g", (
        f"mem_limit must be '1g'; got {ctrl.get('mem_limit')!r}"
    )
    assert ctrl["cpus"] == 1.0, f"cpus must be 1.0; got {ctrl.get('cpus')!r}"
    assert ctrl["restart"] == "no", (
        f"restart must be 'no'; got {ctrl.get('restart')!r}"
    )
    tmpfs = set(ctrl["tmpfs"])
    expected_tmpfs = {"/tmp:size=128m,mode=1777", "/run:size=32m,mode=755"}
    assert tmpfs >= expected_tmpfs, (
        f"tmpfs missing entries; got {tmpfs!r}, need {expected_tmpfs!r}"
    )


@pytest.mark.smoke
def test_controller_only_on_naiw_internal(compose):
    nets = compose["services"]["naiw-controller"]["networks"]
    assert nets == ["naiw-internal"], (
        f"controller must be on naiw-internal ONLY (must not see task net); "
        f"got {nets!r}"
    )


@pytest.mark.smoke
def test_controller_bind_mounts_naiw_data(compose):
    """The :/naiw-data mount must exist and its source must reference the
    NAIW_DATA env var so operator overrides flow through compose substitution.
    Structural parse (split on ':') instead of literal-substring match so the
    test stays green if the default fallback syntax is reformatted."""
    vols = compose["services"]["naiw-controller"]["volumes"]
    data_mounts = [v for v in vols if isinstance(v, str) and v.endswith(":/naiw-data")]
    assert data_mounts, (
        f"naiw-controller missing the :/naiw-data bind mount; got volumes={vols!r}"
    )
    src, _, _ = data_mounts[0].partition(":")
    assert "NAIW_DATA" in src, (
        f"NAIW_DATA env var must drive the bind-mount source for operator "
        f"override; got src={src!r}"
    )


@pytest.mark.smoke
def test_proxy_locked_allowlist_intact(compose):
    """Regression sentinel: the proxy allowlist (5 yes, ~22 explicit-no) must
    not drift. Smoke-checks a representative subset; the full table lives in
    deploy/proxy/README.md."""
    env = compose["services"]["naiw-docker-proxy"]["environment"]
    yes_vars = {"CONTAINERS", "POST", "ALLOW_START", "ALLOW_STOP", "ALLOW_RESTARTS"}
    deny_sample = {"EXEC", "IMAGES", "VOLUMES", "NETWORKS", "BUILD", "PING", "VERSION", "INFO"}
    for v in yes_vars:
        assert env.get(v) == "1", (
            f"proxy allowlist drift: {v} should be '1'; got {env.get(v)!r}"
        )
    for v in deny_sample:
        assert env.get(v) == "0", (
            f"proxy allowlist drift: {v} should be '0'; got {env.get(v)!r}"
        )
