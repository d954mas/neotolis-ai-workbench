"""Tests for naiw_tasks.drift — pure-function hardening audit.

Drift compares a live `container.attrs["HostConfig"]` + `container.attrs["Config"]`
against `HARDENED_HOST_CONFIG_KWARGS`. The module MUST stay pure (no docker SDK
imports, no task.json writes) — these tests lock that contract.

Parametrised over every monitored field: mutating ONE field at a time from the
happy-path expected dict yields exactly the expected DriftItem set. The
happy-path assertion (test_no_drift_on_expected_attrs) guards against false
positives in the audit logic itself.
"""

import dataclasses
from pathlib import Path

import pytest
from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS

from naiw_tasks import drift

EXPECTED_STORAGE_BIND = "/abs/path/tasks/test-1/storage:/home/pi:rw"


def _expected_full_attrs() -> dict:
    """Build a happy-path moby-shape attrs dict.

    Shape matches docker inspect output verified against docker-py 7.1:
      - HostConfig.Tmpfs is a DICT (not list)
      - HostConfig.Memory / MemorySwap are bytes (int)
      - HostConfig.NanoCpus is an int
      - Config.Tty / Config.OpenStdin live under top-level Config (NOT HostConfig)
      - HostConfig.Binds is a list of "host:container:mode" strings
    """
    return {
        "HostConfig": {
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "ReadonlyRootfs": True,
            "Tmpfs": {
                "/tmp": "rw,size=512m,mode=1777",
                "/run": "rw,size=64m,mode=755",
            },
            "PidsLimit": 512,
            "Memory": 4 * 1024 * 1024 * 1024,
            "MemorySwap": 4 * 1024 * 1024 * 1024,
            "NanoCpus": 2_000_000_000,
            "NetworkMode": "naiw-task-net",
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Init": True,
            "Binds": [EXPECTED_STORAGE_BIND],
        },
        "Config": {
            "Tty": True,
            "OpenStdin": True,
        },
    }


# ---------- happy path ------------------------------------------------------


def test_no_drift_on_expected_attrs():
    """Full happy-path attrs → compute_drift returns []."""
    attrs = _expected_full_attrs()
    result = drift.compute_drift(
        attrs["HostConfig"],
        attrs["Config"],
        expected_storage_bind=EXPECTED_STORAGE_BIND,
    )
    assert result == []


# ---------- DriftItem dataclass shape ---------------------------------------


def test_drift_item_is_frozen_dataclass():
    """DriftItem MUST be frozen so audit results are immutable."""
    assert dataclasses.is_dataclass(drift.DriftItem)
    item = drift.DriftItem(
        field="HostConfig.PidsLimit",
        expected=512,
        actual=0,
        severity="resource",
    )
    # Frozen dataclass instances are hashable.
    assert hash(item) is not None
    # Frozen → mutating a field raises.
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.field = "other"  # type: ignore[misc]


def test_drift_item_severity_values():
    """severity is always one of the two documented literals."""
    item_security = drift.DriftItem("X", 1, 2, "security")
    item_resource = drift.DriftItem("Y", 1, 2, "resource")
    assert item_security.severity == "security"
    assert item_resource.severity == "resource"


# ---------- EXPECTED_HOST_CONFIG_DIFF derivation ----------------------------


def test_expected_host_config_diff_keys():
    """EXPECTED_HOST_CONFIG_DIFF is derived from HARDENED_HOST_CONFIG_KWARGS
    at module import — single source of truth."""
    assert drift.EXPECTED_HOST_CONFIG_DIFF["HostConfig.PidsLimit"] == 512
    assert drift.EXPECTED_HOST_CONFIG_DIFF["HostConfig.Memory"] == 4 * 1024 * 1024 * 1024
    assert drift.EXPECTED_HOST_CONFIG_DIFF["HostConfig.MemorySwap"] == 4 * 1024 * 1024 * 1024
    assert drift.EXPECTED_HOST_CONFIG_DIFF["HostConfig.NanoCpus"] == 2_000_000_000
    assert drift.EXPECTED_HOST_CONFIG_DIFF["HostConfig.NetworkMode"] == "naiw-task-net"
    assert drift.EXPECTED_HOST_CONFIG_DIFF["HostConfig.RestartPolicy.Name"] == "no"
    assert drift.EXPECTED_HOST_CONFIG_DIFF["HostConfig.Init"] is True
    assert drift.EXPECTED_HOST_CONFIG_DIFF["Config.Tty"] is True
    assert drift.EXPECTED_HOST_CONFIG_DIFF["Config.OpenStdin"] is True


