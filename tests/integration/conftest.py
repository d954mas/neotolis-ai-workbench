"""Pytest configuration for tests/integration/.

Registers the `integration` marker and brings up deploy/docker-compose.yml
plus the wrapper shim ONCE per pytest session. Tests invoke `naiw-tasks`
through the installed shim via subprocess.run — NEVER via
click.testing.CliRunner. The wrapper-driven path exercises the full
production wire (wrapper -> docker compose run --rm -> controller ->
docker SDK -> proxy -> engine) that unit tests cannot reach.

Skipped automatically when the docker CLI is missing or the daemon is not
reachable. CI brings up the stack on ubuntu-latest; local Windows runs are
skipped cleanly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = str(REPO_ROOT / "deploy" / "docker-compose.yml")
_INSTALL_WRAPPER = str(REPO_ROOT / "scripts" / "install-wrapper.sh")
_TASK_NET = "naiw-task-net"
_PROXY_SERVICE = "naiw-docker-proxy"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require a live Docker daemon and the "
        "deployed compose stack (skipped when Docker is not reachable).",
    )


def pytest_runtest_setup(item):
    if "integration" not in item.keywords:
        return
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH")
    rc = subprocess.run(
        ["docker", "version"],
        capture_output=True,
        check=False,
    )
    if rc.returncode != 0:
        pytest.skip("docker daemon not reachable")


def _wait_for_proxy(env: dict[str, str], timeout_s: int = 60) -> None:
    """Poll `docker compose exec naiw-docker-proxy true` until success.

    Compose deliberately omits a healthcheck. The `exec -T true` probe
    returns 0 once the proxy container is up and the proxy process is
    accepting Unix-socket connections — a stricter contract than a plain
    TCP probe because it traverses the private network.
    """
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        rc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                _COMPOSE_FILE,
                "exec",
                "-T",
                _PROXY_SERVICE,
                "true",
            ],
            env=env,
            capture_output=True,
            check=False,
            text=True,
        )
        if rc.returncode == 0:
            return
        last_err = rc.stderr
        time.sleep(2)
    raise TimeoutError(
        f"proxy not reachable after {timeout_s}s: {last_err}"
    )


@pytest.fixture(scope="session")
def compose_stack(tmp_path_factory):
    """Bring up compose + install the wrapper shim into a tmp PATH dir.

    Session-scoped: the 5 scenario modules share the bring-up cost. Each
    test gets its own task ids via project aliases and tmp data tree
    isolation. Teardown runs `docker compose down -v` to drop volumes;
    naiw-task-net is left intact (external) so re-runs reuse it.
    """
    data_root = tmp_path_factory.mktemp("naiw-data")
    (data_root / "secrets").mkdir(mode=0o700)
    (data_root / "pi-packages").mkdir()
    (data_root / "workspace" / "repos").mkdir(parents=True)
    (data_root / "tasks").mkdir()
    (data_root / "projects.yaml").write_text(
        "projects: {}\n", encoding="utf-8"
    )

    env = {
        **os.environ,
        "NAIW_DATA": str(data_root),
        # Point the installed wrapper at the in-repo compose file (the
        # wrapper hardcodes /etc/naiw/docker-compose.yml otherwise; CI
        # never sudo-installs that path).
        "NAIW_COMPOSE_FILE": _COMPOSE_FILE,
    }

    # External network MUST exist before compose up. naiw-task-net is
    # declared `external: true` in deploy/docker-compose.yml — compose
    # refuses to read the file with "network not found" if we skip this.
    subprocess.run(
        ["docker", "network", "create", _TASK_NET],
        capture_output=True,
        check=False,
        env=env,
    )

    subprocess.run(
        ["docker", "compose", "-f", _COMPOSE_FILE, "up", "-d"],
        env=env,
        check=True,
    )
    try:
        _wait_for_proxy(env)

        bin_dir = tmp_path_factory.mktemp("bin")
        subprocess.run(
            ["bash", _INSTALL_WRAPPER, "--prefix", str(bin_dir)],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        env_with_path = {
            **env,
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        }

        yield {
            "env": env_with_path,
            "data_root": data_root,
            "bin_dir": bin_dir,
            "compose_file": _COMPOSE_FILE,
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", _COMPOSE_FILE, "down", "-v"],
            env=env,
            capture_output=True,
            check=False,
        )


@pytest.fixture
def run_naiw_tasks(compose_stack):
    """Helper to invoke `naiw-tasks` via the installed wrapper.

    Each call shells out through `docker compose run --rm naiw-controller`
    so the test exercises the same wire the operator would on a real box.
    """
    env = compose_stack["env"]

    def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["naiw-tasks", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    return _run
