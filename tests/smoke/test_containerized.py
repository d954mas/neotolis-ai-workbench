"""Containerized-loop smoke wrapper.

Mirrors tests/smoke/test_hardened.py: invokes run-containerized-smoke.sh via
subprocess and asserts returncode 0. Linux-only marker keeps Windows pytest
collection clean. Two unit-style spot-checks (proxy-no-port and controller
image labels) pinpoint the cause when the harness fails as a black box.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "smoke" / "run-containerized-smoke.sh"
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.yml"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.mark.smoke
@pytest.mark.linux_only
def test_containerized_smoke_harness_passes():
    """End-to-end: run-containerized-smoke.sh exits 0."""
    if not _docker_available():
        pytest.skip("docker CLI not on PATH")
    result = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        print("STDOUT:\n" + result.stdout)
        print("STDERR:\n" + result.stderr)
    assert result.returncode == 0


@pytest.mark.smoke
@pytest.mark.linux_only
def test_proxy_unpublished_via_inspect():
    """Spot-check: proxy has no HostConfig.PortBindings."""
    if not _docker_available():
        pytest.skip("docker CLI not on PATH")
    # naiw-task-net is `external: true` in compose; compose may refuse to read
    # the file with "external network not found" before bringing up the proxy.
    # Pre-create idempotently (rc=1 on "already exists" is fine).
    subprocess.run(
        ["docker", "network", "create", "naiw-task-net"],
        capture_output=True,
    )
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "naiw-docker-proxy"],
            check=True, capture_output=True,
        )
        out = subprocess.run(
            [
                "docker",
                "inspect",
                "naiw-docker-proxy",
                "--format",
                "{{json .HostConfig.PortBindings}}",
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "down"],
            capture_output=True,
        )
    assert out in ("{}", "null", ""), (
        f"proxy must not publish a host port; got bindings: {out!r}"
    )


@pytest.mark.smoke
@pytest.mark.linux_only
def test_controller_image_labels():
    """Controller image carries the expected label set."""
    if not _docker_available():
        pytest.skip("docker CLI not on PATH")
    cfg = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
        check=True, capture_output=True, text=True,
    ).stdout
    image = json.loads(cfg)["services"]["naiw-controller"]["image"]

    inspect_out = subprocess.run(
        ["docker", "inspect", image, "--format", "{{json .Config.Labels}}"],
        capture_output=True, text=True,
    )
    if inspect_out.returncode != 0:
        subprocess.run(["docker", "pull", image], check=True, capture_output=True)
        inspect_out = subprocess.run(
            ["docker", "inspect", image, "--format", "{{json .Config.Labels}}"],
            check=True, capture_output=True, text=True,
        )

    labels = json.loads(inspect_out.stdout.strip())
    assert labels.get("naiw.managed") == "1", labels
    assert labels.get("naiw.role") == "controller", labels
    assert labels.get("naiw.docker-api-version") == "1.43", labels
    assert "naiw.version" in labels, labels
    assert "naiw.git-sha" in labels, labels
    assert "org.opencontainers.image.source" in labels, labels
