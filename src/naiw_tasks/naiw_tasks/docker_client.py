"""Sole DockerClient constructor + the hardening kwargs every container starts with.

Every consumer goes through make_client(...). Auto-detecting the daemon via
DOCKER_HOST / the host socket is forbidden: it would bypass naiw-docker-proxy
and grant the controller direct host-socket access, defeating the isolation
model. The only legal route to the Docker engine is `base_url=proxy_url`.

HARDENED_HOST_CONFIG_KWARGS is wrapped in MappingProxyType and uses tuples
for sequence values (cap_drop, security_opt) — the whole structure is
immutable at every level. A typo like
`HARDENED_HOST_CONFIG_KWARGS["read_only"] = False` raises TypeError instead
of silently disabling the read-only rootfs invariant.
"""

from types import MappingProxyType

import docker

HARDENED_HOST_CONFIG_KWARGS = MappingProxyType({
    # Drop every Linux capability — Pi/tmux/git work without any.
    # Tuple (not list) so HARDENED_HOST_CONFIG_KWARGS["cap_drop"].append("...")
    # raises AttributeError instead of silently escalating.
    "cap_drop": ("ALL",),
    # Block setuid escalation inside the container.
    "security_opt": ("no-new-privileges",),
    # Read-only rootfs forces every writable surface to be tmpfs (audit-able).
    "read_only": True,
    # /home/pi tmpfs lets `pip install --user` work under read-only rootfs
    # (verified by the hardened-smoke pass1/pass2 probe).
    "tmpfs": MappingProxyType({
        "/tmp": "rw,size=512m,mode=1777",
        "/run": "rw,size=64m,mode=755",
        "/home/pi": "rw,size=128m,mode=1777",
    }),
    # Cap fork-bombs.
    "pids_limit": 512,
    # 4 GiB mem ceiling, no swap headroom (memswap == mem disables swap usage).
    "mem_limit": "4g",
    "memswap_limit": "4g",
    # 2.0 CPUs in billionths (docker-py kwarg shape equivalent to --cpus=2).
    "nano_cpus": 2_000_000_000,
    # Private bridge network created out-of-band by the deploy compose stack;
    # the controller never creates this network.
    "network": "naiw-task-net",
    # Recovery is operator-driven; never auto-restart on crash.
    "restart_policy": MappingProxyType({"Name": "no"}),
    # tini-equivalent init wrapper as pid 1; signals propagate to tmux/Pi.
    "init": True,
    # tty + stdin_open match `docker run -it`; required for `docker attach` to work.
    "tty": True,
    "stdin_open": True,
})


def make_client(proxy_url: str) -> docker.DockerClient:
    """Construct the only DockerClient used by the controller.

    timeout=10 covers proxy + daemon round-trip on 127.0.0.1; longer values hide
    bugs (a hung proxy should fail loudly, not block the controller indefinitely).
    """
    return docker.DockerClient(base_url=proxy_url, timeout=10)
