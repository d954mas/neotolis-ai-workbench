"""Tests for lifecycle.recover and recover-race serialization."""

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock

import docker.errors
import pytest

from naiw_tasks import lifecycle, store
from naiw_tasks.config import Config
from naiw_tasks.model import Status


def _seed_interrupted_task(
    tmp_path: Path,
    tid: str = "t-001",
    kind: str = "generic",
    with_git: bool = False,
    with_merge_marker: bool = False,
    terminal_log: bytes = b"prior content\n",
    with_storage: bool = True,
) -> tuple[Config, Path]:
    """Seed a tmp NAIW_DATA skeleton with a single interrupted task.

    `with_storage=False` simulates a pre-Phase-5 legacy task — task.json
    exists but the storage/ subdir was never created (it was added later
    as the /home/pi bind-mount source).
    """
    (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pi-packages").mkdir(exist_ok=True)
    cfg = Config(data_root=tmp_path)
    td = tmp_path / "tasks" / tid
    (td / "meta").mkdir(parents=True)
    (td / "io").mkdir()
    (td / "io").chmod(0o1777)
    (td / "io" / "terminal.log").write_bytes(terminal_log)
    if with_storage:
        (td / "storage").mkdir()
        (td / "storage").chmod(0o1777)
    if kind == "project":
        work = td / "work"
        work.mkdir()
        if with_git:
            (work / ".git").mkdir()
            if with_merge_marker:
                (work / ".git" / "MERGE_HEAD").write_text("abc\n")
    else:
        (td / "work").mkdir()
        (td / "work").chmod(0o1777)
    data = {
        "schema_version": 1,
        "id": tid,
        "status": "interrupted",
        "kind": kind,
        "container_name": f"naiw-task-{tid}",
        "image_tag": "naiw-task-image:latest",
        "image_digest": "sha256:old",
        "recovery_count": 0,
        "recovery_history": [],
        "events_offset": 0,
        "terminal_log_max_size": 0,
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
        "secrets": [],
    }
    if kind == "project":
        data["worktree_path"] = str(td / "work")
        data["project_repo_path"] = str(tmp_path / "repo")
        data["project"] = "myproj"
    (td / "meta" / "task.json").write_text(json.dumps(data))
    return cfg, td


def _fake_client(new_image_digest: str = "sha256:new"):
    """Build a MagicMock Docker client that mirrors real semantics:

    - No old container present (NotFound).
    - containers.run returns a container with attrs.Image populated.
    """
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("absent")
    new_ctr = MagicMock()
    new_ctr.id = "newctr"
    new_ctr.attrs = {
        "Image": new_image_digest,
        "State": {"ExitCode": None},
    }
    client.containers.run.return_value = new_ctr
    return client, new_ctr


# ---------------------------------------------------------------------------
# Status gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "created",
        "running",
        "waiting_for_user",
        "completed",
        "failed",
        "cancelled",
    ],
)
def test_recover_refuses_non_interrupted_status(tmp_path, status):
    cfg, td = _seed_interrupted_task(tmp_path)
    meta = td / "meta" / "task.json"
    data = json.loads(meta.read_text())
    data["status"] = status
    meta.write_text(json.dumps(data))
    client, _ = _fake_client()
    with pytest.raises(lifecycle.RecoverNotInterrupted) as exc:
        lifecycle.recover(cfg, client, "t-001")
    assert status in str(exc.value)
    assert client.containers.run.call_count == 0


# ---------------------------------------------------------------------------
# Banner shape and git-state surfacing
# ---------------------------------------------------------------------------


def test_recovery_banner_appended_with_canonical_shape(tmp_path):
    cfg, td = _seed_interrupted_task(tmp_path)
    client, _ = _fake_client()
    lifecycle.recover(cfg, client, "t-001")
    log = (td / "io" / "terminal.log").read_bytes()
    assert log.startswith(b"prior content\n"), "prior content was clobbered"
    text = log.decode("utf-8")
    assert re.search(
        r"\n===== RECOVERED #1 AT "
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z =====\n$",
        text,
    ), f"banner shape wrong: {text!r}"


