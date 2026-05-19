"""Pure-function hardening drift audit for the operator-facing `list` row.

Compares a Docker inspect-style HostConfig + Config (already on every
container.attrs after `containers.list`) against HARDENED_HOST_CONFIG_KWARGS.
Read-only — never mutates task.json. No docker SDK import — the module is
pure logic over dicts so it is testable without spinning up Docker.

Single source of truth: EXPECTED_HOST_CONFIG_DIFF is DERIVED from
HARDENED_HOST_CONFIG_KWARGS at module import. When the operator tightens a
hardening floor (e.g. bumps pids_limit), every still-running task created
before the bump surfaces as `(drift)` on the next `naiw-tasks list`.
"""

from dataclasses import dataclass
from typing import Any

from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS


@dataclass(frozen=True)
class DriftItem:
    """One audited field divergence.

    `severity` is `"security"` for fields whose drift could weaken the
    isolation model (cap_drop, no-new-privileges, ReadonlyRootfs,
    NetworkMode, RestartPolicy, Init, Binds, Tty, OpenStdin, Privileged)
    and `"resource"` for fields whose drift weakens the fork-bomb /
    memory / CPU ceiling (PidsLimit, Memory, MemorySwap, NanoCpus, Tmpfs).
    """

    field: str
    expected: Any
    actual: Any
    severity: str


# Expected-value table derived once at import. Engine-API names are
# `HostConfig.<PascalCase>` / `Config.<PascalCase>`, NOT the docker-py kwarg
# names (snake_case). The mapping happens here so callers compare directly
# against `container.attrs` keys.
#
# Memory ceilings: docker normalises the docker-py `"4g"` kwarg to bytes when
# returning inspect output. 4 GiB = 4_294_967_296 bytes.
EXPECTED_HOST_CONFIG_DIFF: dict[str, Any] = {
    "HostConfig.Privileged": False,
    "HostConfig.CapDrop": list(HARDENED_HOST_CONFIG_KWARGS["cap_drop"]),
    "HostConfig.SecurityOpt_member": "no-new-privileges",
    "HostConfig.ReadonlyRootfs": HARDENED_HOST_CONFIG_KWARGS["read_only"],
    "HostConfig.Tmpfs_keys": tuple(HARDENED_HOST_CONFIG_KWARGS["tmpfs"].keys()),
    "HostConfig.PidsLimit": HARDENED_HOST_CONFIG_KWARGS["pids_limit"],
    "HostConfig.Memory": 4 * 1024 * 1024 * 1024,
    "HostConfig.MemorySwap": 4 * 1024 * 1024 * 1024,
    "HostConfig.NanoCpus": HARDENED_HOST_CONFIG_KWARGS["nano_cpus"],
    "HostConfig.NetworkMode": HARDENED_HOST_CONFIG_KWARGS["network"],
    "HostConfig.RestartPolicy.Name": HARDENED_HOST_CONFIG_KWARGS["restart_policy"]["Name"],
    "HostConfig.Init": HARDENED_HOST_CONFIG_KWARGS["init"],
    "Config.Tty": HARDENED_HOST_CONFIG_KWARGS["tty"],
    "Config.OpenStdin": HARDENED_HOST_CONFIG_KWARGS["stdin_open"],
}


# Severity classification: security-impacting fields vs. resource-ceiling
# fields. List `list` NOTES marker is single `(drift)` regardless; `doctor`
# uses severity to group its output.
_SECURITY_FIELDS: frozenset[str] = frozenset({
    "HostConfig.Privileged",
    "HostConfig.CapDrop",
    "HostConfig.SecurityOpt",
    "HostConfig.ReadonlyRootfs",
    "HostConfig.NetworkMode",
    "HostConfig.RestartPolicy.Name",
    "HostConfig.Init",
    "HostConfig.Binds",
    "Config.Tty",
    "Config.OpenStdin",
})


def _severity(field: str) -> str:
    return "security" if field in _SECURITY_FIELDS else "resource"


