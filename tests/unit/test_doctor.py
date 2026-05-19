"""Tests for naiw_tasks.doctor — consolidated health-check umbrella.

Covers the two public entry points (run_global, run_per_task) and the four
exit codes (0/1/2/3).
"""

import sys

import pytest

if sys.platform != "linux":
    pytest.skip(
        "Linux-only (fcntl / O_NOFOLLOW)",
        allow_module_level=True,
    )

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

from naiw_tasks.config import Config
from naiw_tasks.model import SCHEMA_VERSION, Status  # noqa: F401

from naiw_tasks import doctor as doctor_mod
from naiw_tasks import drift as drift_mod  # noqa: F401

# ---------- helpers ----------------------------------------------------------


@dataclass(frozen=True)
class FakeDriftItem:
    """Lightweight stand-in for naiw_tasks.drift.DriftItem (decoupled from 06-01)."""

    field: str
    expected: object
    actual: object
    severity: str


def _seed_task(
    data_root: Path,
    task_id: str = "alpha-001",
    status: str = "running",
) -> Path:
    """Create a tasks/<id>/ skeleton with a valid task.json."""
    task_dir = data_root / "tasks" / task_id
    meta = task_dir / "meta"
    meta.mkdir(parents=True)
    (task_dir / "storage").mkdir()
    payload = {
        "id": task_id,
        "kind": "project",
        "container_name": f"naiw-task-{task_id}",
        "image_tag": "ghcr.io/x/naiw-task-image:latest",
        "created_at": "2026-05-19T00:00:00.000Z",
        "updated_at": "2026-05-19T00:00:00.000Z",
        "status": status,
        "schema_version": SCHEMA_VERSION,
    }
    (meta / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    return task_dir


def _running_container(task_id: str = "alpha-001") -> MagicMock:
    ctr = MagicMock()
    ctr.labels = {"naiw.managed": "1", "naiw.task-id": task_id}
    ctr.attrs = {
        "State": {"Status": "running"},
        "HostConfig": {},
        "Config": {},
    }
    return ctr


# ---------- run_global -------------------------------------------------------


def test_run_global_clean(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)

    _seed_task(data_root)
    client = MagicMock()
    client.containers.list.return_value = [_running_container()]

    monkeypatch.setattr(
        doctor_mod.startup_checks, "run_all", lambda c, k, **kw: None
    )
    monkeypatch.setattr(
        doctor_mod.disk_mod, "threshold", lambda c: (10, 100, 70.0)
    )
    monkeypatch.setattr(drift_mod, "compute_drift", lambda *a, **kw: [])

    rc = doctor_mod.run_global(cfg, client)
    out = capsys.readouterr().out
    assert rc == 0
    # Sections appear in fixed order
    assert out.index("[startup_checks]") < out.index("[disk]") < out.index("[drift]")
    assert "no drift" in out


def test_run_global_startup_fails(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    client = MagicMock()

    def boom(c, k, **kw):
        raise doctor_mod.startup_checks.StartupCheckFailed("simulated")

    monkeypatch.setattr(doctor_mod.startup_checks, "run_all", boom)
    # threshold/drift MUST NOT be reached
    monkeypatch.setattr(
        doctor_mod.disk_mod,
        "threshold",
        MagicMock(side_effect=AssertionError("disk must not be called")),
    )
    monkeypatch.setattr(
        drift_mod,
        "compute_drift",
        MagicMock(side_effect=AssertionError("drift must not be called")),
    )

    rc = doctor_mod.run_global(cfg, client)
    out = capsys.readouterr().out
    assert rc == 2
    assert "[startup_checks]" in out
    assert "[disk]" not in out
    assert "[drift]" not in out


def test_run_global_disk_warn(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    _seed_task(data_root)
    client = MagicMock()
    client.containers.list.return_value = [_running_container()]

    monkeypatch.setattr(
        doctor_mod.startup_checks, "run_all", lambda c, k, **kw: None
    )
    monkeypatch.setattr(
        doctor_mod.disk_mod, "threshold", lambda c: (85, 100, 85.0)
    )
    monkeypatch.setattr(drift_mod, "compute_drift", lambda *a, **kw: [])

    rc = doctor_mod.run_global(cfg, client)
    out = capsys.readouterr().out
    assert rc == 1
    assert "85.0%" in out


def test_run_global_drift_detected(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    _seed_task(data_root, "alpha-001")
    client = MagicMock()
    client.containers.list.return_value = [_running_container("alpha-001")]

    monkeypatch.setattr(
        doctor_mod.startup_checks, "run_all", lambda c, k, **kw: None
    )
    monkeypatch.setattr(
        doctor_mod.disk_mod, "threshold", lambda c: (10, 100, 70.0)
    )
    monkeypatch.setattr(
        drift_mod,
        "compute_drift",
        lambda *a, **kw: [
            FakeDriftItem("PidsLimit", 512, 0, "resource"),
        ],
    )

    rc = doctor_mod.run_global(cfg, client)
    out = capsys.readouterr().out
    assert rc == 1
    assert "alpha-001" in out
    assert "PidsLimit" in out


def test_run_global_drift_and_disk_warn(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    _seed_task(data_root, "alpha-001")
    client = MagicMock()
    client.containers.list.return_value = [_running_container("alpha-001")]

    monkeypatch.setattr(
        doctor_mod.startup_checks, "run_all", lambda c, k, **kw: None
    )
    monkeypatch.setattr(
        doctor_mod.disk_mod, "threshold", lambda c: (90, 100, 90.0)
    )
    monkeypatch.setattr(
        drift_mod,
        "compute_drift",
        lambda *a, **kw: [FakeDriftItem("ReadonlyRootfs", True, False, "security")],
    )

    rc = doctor_mod.run_global(cfg, client)
    # Single non-zero — do not need to distinguish disk-warn vs drift.
    assert rc == 1


# ---------- run_per_task -----------------------------------------------------


def test_run_per_task_clean(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    _seed_task(data_root, "alpha-001")
    client = MagicMock()
    client.containers.get.return_value = _running_container("alpha-001")

    monkeypatch.setattr(drift_mod, "compute_drift", lambda *a, **kw: [])

    rc = doctor_mod.run_per_task(cfg, client, "alpha-001")
    out = capsys.readouterr().out
    assert rc == 0
    assert "no drift" in out


def test_run_per_task_drift(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    _seed_task(data_root, "alpha-001")
    client = MagicMock()
    client.containers.get.return_value = _running_container("alpha-001")

    monkeypatch.setattr(
        drift_mod,
        "compute_drift",
        lambda *a, **kw: [FakeDriftItem("PidsLimit", 512, 0, "resource")],
    )

    rc = doctor_mod.run_per_task(cfg, client, "alpha-001")
    out = capsys.readouterr().out
    assert rc == 1
    assert "PidsLimit" in out
    assert "alpha-001" in out


def test_run_per_task_invalid_id(tmp_path, capsys):
    cfg = Config(data_root=tmp_path / "naiw-data")
    client = MagicMock()

    rc = doctor_mod.run_per_task(cfg, client, "UPPER")
    err = capsys.readouterr().err
    assert rc == 3
    assert "invalid" in err.lower()


def test_run_per_task_unknown_id(tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    client = MagicMock()

    rc = doctor_mod.run_per_task(cfg, client, "ghost-001")
    err = capsys.readouterr().err
    assert rc == 3
    assert "not found" in err.lower()


def test_run_per_task_container_missing(monkeypatch, tmp_path, capsys):
    data_root = tmp_path / "naiw-data"
    (data_root / "tasks").mkdir(parents=True)
    cfg = Config(data_root=data_root)
    _seed_task(data_root, "alpha-001")

    import docker.errors

    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")

    rc = doctor_mod.run_per_task(cfg, client, "alpha-001")
    err = capsys.readouterr().err
    assert rc == 1
    assert "alpha-001" in err