@pytest.mark.parametrize(
    "marker",
    [
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        None,
    ],
)
def test_recovery_banner_surfaces_git_state(tmp_path, marker):
    cfg, td = _seed_interrupted_task(
        tmp_path, kind="project", with_git=True,
    )
    if marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        (td / "work" / ".git" / marker).write_text("ref\n")
    elif marker in ("rebase-merge", "rebase-apply"):
        (td / "work" / ".git" / marker).mkdir()
    client, _ = _fake_client()
    lifecycle.recover(cfg, client, "t-001")
    text = (td / "io" / "terminal.log").read_text()
    if marker is None:
        assert "GIT STATE:" not in text
    else:
        assert "GIT STATE:" in text
        assert marker in text


# ---------------------------------------------------------------------------
# Never auto-reset worktree
# ---------------------------------------------------------------------------


def test_recover_never_auto_resets_worktree(tmp_path, monkeypatch):
    cfg, td = _seed_interrupted_task(
        tmp_path,
        kind="project",
        with_git=True,
        with_merge_marker=True,
    )
    client, _ = _fake_client()
    forbidden_calls: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(args, *a, **kw):
        # Capture any git destructive call.
        if isinstance(args, (list, tuple)) and args and "git" in str(args[0]):
            args_list = list(args)
            if any(
                tok in args_list
                for tok in (
                    "reset", "--abort", "merge", "rebase",
                    "cherry-pick", "revert",
                )
            ):
                forbidden_calls.append(args_list)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy_run)
    lifecycle.recover(cfg, client, "t-001")
    # MERGE_HEAD still on disk after recover.
    assert (td / "work" / ".git" / "MERGE_HEAD").exists()
    # No git reset/abort calls were made.
    for call in forbidden_calls:
        if "reset" in call or "--abort" in call:
            pytest.fail(f"controller auto-reset: {call}")


# ---------------------------------------------------------------------------
# Container reuse: same name, labels, mounts
# ---------------------------------------------------------------------------


def test_recover_reuses_labels_and_mounts(tmp_path):
    cfg, td = _seed_interrupted_task(tmp_path, kind="project")
    client, _ = _fake_client()
    lifecycle.recover(cfg, client, "t-001")
    run_kwargs = client.containers.run.call_args.kwargs
    assert run_kwargs["name"] == "naiw-task-t-001"
    labels = run_kwargs["labels"]
    assert labels.get("naiw.managed") == "1"
    assert labels.get("naiw.task-id") == "t-001"
    assert labels.get("naiw.role") == "task-container"
    assert labels.get("naiw.project") == "myproj"
    volumes = run_kwargs["volumes"]
    bind_paths = {v["bind"] for v in volumes.values()}
    assert "/home/pi" in bind_paths, "storage bind missing"
    assert "/work" in bind_paths
    assert "/io" in bind_paths
    assert "/pi-packages" in bind_paths


# ---------------------------------------------------------------------------
# terminal.log monotonic growth (append-only invariant)
# ---------------------------------------------------------------------------


def test_recover_does_not_truncate_terminal_log(tmp_path):
    big = b"x" * 4096
    cfg, td = _seed_interrupted_task(tmp_path, terminal_log=big)
    client, _ = _fake_client()
    lifecycle.recover(cfg, client, "t-001")
    size = (td / "io" / "terminal.log").stat().st_size
    assert size >= 4096, f"terminal.log shrunk: {size} < 4096"


# ---------------------------------------------------------------------------
# Legacy task recovery — storage/ created later as a Phase 5 bind-mount source.
# Tasks that became interrupted before Phase 5 landed have no storage/ on
# disk; recover must idempotently create it instead of failing the bind
# validation.
# ---------------------------------------------------------------------------