def compute_drift(
    host_config: dict,
    config: dict,
    *,
    expected_storage_bind: str,
) -> list[DriftItem]:
    """Compare live attrs against the expected hardening floor.

    Args:
      host_config: container.attrs["HostConfig"] from a live inspect.
      config: container.attrs["Config"] (top-level Config holds Tty / OpenStdin
        — they are NOT under HostConfig despite what docker-py kwargs suggest).
      expected_storage_bind: the per-task storage bind-mount string the
        controller expects on this container (e.g.
        `/abs/path/tasks/<id>/storage:/home/pi:rw`). Drift if absent.

    Returns:
      Empty list if all monitored fields match the hardening floor; otherwise
      one DriftItem per divergent field. Tmpfs may surface two items (one per
      missing key) since /tmp and /run are independent invariants.
    """
    items: list[DriftItem] = []

    # Privileged defense-in-depth: if true, CapDrop and ReadonlyRootfs will
    # ALSO disagree, but the explicit check makes the surfaced diagnostic
    # unambiguous for the operator.
    if host_config.get("Privileged") is True:
        items.append(DriftItem(
            "HostConfig.Privileged",
            False,
            True,
            _severity("HostConfig.Privileged"),
        ))

    # CapDrop must equal ["ALL"]. None / empty / partial sets are all drift.
    actual_cap_drop = host_config.get("CapDrop")
    if list(actual_cap_drop or []) != ["ALL"]:
        items.append(DriftItem(
            "HostConfig.CapDrop",
            ["ALL"],
            actual_cap_drop,
            _severity("HostConfig.CapDrop"),
        ))

    # SecurityOpt: membership check. Docker may auto-add "seccomp=default" so
    # exact-equality would false-positive on every container.
    sec_opt = host_config.get("SecurityOpt") or []
    if not any("no-new-privileges" in str(item) for item in sec_opt):
        items.append(DriftItem(
            "HostConfig.SecurityOpt",
            "no-new-privileges",
            sec_opt,
            _severity("HostConfig.SecurityOpt"),
        ))

    if host_config.get("ReadonlyRootfs") is not True:
        items.append(DriftItem(
            "HostConfig.ReadonlyRootfs",
            True,
            host_config.get("ReadonlyRootfs"),
            _severity("HostConfig.ReadonlyRootfs"),
        ))

    # Tmpfs: docker-py inspect shape is DICT (NOT list). Presence of both /tmp
    # and /run is load-bearing; mode/size strings drift on docker tweaks but
    # don't affect security so we audit keys only.
    tmpfs = host_config.get("Tmpfs") or {}
    for key in ("/tmp", "/run"):
        if key not in tmpfs:
            items.append(DriftItem(
                f"HostConfig.Tmpfs[{key}]",
                "<present>",
                tmpfs.get(key),
                "resource",
            ))

    # PidsLimit: 0 and -1 BOTH mean "unlimited" per the docker docs — we want
    # exactly 512. Any other value (incl. None) is drift.
    if host_config.get("PidsLimit") != 512:
        items.append(DriftItem(
            "HostConfig.PidsLimit",
            512,
            host_config.get("PidsLimit"),
            "resource",
        ))

    expected_mem = EXPECTED_HOST_CONFIG_DIFF["HostConfig.Memory"]
    if host_config.get("Memory") != expected_mem:
        items.append(DriftItem(
            "HostConfig.Memory",
            expected_mem,
            host_config.get("Memory"),
            "resource",
        ))

    expected_swap = EXPECTED_HOST_CONFIG_DIFF["HostConfig.MemorySwap"]
    if host_config.get("MemorySwap") != expected_swap:
        items.append(DriftItem(
            "HostConfig.MemorySwap",
            expected_swap,
            host_config.get("MemorySwap"),
            "resource",
        ))

    expected_nano = EXPECTED_HOST_CONFIG_DIFF["HostConfig.NanoCpus"]
    if host_config.get("NanoCpus") != expected_nano:
        items.append(DriftItem(
            "HostConfig.NanoCpus",
            expected_nano,
            host_config.get("NanoCpus"),
            "resource",
        ))

    expected_net = EXPECTED_HOST_CONFIG_DIFF["HostConfig.NetworkMode"]
    if host_config.get("NetworkMode") != expected_net:
        items.append(DriftItem(
            "HostConfig.NetworkMode",
            expected_net,
            host_config.get("NetworkMode"),
            _severity("HostConfig.NetworkMode"),
        ))

    # RestartPolicy is a dict with a Name field; we audit Name only.
    rp = host_config.get("RestartPolicy") or {}
    expected_rp_name = EXPECTED_HOST_CONFIG_DIFF["HostConfig.RestartPolicy.Name"]
    if rp.get("Name") != expected_rp_name:
        items.append(DriftItem(
            "HostConfig.RestartPolicy.Name",
            expected_rp_name,
            rp.get("Name"),
            _severity("HostConfig.RestartPolicy.Name"),
        ))

    # Init is `bool | None` in moby inspect output — None means the daemon
    # didn't set it (so tini-equivalent is NOT running). Treat None as drift.
    if host_config.get("Init") is not True:
        items.append(DriftItem(
            "HostConfig.Init",
            True,
            host_config.get("Init"),
            _severity("HostConfig.Init"),
        ))

    # Per-task storage bind-mount must be present on the container so Pi's
    # /home/pi survives the recover boundary.
    binds = host_config.get("Binds") or []
    if expected_storage_bind not in binds:
        items.append(DriftItem(
            "HostConfig.Binds[storage]",
            expected_storage_bind,
            binds,
            _severity("HostConfig.Binds"),
        ))

    # Tty and OpenStdin are TOP-LEVEL Config keys, not HostConfig — easy to
    # get wrong because the docker-py kwargs `tty=` and `stdin_open=` look
    # like host-config flags. attach() depends on these.
    if config.get("Tty") is not True:
        items.append(DriftItem(
            "Config.Tty",
            True,
            config.get("Tty"),
            _severity("Config.Tty"),
        ))
    if config.get("OpenStdin") is not True:
        items.append(DriftItem(
            "Config.OpenStdin",
            True,
            config.get("OpenStdin"),
            _severity("Config.OpenStdin"),
        ))

    return items
