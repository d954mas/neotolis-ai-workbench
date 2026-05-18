"""Tests for clean.py (`naiw-tasks clean` subcommand + orphan prune)."""

import datetime as dt
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from naiw_tasks import clean
from naiw_tasks.config import Config


def _write_task(
    tasks_dir: Path,
    tid: str,
    status: str,
    finished_at: str | None,
    updated_at: str,
    kind: str = "generic",
    project_repo_path: str | None = None,
    worktree_path: str | None = None,
) -> Path:
    td = tasks_dir / tid
    (td / "meta").mkdir(parents=True)
    data = {
        "schema_version": 1,
        "id": tid,
        "status": status,
        "kind": kind,
        "updated_at": updated_at,
        "container_name": f"naiw-task-{tid}",
        "image_tag": "naiw-task-image:latest",
        "created_at": updated_at,
    }
    if finished_at:
        data["finished_at"] = finished_at
    if project_repo_path:
        data["project_repo_path"] = project_repo_path
    if worktree_path:
        data["worktree_path"] = worktree_path
    (td / "meta" / "task.json").write_text(json.dumps(data))
    return td


def test_parse_older_than_accepts_smhdw_units():
    assert clean.parse_older_than("30d") == dt.timedelta(days=30)
    assert clean.parse_older_than("2w") == dt.timedelta(weeks=2)
    assert clean.parse_older_than("1h") == dt.timedelta(hours=1)
    assert clean.parse_older_than("30m") == dt.timedelta(minutes=30)
    assert clean.parse_older_than("5s") == dt.timedelta(seconds=5)
    assert clean.parse_older_than("  30d  ") == dt.timedelta(days=30)
    with pytest.raises(ValueError) as exc:
        clean.parse_older_than("1d12h")
    assert "s,m,h,d,w" in str(exc.value)
    with pytest.raises(ValueError):
        clean.parse_older_than("30")


def test_clean_filters_to_terminal_states(tmp_path, monkeypatch):
    monkeypatch.setenv("NAIW_DATA", str(tmp_path))
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_ts = "2020-01-01T00:00:00.000+00:00"
    for status in (
        "created", "running", "interrupted",
        "waiting_for_user", "completed", "failed", "cancelled",
    ):
        _write_task(
            tasks_dir, f"t-{status}", status,
            finished_at=old_ts, updated_at=old_ts,
        )
    cfg = Config(data_root=tmp_path)
    candidates = clean._enumerate_candidates(cfg, dt.timedelta(seconds=1))
    ids = sorted(c.task_id for c in candidates)
    assert ids == ["t-cancelled", "t-completed", "t-failed"]


