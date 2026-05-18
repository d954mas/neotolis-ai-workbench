"""Startup checks that gate every CLI invocation."""

import contextlib
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import docker

from naiw_tasks import disk as disk_mod
from naiw_tasks.docker_client import PINNED_DOCKER_API_VERSION

_IMAGE_PI_UID: int = 1000
_WINDOWS_FS_ON_LINUX_RE: re.Pattern[str] = re.compile(r"^/mnt/[A-Za-z]/")
_NAIW_ACK_ENV_VAR: str = "NAIW_ACCEPT_WINDOWS_FS_RISK"
_WINFS_ACK_MARKER_NAME: str = ".naiw-winfs-acked"


class StartupCheckFailed(SystemExit):
    """Exit 2 with a `naiw-tasks: ` stderr prefix."""

    def __init__(self, message: str) -> None:
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
    resolved = data_root.resolve().as_posix()
    if not _WINDOWS_FS_ON_LINUX_RE.match(resolved):
        return

    if os.environ.get(_NAIW_ACK_ENV_VAR) != "1":
        raise StartupCheckFailed(
            f"~/naiw-data/ on Windows-FS path {resolved!r} is not supported "
            f"(Docker Desktop virtiofs/9P breaks fcntl.flock atomicity, "
            f"os.replace non-atomic, chmod 0600 ignored on NTFS). "
            f"To proceed anyway for local dev, set {_NAIW_ACK_ENV_VAR}=1 in your shell."
        )

    marker = data_root / _WINFS_ACK_MARKER_NAME
    if marker.exists():
        return
    print(
        f"naiw-tasks: WARNING - ~/naiw-data/ on Windows-FS path {resolved!r}; "
        f"running with {_NAIW_ACK_ENV_VAR}=1. Known risks: fcntl.flock races, "
        f"os.replace non-atomicity, chmod 0600 ignored on NTFS. Suitable for "
        f"single-task local dev only; do NOT use for production.",
        file=sys.stderr,
        flush=True,
    )
    with contextlib.suppress(OSError):
        marker.touch()


def check_docker_reachable(
    client,
    proxy_url: str,
    attempts: int = 5,
    delay_s: float = 0.5,
) -> None:
    last_exc: docker.errors.DockerException | None = None
    for attempt in range(attempts):
        try:
            client.containers.list(limit=1)
            return
        except docker.errors.DockerException as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(delay_s)
    raise StartupCheckFailed(
        f"cannot reach Docker via proxy at {proxy_url}; "
        f"is deploy/docker-compose.yml up? ({last_exc})"
    ) from last_exc


def check_proxy_allowlist_drift(proxy_url: str) -> None:
    base = proxy_url.replace("tcp://", "http://")
    api = f"{base}/v{PINNED_DOCKER_API_VERSION}"
    probe_id = f"naiw-probe-{uuid.uuid4().hex}"

    def expect_http_status(method: str, path: str, expected: int, why: str) -> None:
        req = urllib.request.Request(f"{api}{path}", method=method)
        label = f"{method} {path}"
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            if exc.code == expected:
                return
            raise StartupCheckFailed(
                f"proxy allowlist drift - {label} returned {exc.code}; "
                f"expected {expected} ({why}); cf. deploy/docker-compose.yml"
            ) from exc
        except urllib.error.URLError as exc:
            raise StartupCheckFailed(
                f"proxy allowlist drift - {label} failed to reach the proxy "
                f"at {proxy_url} ({exc}); expected {expected} ({why}); "
                f"cf. deploy/docker-compose.yml"
            ) from exc
        else:
            raise StartupCheckFailed(
                f"proxy allowlist drift - {label} returned 2xx; expected "
                f"{expected} ({why}); cf. deploy/docker-compose.yml"
            )

    expect_http_status(
        "POST",
        f"/exec/{probe_id}/start",
        403,
        "EXEC=0",
    )
    for method, path in (
        ("POST", f"/containers/{probe_id}/start"),
        ("POST", f"/containers/{probe_id}/stop"),
    ):
        expect_http_status(
            method,
            path,
            404,
            "container write path allowed through proxy; daemon rejects fake id",
        )


def _format_iec_bytes(n: int) -> str:
    """Render an int byte count as <num><IEC unit>. KISS sizer.

    Matches the IEC binary convention used in config.yaml's max_data_size
    field; operator-facing numbers stay readable (e.g. 48.2GiB) instead of
    raw byte counts.
    """
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(f)}{unit}"
            return f"{f:.1f}{unit}"
        f /= 1024.0
    return f"{f:.1f}TiB"  # unreachable


def check_disk_threshold(cfg, verb: str = "start") -> None:
    """Refuse start/recover when ~/naiw-data/ is at >95% of max_data_size.

    Other commands (attach, finish, list, output, clean, disk) are NOT gated
    - they may free disk; gating them would lock the operator out of recovery.

    `verb` is "start" or "recover" - both paths emit the SAME body but the
    lead clause adapts so the operator sees the right verb.
    """
    used, max_bytes, pct = disk_mod._check_threshold(cfg)
    if pct > 95.0:
        used_human = _format_iec_bytes(used)
        max_human = _format_iec_bytes(max_bytes)
        raise StartupCheckFailed(
            f"cannot {verb} — ~/naiw-data/ is at "
            f"{pct:.1f}% of max_data_size "
            f"({used_human} / {max_human}).\n"
            f"  Reclaim space first: naiw-tasks clean --older-than 30d\n"
            f"  Or raise the limit in ~/naiw-data/config.yaml: "
            f"max_data_size: 100GiB"
        )


def warn_uid_mismatch_with_image() -> None:
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


def run_all(
    cfg,
    client,
    gate_disk_threshold: bool = False,
    disk_threshold_verb: str = "start",
) -> None:
    data_root = Path(cfg.data_root)
    check_naiw_data_not_symlink(data_root)
    check_not_on_windows_fs_on_linux(data_root)
    check_docker_reachable(client, cfg.docker_proxy_url)
    check_proxy_allowlist_drift(cfg.docker_proxy_url)
    warn_uid_mismatch_with_image()
    if gate_disk_threshold:
        check_disk_threshold(cfg, verb=disk_threshold_verb)