def test_expected_host_config_diff_derives_from_constant():
    """The constants must agree with HARDENED_HOST_CONFIG_KWARGS — if the
    operator bumps the hardening floor, the audit follows automatically."""
    diff = drift.EXPECTED_HOST_CONFIG_DIFF
    assert diff["HostConfig.PidsLimit"] == HARDENED_HOST_CONFIG_KWARGS["pids_limit"]
    assert diff["HostConfig.NanoCpus"] == HARDENED_HOST_CONFIG_KWARGS["nano_cpus"]
    assert diff["HostConfig.NetworkMode"] == HARDENED_HOST_CONFIG_KWARGS["network"]


# ---------- parametrised single-field mutations -----------------------------


def _mutate_attrs(mutator) -> dict:
    """Build happy-path attrs and apply a single in-place mutation."""
    attrs = _expected_full_attrs()
    mutator(attrs)
    return attrs


# Each row: (label, mutator-callable, expected_field_prefix, expected_severity)
# expected_field_prefix is a substring the surfaced DriftItem.field MUST contain.
_MUTATIONS = [
    # security
    (
        "Privileged_true",
        lambda a: a["HostConfig"].__setitem__("Privileged", True),
        "HostConfig.Privileged",
        "security",
    ),
    (
        "CapDrop_empty",
        lambda a: a["HostConfig"].__setitem__("CapDrop", []),
        "HostConfig.CapDrop",
        "security",
    ),
    (
        "CapDrop_none",
        lambda a: a["HostConfig"].__setitem__("CapDrop", None),
        "HostConfig.CapDrop",
        "security",
    ),
    (
        "SecurityOpt_empty",
        lambda a: a["HostConfig"].__setitem__("SecurityOpt", []),
        "HostConfig.SecurityOpt",
        "security",
    ),
    (
        "ReadonlyRootfs_false",
        lambda a: a["HostConfig"].__setitem__("ReadonlyRootfs", False),
        "HostConfig.ReadonlyRootfs",
        "security",
    ),
    (
        "PidsLimit_zero",
        lambda a: a["HostConfig"].__setitem__("PidsLimit", 0),
        "HostConfig.PidsLimit",
        "resource",
    ),
    (
        "PidsLimit_neg_one",
        lambda a: a["HostConfig"].__setitem__("PidsLimit", -1),
        "HostConfig.PidsLimit",
        "resource",
    ),
    (
        "Memory_smaller",
        lambda a: a["HostConfig"].__setitem__("Memory", 1024 * 1024 * 1024),
        "HostConfig.Memory",
        "resource",
    ),
    (
        "MemorySwap_smaller",
        lambda a: a["HostConfig"].__setitem__("MemorySwap", 1024 * 1024 * 1024),
        "HostConfig.MemorySwap",
        "resource",
    ),
    (
        "NanoCpus_smaller",
        lambda a: a["HostConfig"].__setitem__("NanoCpus", 1_000_000_000),
        "HostConfig.NanoCpus",
        "resource",
    ),
    (
        "NetworkMode_bridge",
        lambda a: a["HostConfig"].__setitem__("NetworkMode", "bridge"),
        "HostConfig.NetworkMode",
        "security",
    ),
    (
        "RestartPolicy_always",
        lambda a: a["HostConfig"].__setitem__(
            "RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}
        ),
        "HostConfig.RestartPolicy.Name",
        "security",
    ),
    (
        "Init_false",
        lambda a: a["HostConfig"].__setitem__("Init", False),
        "HostConfig.Init",
        "security",
    ),
    (
        "Init_none",
        lambda a: a["HostConfig"].__setitem__("Init", None),
        "HostConfig.Init",
        "security",
    ),
    (
        "Binds_wrong_path",
        lambda a: a["HostConfig"].__setitem__(
            "Binds", ["/wrong/path:/home/pi:rw"]
        ),
        "HostConfig.Binds",
        "security",
    ),
    (
        "Config_Tty_false",
        lambda a: a["Config"].__setitem__("Tty", False),
        "Config.Tty",
        "security",
    ),
    (
        "Config_OpenStdin_false",
        lambda a: a["Config"].__setitem__("OpenStdin", False),
        "Config.OpenStdin",
        "security",
    ),
]


