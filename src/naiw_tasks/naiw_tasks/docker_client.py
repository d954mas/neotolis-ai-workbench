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
    # tmpfs covers ephemeral writable surfaces. /home/pi is provided as a
    # per-task bind mount from ~/naiw-data/tasks/<id>/storage/ so Pi's home
    # survives container teardown and the recover boundary.
    "tmpfs": MappingProxyType({
        "/tmp": "rw,size=512m,mode=1777",
        "/run": "rw,size=64m,mode=755",
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


# Pinned Engine API version. docker-py defaults to version=None which triggers
# auto-negotiation via GET /version — blocked by the locked proxy (VERSION=0
# in deploy/proxy/README.md). Pinning here skips the negotiation entirely; the
# value matches the API path used everywhere else in the codebase
# (startup_checks proxy probe, smoke harness).
PINNED_DOCKER_API_VERSION: str = "1.43"


def hardened_kwargs() -> dict:
    """SDK-compatible deep copy of HARDENED_HOST_CONFIG_KWARGS for ``**`` unpack.

    docker-py 7.1's ``HostConfig.__init__`` runs strict isinstance checks:
    ``restart_policy`` MUST be a ``dict`` (not a Mapping subtype),
    ``security_opt`` MUST be a ``list`` (not a tuple/Sequence). The canonical
    constant is intentionally wrapped in ``MappingProxyType`` + ``tuple`` for
    immutability invariants — but those types fail HostConfig validation
    with TypeError *before Docker is even contacted*.

    This function builds a fresh, SDK-typed shallow copy each call so:
      - the SDK gets the exact types it requires;
      - the canonical constant stays immutable (no in-place mutation by
        docker-py can leak back into other callers);
      - every ``client.containers.run(**hardened_kwargs())`` is independent.
    """
    raw = dict(HARDENED_HOST_CONFIG_KWARGS)
    raw["cap_drop"] = list(raw["cap_drop"])
    raw["security_opt"] = list(raw["security_opt"])
    raw["tmpfs"] = dict(raw["tmpfs"])
    raw["restart_policy"] = dict(raw["restart_policy"])
    return raw


def make_client(proxy_url: str) -> docker.DockerClient:
    """Construct the only DockerClient used by the controller.

    timeout=10 covers proxy + daemon round-trip on 127.0.0.1; longer values hide
    bugs (a hung proxy should fail loudly, not block the controller indefinitely).

    `version` is pinned (NOT None / "auto") so the constructor does not call
    GET /version — that endpoint is forbidden by the proxy (VERSION=0) and
    would surface as DockerException at controller startup.
    """
    return docker.DockerClient(
        base_url=proxy_url,
        timeout=10,
        version=PINNED_DOCKER_API_VERSION,
    )
