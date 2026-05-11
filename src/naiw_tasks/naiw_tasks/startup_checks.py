"""Four fatal startup probes that gate every CLI invocation.

is_symlink() inspects the last component only — intermediate-symlink protection
lives in path_validation.validate_bind_source via Path.resolve(strict=True).
The two checks together cover the surface.

Click's UsageError default exit code is 2 — same as ours here. The naiw-tasks:
stderr prefix is the operator-visible distinguisher (a Click usage error
prints 'Usage: naiw-tasks ...' first).

live-restore / daemon-version are intentionally NOT probed here — proxy has
INFO=0 (a security invariant). Operators verify live-restore manually after
Docker daemon install via `docker info --format '{{.LiveRestoreEnabled}}'`.
"""

import platform
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import docker

# Any /mnt/<letter>/ path on WSL2 is a Windows-FS mount (Docker Desktop
# virtiofs/9P). Original /mnt/c/-only check was a false-negative on /mnt/d/ etc.
_WINDOWS_FS_ON_LINUX_RE: re.Pattern[str] = re.compile(r"^/mnt/[a-z]/")


class StartupCheckFailed(SystemExit):
    """Exit 2 with a `naiw-tasks: ` stderr prefix."""

    def __init__(self, message: str) -> None:
        # Flush after every line — without an explicit flush, SystemExit can
        # tear the interpreter down before stderr drains when stderr is a pipe
        # (CI logs, click.testing.CliRunner, `2>` redirects), and the operator
        # sees an empty error.
        print(f"naiw-tasks: {message}", file=sys.stderr, flush=True)
        super().__init__(2)


def check_naiw_data_not_symlink(data_root: Path) -> None:
    if data_root.is_symlink():
        target = data_root.resolve()
        raise StartupCheckFailed(
            f"~/naiw-data/ must not be a symlink (got: {target}); "
            f"resolve and remount"
        )


def check_not_on_windows_fs_on_linux(data_root: Path) -> None:
    if platform.system() != "Linux":
        return
    resolved = str(data_root.resolve())
    if _WINDOWS_FS_ON_LINUX_RE.match(resolved):
        raise StartupCheckFailed(
            f"~/naiw-data/ on Windows-FS path {resolved!r} is not supported "
            f"(Docker Desktop virtiofs/9P semantics); "
            f"move to a Linux-FS path on WSL2"
        )


def check_docker_reachable(client, proxy_url: str) -> None:
    try:
        # Cheapest allowed call — proxy CONTAINERS=1 covers it.
        client.containers.list(limit=1)
    except docker.errors.DockerException as exc:
        raise StartupCheckFailed(
            f"cannot reach Docker via proxy at {proxy_url}; "
            f"is deploy/docker-compose.yml up? ({exc})"
        ) from exc


def check_proxy_allowlist_drift(proxy_url: str) -> None:
    """Negative probe: an EXEC endpoint MUST 403 (proxy EXEC=0 invariant)."""
    # proxy_url is tcp://host:port — reshape to http://host:port for urllib.
    base = proxy_url.replace("tcp://", "http://")
    req = urllib.request.Request(
        f"{base}/v1.43/exec/fakeid/start",
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return
        raise StartupCheckFailed(
            f"proxy allowlist drift — POST /exec/.../start returned {exc.code}; "
            f"expected 403 (EXEC=0); cf. deploy/docker-compose.yml"
        ) from exc
    except urllib.error.URLError as exc:
        raise StartupCheckFailed(
            f"proxy allowlist drift — POST /exec/.../start failed to reach "
            f"the proxy at {proxy_url} ({exc}); "
            f"expected 403 (EXEC=0); cf. deploy/docker-compose.yml"
        ) from exc
    else:
        raise StartupCheckFailed(
            "proxy allowlist drift — POST /exec/.../start returned 2xx; "
            "expected 403 (EXEC=0); cf. deploy/docker-compose.yml"
        )


def run_all(cfg, client) -> None:
    """Run every fatal probe in order. Raises StartupCheckFailed on any failure."""
    data_root = Path(cfg.data_root)
    check_naiw_data_not_symlink(data_root)
    check_not_on_windows_fs_on_linux(data_root)
    check_docker_reachable(client, cfg.docker_proxy_url)
    check_proxy_allowlist_drift(cfg.docker_proxy_url)
