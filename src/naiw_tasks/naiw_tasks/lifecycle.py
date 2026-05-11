"""Orchestrates start (project + generic) and finish (permissive matrix).

All Docker, git, and filesystem ops go through their dedicated modules — this
file is sequencing and the failure-rollback policy.

Skeleton kind-awareness is the load-bearing invariant: generic tasks get
meta/, work/, io/ created by the controller; project tasks get meta/ + io/
only — `git worktree add` creates work/ and requires the path to not exist.
Keeping the two shapes distinct here eliminates any rmdir-before-worktree-add
TOCTOU window. The module never raw-deletes a worktree path; teardown is
git-driven via git_ops.worktree_remove.

On post-skeleton failure: write task.json.status='failed' with a short
failure_reason, leave the worktree + container artifacts in place, print the
cause and a cleanup hint to stderr, then raise StartFailed. The operator
reclaims disk via `naiw-tasks finish <id> --delete-worktree`.
"""

import logging
import logging.handlers
import sys
from dataclasses import replace
from pathlib import Path

import docker.errors
from naiw_common.events import Event

from naiw_tasks import git_ops, projects, store
from naiw_tasks.config import Config
from naiw_tasks.docker_client import HARDENED_HOST_CONFIG_KWARGS
from naiw_tasks.ids import allocate_task_id, validate_task_id
from naiw_tasks.model import (
    FinishPolicy,
    Status,
    Task,
    TaskKind,
)
from naiw_tasks.path_validation import (
    BindMountEscapeError,
    validate_bind_source,
)

GENERIC_PROJECT_ALIAS = "task"


class StartFailed(RuntimeError):
    """Surface for any post-skeleton failure during start.

    Carries the short reason for the operator-visible stderr line; the full
    failure cause has already been written to task.json.failure_reason.
    """


def _container_name(task_id: str) -> str:
    return f"naiw-task-{task_id}"


def _make_skeleton(data_root: Path, task_id: str, kind: TaskKind) -> Path:
    """Create the per-task directory skeleton sized to the task kind.

    Generic tasks get meta/, work/, io/ — controller owns the empty work/.
    Project tasks get meta/, io/ only — git worktree add creates work/ later
    (and it requires the path to not already exist).
    """
    task_dir = data_root / "tasks" / task_id
    (task_dir / "meta").mkdir(parents=True, exist_ok=True)
    (task_dir / "io" / ".naiw").mkdir(parents=True, exist_ok=True)
    if kind is TaskKind.GENERIC:
        (task_dir / "work").mkdir(exist_ok=True)
    return task_dir


def _attach_controller_log(task_dir: Path) -> logging.Handler:
    """Attach a per-task RotatingFileHandler. Detached on lifecycle exit."""
    log_path = task_dir / "meta" / "controller.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_048_576, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger = logging.getLogger("naiw_tasks")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def _detach_controller_log(handler: logging.Handler) -> None:
    logging.getLogger("naiw_tasks").removeHandler(handler)
    handler.close()


def _build_volumes(
    data_root: Path,
    task_dir: Path,
    secrets: list[str],
) -> dict[str, dict[str, str]]:
    """Resolve every bind source under data_root before docker is touched."""
    volumes: dict[str, dict[str, str]] = {}
    work_src = validate_bind_source(task_dir / "work", data_root)
    io_src = validate_bind_source(task_dir / "io", data_root)
    pi_pkgs_src = validate_bind_source(data_root / "pi-packages", data_root)
    volumes[str(work_src)] = {"bind": "/work", "mode": "rw"}
    volumes[str(io_src)] = {"bind": "/io", "mode": "rw"}
    volumes[str(pi_pkgs_src)] = {"bind": "/pi-packages", "mode": "ro"}
    for name in secrets:
        secret_src = validate_bind_source(
            data_root / "secrets" / name, data_root
        )
        volumes[str(secret_src)] = {
            "bind": f"/run/secrets/{name}",
            "mode": "ro",
        }
    return volumes


def _build_labels(task_id: str, project: str | None) -> dict[str, str]:
    labels = {
        "naiw.managed": "1",
        "naiw.task-id": task_id,
        "naiw.role": "task-container",
    }
    if project is not None:
        labels["naiw.project"] = project
    return labels