def test_recover_creates_missing_storage_for_legacy_task(tmp_path):
    cfg, td = _seed_interrupted_task(tmp_path, with_storage=False)
    assert not (td / "storage").exists(), "precondition: no storage/"
    client, _ = _fake_client()
    lifecycle.recover(cfg, client, "t-001")
    storage = td / "storage"
    assert storage.is_dir(), "recover must create storage/ for legacy tasks"
    # 1777 sticky-writable so pi uid 1000 can write under /home/pi even when
    # the operator's host uid differs.
    mode = storage.stat().st_mode & 0o7777
    assert mode == 0o1777, f"storage mode: expected 0o1777, got {oct(mode)}"


# ---------------------------------------------------------------------------
# recovery_count does not stick on early failure (regression: prior design
# bumped recovery_count BEFORE docker run; any docker/fs error in steps 4-7
# left task.json with a higher count but no matching history entry).
# ---------------------------------------------------------------------------


def test_recover_failure_at_docker_run_does_not_bump_recovery_count(tmp_path):
    cfg, td = _seed_interrupted_task(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("absent")
    # docker run fails — simulates "image not found", "name in use", etc.
    client.containers.run.side_effect = docker.errors.APIError(
        "Conflict. The container name '/naiw-task-t-001' is already in use"
    )
    with pytest.raises(lifecycle.RecoverFailed):
        lifecycle.recover(cfg, client, "t-001")
    # task.json untouched: status still interrupted, recovery_count still 0,
    # history empty. Banner may or may not have landed (best-effort, host-side).
    data = json.loads((td / "meta" / "task.json").read_text())
    assert data["status"] == "interrupted", (
        f"status must stay interrupted on docker-run failure; "
        f"got {data['status']!r}"
    )
    assert data["recovery_count"] == 0, (
        f"recovery_count must not bump on early failure; "
        f"got {data['recovery_count']!r}"
    )
    assert data["recovery_history"] == [], (
        f"recovery_history must stay empty on early failure; "
        f"got {data['recovery_history']!r}"
    )


def test_recover_bumps_count_inside_final_mutator_not_from_closure(tmp_path):
    """Concurrent-recover regression: even though banner_count is computed
    from `initial` outside the flock window, the persisted recovery_count
    must come from the value read inside the mutator. We simulate this by
    pre-bumping task.json AFTER the initial read but BEFORE update_task.
    """
    cfg, td = _seed_interrupted_task(tmp_path)
    # Pre-set count to 5 — simulates a previous successful recover that
    # landed between this recover's initial read and its final mutator.
    meta = td / "meta" / "task.json"
    data = json.loads(meta.read_text())
    data["recovery_count"] = 5
    meta.write_text(json.dumps(data))
    client, _ = _fake_client()
    lifecycle.recover(cfg, client, "t-001")
    final = json.loads(meta.read_text())
    # count must be 5 + 1, not (initial=0) + 1.
    assert final["recovery_count"] == 6, (
        f"recovery_count must bump from value-in-d, not stale closure: "
        f"expected 6, got {final['recovery_count']!r}"
    )
    # history entry must match.
    assert len(final["recovery_history"]) == 1
    assert final["recovery_history"][0]["recovery_count"] == 6


def test_recover_kills_new_container_if_concurrent_finish_wins(
        tmp_path, monkeypatch,
):
    """If a concurrent finish flips status between our pre-flock read and
    the final mutator, the new container we just started is a zombie —
    live but with no task.json bookkeeping pointing at it. Recover MUST
    stop+remove it before re-raising RecoverNotInterrupted.
    """
    cfg, td = _seed_interrupted_task(tmp_path)
    client, new_ctr = _fake_client()

    # Simulate the race: real store.update_task reads d, but we monkey-patch
    # the read step inside update_task to flip status to 'cancelled' after
    # the initial read in recover() but before the final mutator. Simplest:
    # patch store.update_task so its first invocation sees status=cancelled.
    from naiw_tasks import store as store_mod

    real_update = store_mod.update_task
    call_count = {"n": 0}

    def racing_update(td_arg, mutator):
        call_count["n"] += 1
        # Flip to cancelled on disk right before the real update runs its
        # read-modify-write under flock. The mutator will then see status
        # != INTERRUPTED and raise.
        meta = td_arg / "meta" / "task.json"
        data = json.loads(meta.read_text())
        data["status"] = "cancelled"
        meta.write_text(json.dumps(data))
        return real_update(td_arg, mutator)

    monkeypatch.setattr(store_mod, "update_task", racing_update)
    monkeypatch.setattr(lifecycle.store, "update_task", racing_update)

    with pytest.raises(lifecycle.RecoverNotInterrupted):
        lifecycle.recover(cfg, client, "t-001")

    # Container was created via containers.run …
    assert client.containers.run.call_count == 1, "new container started"
    # …but then torn down before the exception propagated, so no zombie.
    new_ctr.stop.assert_called(), "new container must be stopped"
    new_ctr.remove.assert_called_with(force=True), "new container must be removed"


# ---------------------------------------------------------------------------
# Concurrent recover + finish flock race
# ---------------------------------------------------------------------------


def test_recover_finish_race_does_not_corrupt_task_json(
        tmp_path, monkeypatch,
):
    """Concurrent recover + finish must serialise via flock and leave task.json:
      (a) structurally valid JSON,
      (b) status in {RUNNING, CANCELLED, COMPLETED} - NEVER INTERRUPTED,
          NEVER a half-merged blob,
      (c) every required Task dataclass key present.

    Determinism: force an interleave by making Event.now_iso() rendez-vous on
    a threading.Barrier inside the recover mutator window. This guarantees
    finish() lands inside recover()'s read-then-mutate region (otherwise
    scheduling on a fast box can serialise them so neatly that the race
    never actually overlaps).
    """
    cfg, td = _seed_interrupted_task(tmp_path)
    client, _ = _fake_client()
    errors: list[Exception] = []

    # Two-party barrier - the first now_iso() call from each thread waits;
    # subsequent calls pass through normally so we don't deadlock the rest
    # of the function.
    barrier = threading.Barrier(parties=2, timeout=10)
    from naiw_common import events as _events

    real_now_iso = _events.Event.now_iso
    toggle = {"hit": 0}
    lock = threading.Lock()

    def rendezvous_now_iso() -> str:
        with lock:
            toggle["hit"] += 1
            n = toggle["hit"]
        if n <= 2:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real_now_iso()

    monkeypatch.setattr(
        _events.Event, "now_iso", staticmethod(rendezvous_now_iso),
    )

    def do_recover():
        try:
            lifecycle.recover(cfg, client, "t-001")
        except (
            lifecycle.RecoverNotInterrupted,
            lifecycle.RecoverFailed,
        ) as exc:
            errors.append(exc)

    def do_finish():
        # Simulate finish flipping status directly under the same flock.
        try:
            store.update_task(td, lambda d: {
                **d,
                "status": "cancelled",
                "finished_at": real_now_iso(),
                "updated_at": real_now_iso(),
            })
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=do_recover),
        threading.Thread(target=do_finish),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "race thread hung past barrier timeout"

    # (a) task.json is valid JSON.
    raw = (td / "meta" / "task.json").read_text(encoding="utf-8")
    data = json.loads(raw)

    # (b) Tight assertion: status is one of the canonical post-race values.
    # Specifically NEVER 'interrupted' (must have been bumped by one of the
    # two paths) and NEVER a missing/non-string blob.
    assert isinstance(data.get("status"), str), \
        f"status must be a string, got {data.get('status')!r}"
    allowed_terminal = {
        str(Status.RUNNING),
        str(Status.CANCELLED),
        str(Status.COMPLETED),
    }
    assert data["status"] in allowed_terminal, (
        f"post-race status must be in {allowed_terminal!r}; "
        f"got {data['status']!r}. Half-merged JSON indicates lost flock."
    )
    assert data["status"] != str(Status.INTERRUPTED), \
        "status must not still be 'interrupted' after either path wins"

    # (c) Schema sanity - required Task dataclass keys preserved.
    for required_key in ("id", "kind", "container_name", "schema_version"):
        assert required_key in data, f"required key missing: {required_key}"
