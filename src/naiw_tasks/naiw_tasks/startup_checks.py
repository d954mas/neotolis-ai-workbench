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

import contextlib
import os
import platform
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import docker

from naiw_tasks.docker_client import PINNED_DOCKER_API_VERSION

# The task image bakes `pi` as uid 1000 (image/Dockerfile). When the operator's
# host uid differs, files Pi writes inside the container land on the host
# bind mount as owner=1000 — not the operator. Reads OK; modifications need
# `sudo chown`. _make_skeleton chmod's the bind-mount sources to 1777 so this
# is a UX annoyance, not a functional break, but worth surfacing once at start.
_IMAGE_PI_UID: int = 1000

# Any /mnt/<letter>/ path on WSL2 is a Windows-FS mount (Docker Desktop
# virtiofs/9P). Original /mnt/c/-only check was a false-negative on /mnt/d/ etc.
_WINDOWS_FS_ON_LINUX_RE: re.Pattern[str] = re.compile(r"^/mnt/[a-z]/")

# Phase 3.5 D-S3: WinFS warning + sticky env-var ack. Exact match on "1"
# (not truthy) so accidental settings like "true" / "yes" / "0" still raise.
_NAIW_ACK_ENV_VAR: str = "NAIW_ACCEPT_WINDOWS_FS_RISK"
# Marker lives on the controller container's tmpfs (/tmp is tmpfs per D-H1),
# so it resets on every fresh `docker compose run --rm`. Suppresses the WARNING
# noise to once per controller invocation, not once per shell session.
_WINFS_ACK_MARKER: Path = Path("/tmp/.naiw-winfs-acked")


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
    """Warn (or fatal) when ~/naiw-data/ resolves under /mnt/<letter>/ on Linux.

    Phase 3.5 D-S3 behavior matrix:
      - Not on Linux                                -> no-op.
      - Path does not match /mnt/<letter>/          -> no-op.
      - Matches AND NAIW_ACCEPT_WINDOWS_FS_RISK!='1'-> StartupCheckFailed (exit 2).
      - Matches AND NAIW_ACCEPT_WINDOWS_FS_RISK=='1'-> one-time WARNING; return None.

    The opt-in env var is sticky (operator sets it in their shell rc). Suppression
    of repeated warnings uses the /tmp tmpfs marker (D-H1 ensures /tmp is tmpfs);
    fresh `docker compose run --rm` resets the marker, so each new container
    surfaces the WARNING once. Production VPS on Linux ext4 never trips this branch.
    """
    if platform.system() != "Linux":
        return
    resolved = str(data_root.resolve())
    if not _WINDOWS_FS_ON_LINUX_RE.match(resolved):
        return

    if os.environ.get(_NAIW_ACK_ENV_VAR) != "1":
        raise StartupCheckFailed(
            f"~/naiw-data/ on Windows-FS path {resolved!r} is not supported "
            f"(Docker Desktop virtiofs/9P breaks fcntl.flock atomicity, "
            f"os.replace non-atomic, chmod 0600 ignored on NTFS). "
            f"To proceed anyway for local dev, set {_NAIW_ACK_ENV_VAR}=1 in your shell."
        )

    if _WINFS_ACK_MARKER.exists():
        return
    print(
        f"naiw-tasks: WARNING - ~/naiw-data/ on Windows-FS path {resolved!r}; "
        f"running with {_NAIW_ACK_ENV_VAR}=1. Known risks: fcntl.flock races, "
        f"os.replace non-atomicity, chmod 0600 ignored on NTFS. Suitable for "
        f"single-task local dev only; do NOT use for production.",
        file=sys.stderr,
        flush=True,
    )
    # Marker creation failures (read-only /tmp) make the warning print every
    # invocation - annoying but not broken. Stay silent here.
    with contextlib.suppress(OSError):
        _WINFS_ACK_MARKER.touch()


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
    # Use the same pinned API version the SDK does — single source of truth,
    # no chance of probe-URL drifting from the version naiw_tasks actually
    # speaks if the constant is ever bumped.
    req = urllib.request.Request(
        f"{base}/v{PINNED_DOCKER_API_VERSION}/exec/fakeid/start",
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


def warn_uid_mismatch_with_image() -> None:
    """Non-fatal: warn once if the operator's uid differs from the image's
    baked `pi` uid. _make_skeleton's 1777 mode keeps the controller working,
    but files Pi creates inside containers will appear on the host as
    owner=uid 1000. Operator can still read; modifying needs `sudo chown`.
    """
    # getuid is POSIX-only. On non-POSIX (Windows-native) the controller is
    # already unsupported by other startup checks; quietly skip here.
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return
    operator_uid = getuid()
    if operator_uid != _IMAGE_PI_UID:
        print(
            f"naiw-tasks: note: operator uid={operator_uid} differs from the "
            f"task image's pi uid={_IMAGE_PI_UID}; files Pi writes inside "
            f"containers will appear on the host as owner={_IMAGE_PI_UID}. "
            f"Bind mounts work (mode 1777) but `sudo chown` is needed to "
            f"modify Pi-created files from the host shell. To eliminate this, "
            f"rebuild the image with the operator's uid; see image/Dockerfile.",
            file=sys.stderr,
            flush=True,
        )


def run_all(cfg, client) -> None:
    """Run every fatal probe in order. Raises StartupCheckFailed on any failure."""
    data_root = Path(cfg.data_root)
    check_naiw_data_not_symlink(data_root)
    check_not_on_windows_fs_on_linux(data_root)
    check_docker_reachable(client, cfg.docker_proxy_url)
    check_proxy_allowlist_drift(cfg.docker_proxy_url)
    warn_uid_mismatch_with_image()