def _initial_task(
    task_id: str,
    kind: TaskKind,
    project: str | None,
    branch: str | None,
    worktree_path: str | None,
    base_branch: str | None,
    base_commit: str | None,
    finish_policy: FinishPolicy,
    secrets: list[str],
    cfg: Config,
    labels: dict[str, str],
) -> Task:
    ts = Event.now_iso()
    return Task(
        id=task_id,
        kind=kind,
        container_name=_container_name(task_id),
        image_tag=cfg.task_image,
        created_at=ts,
        updated_at=ts,
        status=Status.CREATED,
        project=project,
        branch=branch,
        worktree_path=worktree_path,
        base_branch=base_branch,
        base_commit=base_commit,
        finish_policy=finish_policy,
        secrets=list(secrets),
        labels=dict(labels),
    )


def start(
    cfg: Config,
    client,
    project: str | None,
    base_ref: str | None,
    finish_policy: str = "ask",
    secrets: list[str] | None = None,
) -> Task:
    """Start a project task (project!=None) or generic task. Returns the Task."""
    secrets = list(secrets or [])
    policy = FinishPolicy(finish_policy)
    kind = TaskKind.PROJECT if project else TaskKind.GENERIC
    counter_alias = project if project else GENERIC_PROJECT_ALIAS

    task_id = allocate_task_id(cfg.data_root, counter_alias)
    validate_task_id(task_id)
    task_dir = _make_skeleton(cfg.data_root, task_id, kind)
    log_handler = _attach_controller_log(task_dir)

    # Schema-valid scaffold BEFORE any failable op: projects.load, resolve_base,
    # worktree_add, validate_bind_source, containers.run can all raise — without
    # a prewritten task.json, the failed-state path would publish a stub missing
    # schema_version/id/kind, breaking subsequent `finish` with UnsupportedSchemaError.
    labels = _build_labels(task_id, project)
    task = _initial_task(
        task_id=task_id,
        kind=kind,
        project=project,
        branch=None,
        worktree_path=None,
        base_branch=None,
        base_commit=None,
        finish_policy=policy,
        secrets=secrets,
        cfg=cfg,
        labels=labels,
    )
    store.write_task(task_dir, task)

    try:
        logger = logging.getLogger("naiw_tasks")
        logger.info("start: task_id=%s kind=%s", task_id, kind)

        if kind is TaskKind.PROJECT:
            projects_yaml = cfg.data_root / "projects.yaml"
            project_map = projects.load(projects_yaml, cfg.data_root)
            if project not in project_map:
                raise StartFailed(
                    f"unknown project {project!r}; "
                    f"alias not in {projects_yaml}"
                )
            project_repo = Path(project_map[project]["path"])
            base_branch, base_commit = git_ops.resolve_base(
                project_repo, base_ref
            )
            work_path = task_dir / "work"
            # work_path MUST NOT exist here — _make_skeleton intentionally
            # skipped it for project kind so git worktree add can create it.
            git_ops.worktree_add(project_repo, task_id, work_path, base_commit)
            branch = f"agent/{task_id}"
            worktree_path = str(work_path.resolve())
            ts_meta = Event.now_iso()

            def _attach_project_meta(d: dict) -> dict:
                d = dict(d)
                d["branch"] = branch
                d["worktree_path"] = worktree_path
                d["base_branch"] = base_branch
                d["base_commit"] = base_commit
                d["updated_at"] = ts_meta
                return d

            store.update_task(task_dir, _attach_project_meta)
            task = replace(
                task,
                branch=branch,
                worktree_path=worktree_path,
                base_branch=base_branch,
                base_commit=base_commit,
                updated_at=ts_meta,
            )

        volumes = _build_volumes(cfg.data_root, task_dir, secrets)

        container = client.containers.run(
            image=cfg.task_image,
            name=_container_name(task_id),
            labels=labels,
            volumes=volumes,
            detach=True,
            **HARDENED_HOST_CONFIG_KWARGS,
        )
        image_digest = getattr(container.image, "id", None)

        ts_started = Event.now_iso()

        def _to_running(d: dict) -> dict:
            d = dict(d)
            d["status"] = str(Status.RUNNING)
            d["started_at"] = ts_started
            d["updated_at"] = ts_started
            d["image_digest"] = image_digest
            return d

        store.update_task(task_dir, _to_running)

        logger.info(
            "start: container=%s status=running digest=%s",
            _container_name(task_id),
            image_digest,
        )

        print(f"started {_container_name(task_id)} (status=running)")
        print(f"  attach:   naiw-tasks attach {task_id}")
        print("  detach:   Ctrl-P Ctrl-Q (Docker default; documented)")

        return replace(
            task,
            status=Status.RUNNING,
            started_at=ts_started,
            updated_at=ts_started,
            image_digest=image_digest,
        )

    except (
        BindMountEscapeError,
        git_ops.GitWorktreeError,
        docker.errors.APIError,
        docker.errors.NotFound,
        StartFailed,
    ) as exc:
        ts_now = Event.now_iso()
        reason = str(exc)
        stderr_extra = ""
        if isinstance(exc, git_ops.GitWorktreeError) and exc.stderr:
            stderr_extra = "\n  git stderr: " + exc.stderr.strip().replace(
                "\n", "\n  "
            )
            first_line = exc.stderr.strip().splitlines()[0] if exc.stderr.strip() else ""
            if first_line:
                reason = f"git worktree add failed: {first_line}"

        def _to_failed(d: dict) -> dict:
            d = dict(d)
            d["status"] = str(Status.FAILED)
            d["failure_reason"] = reason
            d["updated_at"] = ts_now
            return d

        # Scaffold task.json was written before the try block, so the failed-state
        # mutation always operates on a schema-valid document. FileNotFoundError
        # is impossible here under normal flow; let any real I/O failure surface.
        store.update_task(task_dir, _to_failed)

        # flush=True so the message survives buffered-stderr in CI pipes / test runners.
        print(
            f"naiw-tasks: start failed for task {task_id}: {reason}{stderr_extra}\n"
            f"  to reclaim disk: naiw-tasks finish {task_id} --delete-worktree",
            file=sys.stderr,
            flush=True,
        )
        if isinstance(exc, StartFailed):
            raise
        raise StartFailed(reason) from exc

    finally:
        _detach_controller_log(log_handler)