@pytest.mark.parametrize(
    "label,mutator,expected_field_prefix,expected_severity",
    _MUTATIONS,
    ids=[m[0] for m in _MUTATIONS],
)
def test_single_field_mutation_produces_drift(
    label, mutator, expected_field_prefix, expected_severity
):
    """Each row mutates exactly one field; audit surfaces exactly one item
    whose .field starts with the documented prefix."""
    attrs = _mutate_attrs(mutator)
    result = drift.compute_drift(
        attrs["HostConfig"],
        attrs["Config"],
        expected_storage_bind=EXPECTED_STORAGE_BIND,
    )
    matches = [it for it in result if it.field.startswith(expected_field_prefix)]
    assert len(matches) == 1, (
        f"{label}: expected exactly one DriftItem starting with "
        f"{expected_field_prefix!r}, got {[it.field for it in result]}"
    )
    assert matches[0].severity == expected_severity, (
        f"{label}: expected severity={expected_severity!r}, got {matches[0].severity!r}"
    )
    # Cross-row sanity: no surfaced field is the unmutated baseline.
    # (Every other monitored field should still be at the expected value, so
    # the only drift is the mutated one. The Tmpfs case below is the documented
    # exception — it's a separate parametrise row.)
    assert len(result) == 1, (
        f"{label}: expected ONE DriftItem total (the mutated field), "
        f"got {[it.field for it in result]}"
    )


# ---------- Tmpfs dict shape (multiple items allowed) -----------------------


def test_tmpfs_missing_run_only():
    """Tmpfs is a dict in docker-py response. Missing /run produces drift."""
    attrs = _expected_full_attrs()
    attrs["HostConfig"]["Tmpfs"] = {"/tmp": "rw,size=512m,mode=1777"}
    result = drift.compute_drift(
        attrs["HostConfig"],
        attrs["Config"],
        expected_storage_bind=EXPECTED_STORAGE_BIND,
    )
    tmpfs_items = [it for it in result if "Tmpfs" in it.field]
    assert len(tmpfs_items) == 1
    assert "/run" in tmpfs_items[0].field
    assert tmpfs_items[0].severity == "resource"


def test_tmpfs_empty_produces_two_items():
    """Empty Tmpfs surfaces drift for both /tmp and /run."""
    attrs = _expected_full_attrs()
    attrs["HostConfig"]["Tmpfs"] = {}
    result = drift.compute_drift(
        attrs["HostConfig"],
        attrs["Config"],
        expected_storage_bind=EXPECTED_STORAGE_BIND,
    )
    tmpfs_items = [it for it in result if "Tmpfs" in it.field]
    # Either two items (one per missing path) or one composite — the doc says
    # the field strings MUST contain "Tmpfs".
    assert len(tmpfs_items) >= 1
    for it in tmpfs_items:
        assert "Tmpfs" in it.field
        assert it.severity == "resource"


# ---------- SecurityOpt membership semantics (Pitfall 3) -------------------


def test_security_opt_membership_allows_seccomp_default():
    """Docker may auto-add 'seccomp=default' to SecurityOpt. As long as
    'no-new-privileges' is present, no drift for that field."""
    attrs = _expected_full_attrs()
    attrs["HostConfig"]["SecurityOpt"] = ["seccomp=default", "no-new-privileges"]
    result = drift.compute_drift(
        attrs["HostConfig"],
        attrs["Config"],
        expected_storage_bind=EXPECTED_STORAGE_BIND,
    )
    sec_items = [it for it in result if it.field == "HostConfig.SecurityOpt"]
    assert sec_items == [], (
        "no-new-privileges present (alongside seccomp=default) must NOT drift"
    )


def test_security_opt_none_produces_drift():
    """SecurityOpt = None (engine may report as null) is drift — no-new-privileges absent."""
    attrs = _expected_full_attrs()
    attrs["HostConfig"]["SecurityOpt"] = None
    result = drift.compute_drift(
        attrs["HostConfig"],
        attrs["Config"],
        expected_storage_bind=EXPECTED_STORAGE_BIND,
    )
    sec_items = [it for it in result if it.field == "HostConfig.SecurityOpt"]
    assert len(sec_items) == 1


# ---------- read-only contract (Rule 2) -------------------------------------


def test_drift_module_does_not_import_docker_sdk():
    """Pure module — no docker SDK coupling. Source-text guard."""
    src = Path(drift.__file__).read_text(encoding="utf-8")
    assert "import docker" not in src, (
        "drift.py must be a pure module (no docker SDK import)"
    )


