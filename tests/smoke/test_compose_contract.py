"""Phase 3.5 compose contract tests.

File-only — parses deploy/docker-compose.yml with yaml.safe_load. No Docker
required; runs in <100 ms; runs in CI and local pytest. Asserts D-H1 hardening
keys, D-N2 unpublished proxy, D-N1 controller-only-on-naiw-internal, plus a
regression sentinel for the Phase-1 locked proxy allowlist.
"""

from pathlib import Path

import pytest
import yaml

# tests/smoke/<this>.py -> parents[2] is repo root.
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parsed deploy/docker-compose.yml — yaml.safe_load (PyYAML 6.0.3, CLAUDE.md)."""
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


@pytest.mark.smoke
def test_proxy_no_published_port(compose):
    proxy = compose["services"]["naiw-docker-proxy"]
    assert "ports" not in proxy, (
        "D-N2 violated: naiw-docker-proxy must NOT publish a host port; "
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
        f"D-H1: cap_drop must be [ALL]; got {ctrl.get('cap_drop')!r}"
    )
    assert ctrl["security_opt"] == ["no-new-privileges"], (
        f"D-H1: security_opt must be [no-new-privileges]; got {ctrl.get('security_opt')!r}"
    )
    assert ctrl["read_only"] is True, (
        f"D-H1: read_only must be True; got {ctrl.get('read_only')!r}"
    )
    assert ctrl["pids_limit"] == 256, (
        f"D-H1: pids_limit must be 256; got {ctrl.get('pids_limit')!r}"
    )
    assert ctrl["mem_limit"] == "1g", (
        f"D-H1: mem_limit must be '1g'; got {ctrl.get('mem_limit')!r}"
    )
    assert ctrl["cpus"] == 1.0, f"D-H1: cpus must be 1.0; got {ctrl.get('cpus')!r}"
    assert ctrl["restart"] == "no", (
        f"D-H1: restart must be 'no'; got {ctrl.get('restart')!r}"
    )
    tmpfs = set(ctrl["tmpfs"])
    expected_tmpfs = {"/tmp:size=128m,mode=1777", "/run:size=32m,mode=755"}
    assert tmpfs >= expected_tmpfs, (
        f"D-H1: tmpfs missing entries; got {tmpfs!r}, need {expected_tmpfs!r}"
    )


@pytest.mark.smoke
def test_controller_only_on_naiw_internal(compose):
    nets = compose["services"]["naiw-controller"]["networks"]
    assert nets == ["naiw-internal"], (
        f"D-N1 violated: controller must be on naiw-internal ONLY; got {nets!r}"
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
    """Regression sentinel: P02 must NOT touch the proxy allowlist."""
    env = compose["services"]["naiw-docker-proxy"]["environment"]
    yes_vars = {"CONTAINERS", "POST", "ALLOW_START", "ALLOW_STOP", "ALLOW_RESTARTS"}
    deny_sample = {"EXEC", "IMAGES", "VOLUMES", "NETWORKS", "BUILD", "PING", "VERSION", "INFO"}
    for v in yes_vars:
        assert env.get(v) == "1", (
            f"Phase-1 D-26 violated: {v} should be '1'; got {env.get(v)!r}"
        )
    for v in deny_sample:
        assert env.get(v) == "0", (
            f"Phase-1 D-26 violated: {v} should be '0'; got {env.get(v)!r}"
        )