def _resolve_finish_policy(
    cli_override: str | None,
    task_policy: str,
) -> FinishPolicy:
    """CLI flag wins; otherwise task.json's stored policy; if 'ask', prompt."""
    if cli_override:
        return FinishPolicy(cli_override)
    policy = FinishPolicy(task_policy)
    if policy is FinishPolicy.ASK:
        answer = input("keep worktree? [y/N]: ").strip().lower()
        return (
            FinishPolicy.KEEP_WORKTREE
            if answer in ("y", "yes")
            else FinishPolicy.DELETE_WORKTREE
        )
    return policy


def finish(
    cfg: Config,
    client,
    task_id: str,
    policy_override: str | None = None,
) -> None:
    """Permissive finish: works on running/created/failed; idempotent on completed."""
    validate_task_id(task_id)
    task_dir = cfg.data_root / "tasks" / task_id
    if not task_dir.exists():
        print(
            f"naiw-tasks: task {task_id!r} not found at {task_dir}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    log_handler = _attach_controller_log(task_dir)
    try:
        data = store.read_task(task_dir)
        current_status = data.get("status")
        if current_status == str(Status.COMPLETED):
            print(
                f"naiw-tasks: task {task_id} is already completed; nothing to do"
            )
            return

        container_name = data.get("container_name", _container_name(task_id))
        try:
            container = client.containers.get(container_name)
            try:
                container.stop(timeout=10)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError:
                pass
            try:
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError:
                pass
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError:
            pass

        # Worktree teardown — project tasks only. delete_worktree => git
        # worktree remove --force + prune (never raw recursive-delete).
        if data.get("kind") == str(TaskKind.PROJECT):
            policy = _resolve_finish_policy(
                policy_override,
                data.get("finish_policy", "ask"),
            )
            if policy is FinishPolicy.DELETE_WORKTREE:
                projects_yaml = cfg.data_root / "projects.yaml"
                pmap = projects.load(projects_yaml, cfg.data_root)
                project_alias = data.get("project")
                if project_alias and project_alias in pmap and data.get("worktree_path"):
                    project_repo = Path(pmap[project_alias]["path"])
                    work_path = Path(data["worktree_path"])
                    git_ops.worktree_remove(project_repo, work_path)

        ts_now = Event.now_iso()

        def _to_completed(d: dict) -> dict:
            d = dict(d)
            d["status"] = str(Status.COMPLETED)
            d["finished_at"] = ts_now
            d["updated_at"] = ts_now
            return d

        store.update_task(task_dir, _to_completed)
    finally:
        _detach_controller_log(log_handler)