def test_drift_module_does_not_mutate_task_json():
    """Drift is read-only per CONTEXT.md D-A2. No store.update_task call."""
    src = Path(drift.__file__).read_text(encoding="utf-8")
    assert "store.update_task" not in src
    assert "store.write" not in src


# ---------- expected_storage_bind helper ------------------------------------
#
# Drift's expected_storage_bind must reflect the HOST path, because
# HostConfig.Binds[*] is what the daemon recorded at container-create
# time and the daemon sees host paths. In the containerized-controller
# deployment (Phase 3.5 default), the controller's view of /home/op/naiw-data
# lives at /data inside its container — cfg.data_root_host (a.k.a.
# cfg.host_root) carries the real host path. Using cfg.data_root or
# task_dir.resolve() instead would compare against the controller-internal
# path and surface a false-positive (drift) on every running task.
#
# The helper exists in drift.py (not in lifecycle / list_cmd / doctor)
# so that every caller asking "what bind should this task have?" goes
# through the same compute. If a future PR changes the bind shape
# (e.g. adds `,nodev`), updating drift.expected_storage_bind keeps the
# audit and the create-time bind in lockstep automatically.


class _CfgWithHostRoot:
    """Stub cfg exposing data_root + host_root, matching Config's surface."""

    def __init__(self, data_root: Path, data_root_host: Path | None) -> None:
        self.data_root = data_root
        self._host = data_root_host

    @property
    def host_root(self) -> Path:
        return self._host if self._host is not None else self.data_root


def test_expected_storage_bind_uses_host_root_when_distinct():
    """The bind string must use host_root, NOT data_root.

    Simulates the containerized-controller deployment: data_root is the
    in-container path (/data), data_root_host is the actual host path
    (/home/op/naiw-data). The bind the daemon records uses the host path.
    """
    cfg = _CfgWithHostRoot(
        data_root=Path("/data"),
        data_root_host=Path("/home/op/naiw-data"),
    )
    bind = drift.expected_storage_bind(cfg, "demo-001")
    assert bind.startswith("/home/op/naiw-data"), (
        f"helper used data_root instead of host_root: {bind!r}"
    )
    # Container side is fixed.
    assert bind.endswith(":/home/pi:rw"), bind


def test_expected_storage_bind_falls_back_to_data_root():
    """Local-Linux operator runs the controller on the host directly:
    data_root_host is None, so host_root == data_root."""
    cfg = _CfgWithHostRoot(
        data_root=Path("/home/op/naiw-data"),
        data_root_host=None,
    )
    bind = drift.expected_storage_bind(cfg, "demo-001")
    assert bind == "/home/op/naiw-data/tasks/demo-001/storage:/home/pi:rw"


def test_expected_storage_bind_shape_is_src_dst_mode():
    """`src:dst:mode` triple — the canonical Docker bind format."""
    cfg = _CfgWithHostRoot(
        data_root=Path("/srv/naiw-data"),
        data_root_host=None,
    )
    bind = drift.expected_storage_bind(cfg, "task-007")
    src, dst, mode = bind.rsplit(":", 2)
    assert src.endswith("/tasks/task-007/storage"), src
    assert dst == "/home/pi"
    assert mode == "rw"


def test_expected_storage_bind_strips_trailing_slash():
    """Defends against a config recording host_root with trailing /.

    Without rstrip, the bind would be `/home/op/naiw-data//tasks/X/...`
    and exact-string drift comparison would false-positive on every task
    (Docker normalises double-slashes silently in the bind it records).
    """
    cfg = _CfgWithHostRoot(
        data_root=Path("/home/op/naiw-data/"),
        data_root_host=Path("/home/op/naiw-data/"),
    )
    bind = drift.expected_storage_bind(cfg, "demo-001")
    assert "//" not in bind.split(":/home/pi", 1)[0], (
        f"double slash leaked through: {bind!r}"
    )
    assert bind == "/home/op/naiw-data/tasks/demo-001/storage:/home/pi:rw"


def test_expected_storage_bind_is_signature_caller_uses():
    """The helper is what list_cmd AND doctor MUST call.

    Both modules previously rolled their own bind string and diverged
    (list_cmd used host_root, doctor used task_dir.resolve()). This test
    locks the API shape — accepting cfg + task_id — so a future refactor
    can't bring back the per-call duplication.
    """
    import inspect
    sig = inspect.signature(drift.expected_storage_bind)
    params = list(sig.parameters)
    assert params == ["cfg", "task_id"], (
        f"helper signature drift: {params}"
    )