def test_clean_prunes_worktree_before_rmtree(tmp_path, monkeypatch):
    old_ts = "2020-01-01T00:00:00.000+00:00"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_task(
        tasks_dir, "t-proj", status="completed",
        finished_at=old_ts, updated_at=old_ts,
        kind="project",
        project_repo_path=str(repo),
        worktree_path=str(tasks_dir / "t-proj" / "work"),
    )
    call_order: list[str] = []
    monkeypatch.setattr(
        clean.git_ops, "worktree_prune",
        lambda p: call_order.append(f"worktree_prune({p})"),
    )
    real_rmtree = shutil.rmtree

    def spy_rmtree(p, *a, **kw):
        call_order.append(f"rmtree({p})")
        return real_rmtree(p, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", spy_rmtree)
    client = MagicMock()
    client.containers.list.return_value = []
    cfg = Config(data_root=tmp_path)
    rc = clean.run(cfg, client, dt.timedelta(seconds=1), skip_prompt=True)
    assert rc == 0
    worktree_idx = next(
        i for i, c in enumerate(call_order) if c.startswith("worktree_prune")
    )
    rmtree_idx = next(
        i for i, c in enumerate(call_order) if c.startswith("rmtree")
    )
    assert worktree_idx < rmtree_idx, "worktree_prune must precede rmtree"


def test_clean_prunes_orphan_containers(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    # Existing task folder for one container; the other is orphan.
    (tasks_dir / "t-have").mkdir()
    client = MagicMock()
    live = MagicMock()
    live.name = "naiw-task-t-have"
    live.attrs = {"Config": {"Labels": {
        "naiw.managed": "1", "naiw.task-id": "t-have",
    }}}
    orphan = MagicMock()
    orphan.name = "naiw-task-t-gone"
    orphan.attrs = {"Config": {"Labels": {
        "naiw.managed": "1", "naiw.task-id": "t-gone",
    }}}
    client.containers.list.return_value = [live, orphan]
    cfg = Config(data_root=tmp_path)
    rc = clean.run(cfg, client, dt.timedelta(days=365), skip_prompt=True)
    assert rc == 0
    # Only the orphan was removed.
    live.remove.assert_not_called()
    orphan.remove.assert_called_once_with(force=True)


def test_dry_run_does_not_remove_orphans(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    client = MagicMock()
    orphan = MagicMock()
    orphan.name = "naiw-task-t-gone"
    orphan.attrs = {"Config": {"Labels": {
        "naiw.managed": "1", "naiw.task-id": "t-gone",
    }}}
    client.containers.list.return_value = [orphan]
    cfg = Config(data_root=tmp_path)
    rc = clean.run(cfg, client, dt.timedelta(days=365), dry_run=True)
    assert rc == 0
    orphan.remove.assert_not_called()


def test_clean_runs_disk_side_with_no_docker_client(tmp_path, monkeypatch, capsys):
    """clean must keep working when Docker is unreachable so the operator can
    reclaim space before fixing the daemon. CLI passes client=None on
    StartupCheckFailed; clean.run handles it by skipping the orphan scan and
    printing a clear notice. Disk-side per-task removal still runs.
    """
    old_ts = "2020-01-01T00:00:00.000+00:00"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    td = _write_task(
        tasks_dir, "t-001", "completed",
        finished_at=old_ts, updated_at=old_ts,
    )
    cfg = Config(data_root=tmp_path)
    rc = clean.run(
        cfg, client=None, older_than=dt.timedelta(seconds=1), skip_prompt=True,
    )
    assert rc == 0
    assert not td.exists(), "disk-side removal must still run with no client"


def test_dry_run_with_no_docker_client_notes_skip(tmp_path, capsys):
    """With no Docker client AND tasks to clean, dry-run prints the
    orphan-scan-skipped notice so the operator knows orphans were not checked.
    """
    old_ts = "2020-01-01T00:00:00.000+00:00"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write_task(
        tasks_dir, "t-001", "completed",
        finished_at=old_ts, updated_at=old_ts,
    )
    cfg = Config(data_root=tmp_path)
    rc = clean.run(
        cfg, client=None, older_than=dt.timedelta(seconds=1), dry_run=True,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan-container scan skipped" in out, (
        f"dry-run with no client must say it skipped orphans; got: {out!r}"
    )


def test_clean_no_candidates_no_client_notes_skip(tmp_path, capsys):
    """The empty-case (no candidates, no Docker) also prints the notice so
    the operator does not get a misleading 'no tasks to clean' that hides
    the fact that orphans weren't checked at all.
    """
    (tmp_path / "tasks").mkdir()
    cfg = Config(data_root=tmp_path)
    rc = clean.run(
        cfg, client=None, older_than=dt.timedelta(days=365),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan-container scan skipped" in out, (
        f"empty-case with no client must say it skipped orphans; got: {out!r}"
    )


def test_yes_skips_prompt(tmp_path, monkeypatch):
    confirm_calls: list = []
    monkeypatch.setattr(
        clean.click, "confirm",
        lambda *a, **kw: confirm_calls.append((a, kw)) or True,
    )
    old_ts = "2020-01-01T00:00:00.000+00:00"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write_task(
        tasks_dir, "t-001", "completed",
        finished_at=old_ts, updated_at=old_ts,
    )
    client = MagicMock()
    client.containers.list.return_value = []
    cfg = Config(data_root=tmp_path)
    rc = clean.run(cfg, client, dt.timedelta(seconds=1), skip_prompt=True)
    assert rc == 0
    assert confirm_calls == [], (
        "click.confirm must not be invoked with skip_prompt=True"
    )


def test_clean_uses_finished_at_then_falls_back_to_updated_at(tmp_path):
    old_ts_finished = "2020-01-01T00:00:00.000+00:00"
    old_ts_updated = "2020-02-01T00:00:00.000+00:00"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    # Has finished_at; use it.
    _write_task(
        tasks_dir, "t-with-finished", "completed",
        finished_at=old_ts_finished,
        updated_at="2026-05-18T00:00:00.000+00:00",
    )
    # No finished_at -- must fall back to updated_at.
    _write_task(
        tasks_dir, "t-no-finished", "failed",
        finished_at=None, updated_at=old_ts_updated,
    )
    cfg = Config(data_root=tmp_path)
    candidates = clean._enumerate_candidates(cfg, dt.timedelta(seconds=1))
    ids = {c.task_id: c.reference_ts for c in candidates}
    assert ids == {
        "t-with-finished": old_ts_finished,
        "t-no-finished": old_ts_updated,
    }


def test_clean_handles_per_task_rmtree_failure_continues(tmp_path, monkeypatch):
    old_ts = "2020-01-01T00:00:00.000+00:00"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write_task(
        tasks_dir, "t-a-fail", "completed",
        finished_at=old_ts, updated_at=old_ts,
    )
    _write_task(
        tasks_dir, "t-b-ok", "completed",
        finished_at=old_ts, updated_at=old_ts,
    )
    real_rmtree = shutil.rmtree
    rmtree_calls: list[str] = []

    def fake_rmtree(p, *a, **kw):
        rmtree_calls.append(str(p))
        if "t-a-fail" in str(p):
            raise PermissionError(13, "Permission denied", str(p))
        return real_rmtree(p, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", fake_rmtree)
    client = MagicMock()
    client.containers.list.return_value = []
    cfg = Config(data_root=tmp_path)
    rc = clean.run(cfg, client, dt.timedelta(seconds=1), skip_prompt=True)
    assert rc == 1, "at least 1 failure must surface as exit 1"
    # Both tasks were attempted (no early bail-out).
    assert any("t-a-fail" in c for c in rmtree_calls)
    assert any("t-b-ok" in c for c in rmtree_calls)
    # t-b-ok was actually removed.
    assert not (tasks_dir / "t-b-ok").exists()
