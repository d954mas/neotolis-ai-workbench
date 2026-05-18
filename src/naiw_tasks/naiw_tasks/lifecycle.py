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
import stat
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import docker.errors
from naiw_common.events import Event

from naiw_tasks import git_ops, projects, store
from naiw_tasks.config import Config
from naiw_tasks.docker_client import (
    PINNED_DOCKER_API_VERSION,
    hardened_kwargs,
)
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

    Bind-mount source dirs (io/.naiw, generic-task work/) get mode 1777 (sticky
    + world-writable, same as /tmp). The task image runs as `pi` uid 1000
    hardcoded; without this the operator's umask 0755 would block pi from
    writing /io/.naiw/events.jsonl whenever the operator's host uid is not
    1000 (LDAP boxes, second-user installs, etc.). Sticky bit preserves
    owner-only delete so pi cannot remove files written by the operator.
    meta/ stays at default 0755: it is host-only, never bind-mounted into the
    container, and pi must not touch it.
    """
    task_dir = data_root / "tasks" / task_id
    (task_dir / "meta").mkdir(parents=True, exist_ok=True)
    io_naiw = task_dir / "io" / ".naiw"
    io_naiw.mkdir(parents=True, exist_ok=True)
    # Apply mode AFTER mkdir — mkdir's mode arg is masked by umask, chmod is not.
    (task_dir / "io").chmod(0o1777)
    io_naiw.chmod(0o1777)
    if kind is TaskKind.GENERIC:
        work = task_dir / "work"
        work.mkdir(exist_ok=True)
        work.chmod(0o1777)
    return task_dir


def _make_worktree_writable_by_pi(work_path: Path) -> None:
    """Recursively widen permissions on a freshly-checked-out git worktree
    so the image's `pi` user (uid 1000, hardcoded in image/Dockerfile) can
    edit files regardless of the operator's host uid.

    `git worktree add` runs as the operator and lays out files with the
    operator's umask — typically dirs 0755, files 0644 or 0755. When the
    operator is not uid 1000 (LDAP boxes, second-user installs), pi inside
    the container cannot modify the checked-out source. _make_skeleton
    chmod's bind-mount sources for generic tasks, but project worktrees
    are created HERE by git, not by _make_skeleton — they need the same
    fix applied after the fact.

    Mode policy:
      - directories            → 1777 (sticky-writable, /tmp-style)
      - regular files, no exec → 0666 (rw for all)
      - regular files w/ exec  → 0777 (rwx for all; preserves any-exec-bit
                                 so git sees the same 100755 vs 100644
                                 mode as before — `git status` stays clean)
      - symlinks / special     → skipped (chmod through symlink target may
                                 leak outside the worktree)

    Trade-off: this gives every host user rw access to the worktree, which
    is acceptable on the single-operator VPS this tool targets but is not
    the tightest possible sandbox. Cleaner alternatives, none free:
      - build task image with the operator's uid (requires a per-host image
        rebuild and breaks the published-from-ghcr distribution story)
      - run task containers with `--user $(id -u):$(id -g)` like the wrapper
        does (requires Pi tooling to function without a passwd entry)
      - chown the worktree to uid 1000 on Linux hosts (requires `sudo` or
        capable setuid helper, complicates Windows/WSL2 portability)
    Revisit when the single-user assumption stops holding.
    """
    work_path.chmod(0o1777)
    for path in work_path.rglob("*"):
        try:
            st = path.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            with suppress(OSError):
                path.chmod(0o1777)
        elif stat.S_ISREG(st.st_mode):
            new_mode = 0o777 if (st.st_mode & 0o111) else 0o666
            with suppress(OSError):
                path.chmod(new_mode)
        # else: symlink, fifo, socket — leave alone


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
    host_root: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Resolve every bind source under data_root before docker is touched.

    Source paths in the returned dict are HOST-side (rooted at host_root) —
    `client.containers.run(volumes=...)` is fielded by the Docker daemon, which
    resolves bind sources on the host's filesystem, NOT inside the controller
    container. When the controller runs under compose, data_root (`/naiw-data`)
    and host_root (`${HOME}/naiw-data`) differ. Validation still uses data_root
    so symlink-escape checks fire against the in-container filesystem the
    controller can actually read.

    host_root=None means host == container (direct host invocation, tests).
    """
    if host_root is None:
        host_root = data_root

    def _to_host(p: Path) -> str:
        return str(host_root / p.relative_to(data_root))

    volumes: dict[str, dict[str, str]] = {}
    work_src = validate_bind_source(task_dir / "work", data_root)
    io_src = validate_bind_source(task_dir / "io", data_root)
    pi_pkgs_src = validate_bind_source(data_root / "pi-packages", data_root)
    volumes[_to_host(work_src)] = {"bind": "/work", "mode": "rw"}
    volumes[_to_host(io_src)] = {"bind": "/io", "mode": "rw"}
    volumes[_to_host(pi_pkgs_src)] = {"bind": "/pi-packages", "mode": "ro"}
    # Secrets MUST resolve under data_root/secrets/, NOT just under data_root.
    # Without narrowing the prefix, a name like "../config.yaml" passes the
    # broader data_root check (after `..` collapses) and the controller would
    # mount ~/naiw-data/<arbitrary> into /run/secrets/<traversed> — an
    # isolation escape. CLI also pre-validates via ids.validate_secret_name;
    # this prefix narrowing is defense-in-depth for programmatic callers.
    secrets_dir = data_root / "secrets"
    for name in secrets:
        secret_src = validate_bind_source(
            secrets_dir / name, data_root, prefix=secrets_dir
        )
        volumes[_to_host(secret_src)] = {
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
    auto_finish: bool,
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
        auto_finish=auto_finish,
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
    auto_finish: bool = False,
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
        auto_finish=auto_finish,
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
            # Make the just-checked-out worktree writable by pi (uid 1000
            # inside the container) regardless of the operator's host uid.
            # Without this, project tasks on uid != 1000 hosts get a /work
            # bind mount Pi can read but not edit — exactly the case
            # _make_skeleton's chmod 1777 covers for generic tasks.
            _make_worktree_writable_by_pi(work_path)
            branch = f"agent/{task_id}"
            worktree_path = str(work_path.resolve())
            project_repo_path = str(project_repo.resolve())
            ts_meta = Event.now_iso()

            def _attach_project_meta(d: dict) -> dict:
                d = dict(d)
                d["branch"] = branch
                d["worktree_path"] = worktree_path
                d["base_branch"] = base_branch
                d["base_commit"] = base_commit
                d["project_repo_path"] = project_repo_path
                d["updated_at"] = ts_meta
                return d

            store.update_task(task_dir, _attach_project_meta)
            task = replace(
                task,
                branch=branch,
                worktree_path=worktree_path,
                base_branch=base_branch,
                base_commit=base_commit,
                project_repo_path=project_repo_path,
                updated_at=ts_meta,
            )

        volumes = _build_volumes(cfg.data_root, task_dir, secrets, cfg.host_root)

        container = client.containers.run(
            image=cfg.task_image,
            name=_container_name(task_id),
            labels=labels,
            volumes=volumes,
            detach=True,
            **hardened_kwargs(),
        )
        # Read the image digest from inspect attrs, NOT from any property
        # that would resolve to GET /images/* — the locked proxy blocks that
        # path (IMAGES=0). reload() repopulates attrs via GET /containers/<id>/json
        # (CONTAINERS endpoint, allow-listed); attrs["Image"] is the resolved
        # digest in "sha256:..." form, same value the higher-level shortcut
        # would return.
        container.reload()
        image_digest = container.attrs.get("Image")
        # task.json must carry the resolved image digest as audit trail —
        # without it we cannot answer "which image actually ran task X" once
        # the tag is repointed (e.g., naiw-task-image:latest moves to a new
        # build), and the recovery flow cannot verify image identity later.
        # Fail fast — the leftover container is cleaned via `naiw-tasks finish`.
        if not image_digest:
            raise StartFailed(
                f"could not resolve image digest for {cfg.task_image} after "
                f"containers.run; refusing to record task without audit digest"
            )

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
        # projects.load raises raw FileNotFoundError (yaml missing) or ValueError
        # (yaml schema bad). Without catching them, `naiw-tasks start <alias>`
        # against a missing/corrupt projects.yaml dumps a Python traceback to the
        # operator instead of a clean naiw-tasks: error.
        FileNotFoundError,
        ValueError,
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
        elif (
            isinstance(exc, docker.errors.APIError)
            and getattr(exc, "status_code", None) == 409
        ):
            # Daemon returns 409 Conflict when a container with the requested
            # name already exists. Common operator scenario: the previous task
            # crashed mid-start and left an orphan, or `tasks/<id>/` was deleted
            # by hand but `docker rm` was forgotten. Swap the raw HTTP-409 text
            # for a clean reason with a ready-to-run cleanup command.
            #
            # The hint embeds BOTH:
            #   - `-H <proxy_url>` so the command stays within the locked-proxy
            #     boundary (a plain `docker rm -f` would hit /var/run/docker.sock
            #     and bypass the same proxy that attach.py routes through).
            #   - `DOCKER_API_VERSION=<pinned>` so the docker CLI does NOT
            #     negotiate via /_ping (blocked, PING=0) and does NOT default
            #     to its bundled-client version (often 1.45+ on docker CLI 26)
            #     which may mismatch the daemon. Both attach and this cleanup
            #     hint take the same pin path.
            cname = _container_name(task_id)
            reason = (
                f"container name {cname!r} already in use by an orphan; "
                f"clean up with: "
                f"DOCKER_API_VERSION={PINNED_DOCKER_API_VERSION} "
                f"docker -H {cfg.docker_proxy_url} rm -f {cname}"
            )

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


def _mark_finish_failed(task_dir: Path, task_id: str, reason: str) -> None:
    """Write task.json.status=failed with reason, log a clear retry hint to stderr.

    Used when `finish` cannot verify that the container was torn down. Leaving
    the task in `failed` (not `completed`) means the next `naiw-tasks finish`
    invocation will not short-circuit, so the operator can retry once the
    Docker situation is resolved.
    """
    ts_now = Event.now_iso()

    def _to_failed(d: dict) -> dict:
        d = dict(d)
        d["status"] = str(Status.FAILED)
        d["failure_reason"] = f"finish: {reason}"
        d["updated_at"] = ts_now
        return d

    store.update_task(task_dir, _to_failed)
    print(
        f"naiw-tasks: finish failed for task {task_id}: {reason}\n"
        f"  retry: naiw-tasks finish {task_id}",
        file=sys.stderr,
        flush=True,
    )


def _resolve_finish_policy(
    cli_override: str | None,
    task_policy: str,
    allow_prompt: bool = True,
) -> FinishPolicy:
    """CLI flag wins; otherwise task.json's stored policy; if 'ask', prompt
    in interactive contexts.

    Non-interactive contexts (cron, systemd timers, shell pipes,
    signal-driven auto_finish, pytest captured stdin) cannot answer input()
    and would either hang or raise EOFError. In those contexts the policy
    defaults to delete_worktree — matches the documented MVP default
    ('disk-light by default for small VPS' per CLAUDE.md) — with a clear
    stderr note so the operator can see what happened in cron logs.
    """
    if cli_override:
        return FinishPolicy(cli_override)
    policy = FinishPolicy(task_policy)
    if policy is FinishPolicy.ASK:
        if not allow_prompt or not sys.stdin.isatty():
            print(
                "naiw-tasks: non-interactive context detected; defaulting "
                "finish_policy=ask to delete_worktree. "
                "Pass --keep-worktree to override.",
                file=sys.stderr,
                flush=True,
            )
            return FinishPolicy.DELETE_WORKTREE
        answer = input("keep worktree? [y/N]: ").strip().lower()
        return (
            FinishPolicy.KEEP_WORKTREE
            if answer in ("y", "yes")
            else FinishPolicy.DELETE_WORKTREE
        )
    return policy


# Disk statuses that mean teardown already happened. The CLI surface
# short-circuits on these; the shared helper does not.
TERMINAL_STATUSES: frozenset[str] = frozenset({
    str(Status.COMPLETED),
    str(Status.FAILED),
    str(Status.CANCELLED),
})


def teardown_and_mark(
    cfg: Config,
    client,
    task_id: str,
    task_dir: Path,
    terminal_status: Status,
    policy_override: str | None,
    allow_prompt: bool = True,
    failure_reason: str | None = None,
) -> None:
    """Stop+remove container, verify NotFound, apply finish policy if project
    task, write task.json.status=terminal_status with finished_at + updated_at
    atomically.

    The caller is responsible for any "already done" short-circuit; this helper
    always runs the full teardown sequence. Used by both the operator-facing
    `finish` wrapper (terminal_status=COMPLETED) and the lazy-event tailer that
    inline-finishes on `done`/`fail` events (terminal_status=COMPLETED/FAILED).
    """
    log_handler = _attach_controller_log(task_dir)
    try:
        data = store.read_task(task_dir)
        container_name = data.get("container_name", _container_name(task_id))

        # Best-effort stop + remove. APIError is silently caught here because
        # the verify step below is the source of truth — we mark the terminal
        # status only when the container is verifiably gone. Silently marking
        # terminal despite docker errors would create a zombie container with
        # no path back through the CLI (the wrapper short-circuits on terminal
        # disk status).
        try:
            container = client.containers.get(container_name)
        except docker.errors.NotFound:
            container = None
        except docker.errors.APIError as exc:
            logging.getLogger("naiw_tasks").warning(
                "finish: task %s: cannot inspect container before teardown: %s",
                task_id,
                exc,
            )
            container = None

        if container is not None:
            with suppress(docker.errors.NotFound, docker.errors.APIError):
                container.stop(timeout=10)
            with suppress(docker.errors.NotFound, docker.errors.APIError):
                container.remove(force=True)

        # Verify teardown actually happened. ONLY NotFound counts as success
        # — even an exited/dead/created container leaves a record in
        # `docker ps -a`, keeps the name reserved, and (because the wrapper
        # short-circuits on terminal disk status) cuts off the CLI path back
        # to cleanup. Marking failed in those cases lets the operator retry
        # once the daemon is healthy; force-remove typically succeeds on the
        # second attempt against an exited container.
        try:
            survivor = client.containers.get(container_name)
            survivor_state = survivor.attrs.get("State", {}).get(
                "Status", "<unknown>"
            )
        except docker.errors.NotFound:
            survivor_state = None
        except docker.errors.APIError as exc:
            _mark_finish_failed(
                task_dir,
                task_id,
                f"cannot verify container teardown ({exc})",
            )
            raise SystemExit(1) from exc

        if survivor_state is not None:
            _mark_finish_failed(
                task_dir,
                task_id,
                f"container {container_name} still present "
                f"(state={survivor_state!r}) after stop+remove",
            )
            raise SystemExit(1)

        # Worktree teardown — project tasks only. delete_worktree => git
        # worktree remove --force + prune (never raw recursive-delete).
        # Repo path comes from task.json (project_repo_path), NOT from projects.yaml —
        # this makes teardown robust against config edits between start and finish.
        if data.get("kind") == str(TaskKind.PROJECT):
            policy = _resolve_finish_policy(
                policy_override,
                data.get("finish_policy", "ask"),
                allow_prompt=allow_prompt,
            )
            if policy is FinishPolicy.DELETE_WORKTREE:
                repo_path = data.get("project_repo_path")
                worktree_path = data.get("worktree_path")

                # Compat-fallback for legacy task.json (task started before
                # project_repo_path was added to the schema): if the field is
                # absent but project alias + worktree are set, attempt a one-time
                # projects.yaml lookup. This preserves the "updates must not
                # break in-flight task.json state" invariant from CLAUDE.md.
                if not repo_path and worktree_path and data.get("project"):
                    project_alias = data["project"]
                    try:
                        pmap = projects.load(
                            cfg.data_root / "projects.yaml", cfg.data_root
                        )
                        if project_alias in pmap:
                            repo_path = pmap[project_alias]["path"]
                            print(
                                f"naiw-tasks: task {task_id}: legacy task.json "
                                f"missing project_repo_path; resolved "
                                f"{project_alias!r} via projects.yaml fallback",
                                file=sys.stderr,
                                flush=True,
                            )
                    except (FileNotFoundError, ValueError):
                        # Fall through to leak-warning below.
                        pass

                if repo_path and worktree_path:
                    try:
                        git_ops.worktree_remove(
                            Path(repo_path), Path(worktree_path)
                        )
                    except git_ops.GitWorktreeError as exc:
                        # User asked for delete_worktree but git failed —
                        # don't silently mark terminal with a dirty disk.
                        first_line = (
                            exc.stderr.strip().splitlines()[0]
                            if exc.stderr and exc.stderr.strip()
                            else str(exc)
                        )
                        _mark_finish_failed(
                            task_dir,
                            task_id,
                            f"git worktree remove failed: {first_line}",
                        )
                        raise SystemExit(1) from exc
                elif worktree_path:
                    print(
                        f"naiw-tasks: task {task_id}: worktree at "
                        f"{worktree_path} NOT removed (no project_repo_path "
                        f"in task.json, projects.yaml fallback unavailable); "
                        f"clean up manually",
                        file=sys.stderr,
                        flush=True,
                    )

        ts_now = Event.now_iso()

        def _to_terminal(d: dict) -> dict:
            d = dict(d)
            d["status"] = str(terminal_status)
            d["finished_at"] = d.get("finished_at") or ts_now
            d["updated_at"] = ts_now
            # failure_reason precedence on a successful retry:
            #   1. A caller-provided reason (e.g., the Pi-side
            #      `naiw-signal fail --reason ...` payload) is authoritative —
            #      it replaces any stale `finish:` teardown error left by an
            #      earlier reap that hit a transient docker hiccup.
            #   2. Otherwise on COMPLETED, clear any leftover `finish:`
            #      reason — a successfully completed task shouldn't carry a
            #      failure reason from a previous teardown attempt.
            #   3. On FAILED/CANCELLED without a new reason, preserve what's
            #      there (might be the real cause from an earlier write).
            existing = str(d.get("failure_reason") or "")
            if failure_reason:
                d["failure_reason"] = failure_reason
            elif terminal_status == Status.COMPLETED and existing.startswith("finish:"):
                d.pop("failure_reason", None)
            return d

        store.update_task(task_dir, _to_terminal)
    finally:
        _detach_controller_log(log_handler)


def finish(
    cfg: Config,
    client,
    task_id: str,
    policy_override: str | None = None,
    force: bool = False,
) -> None:
    """Permissive finish (operator-facing). Idempotent on terminal disk status.

    The wrapper preserves the friendly "already <status>; nothing to do" message
    for the human typing `naiw-tasks finish <id>` against a task that has already
    reached a terminal state. Programmatic callers that need teardown regardless
    of disk status (e.g., the lazy-event tailer applying a `done`/`fail` event
    with auto_finish=true) should call `teardown_and_mark` directly with the
    desired terminal_status.

    `force=True` is the operator recovery hatch when an earlier auto_finish (or
    a previous finish attempt) wrote a terminal status to task.json but failed
    to tear down the container — e.g. `docker rm` rejected by the proxy. The
    short-circuit then traps the operator: the disk says `failed`, but the
    container is still alive and the lazy event tailer will not re-fire because
    `events_offset` is already past the event. `--force` bypasses the
    short-circuit and re-runs `teardown_and_mark`, preserving the existing
    terminal status (`failed` stays `failed`, `cancelled` stays `cancelled`)
    so the audit trail of WHY the task is in that state survives the retry.

    Auto-recovery: when task.json already records a terminal status but a
    container with the matching name still exists on Docker (e.g., a non-
    auto_finish task whose `done`/`fail` event flipped task.json forward in
    list, while the container kept running), the short-circuit would leave
    the operator with a leaked container. To avoid that trap, probe Docker
    first; if the container is still present, fall through to teardown_and_mark
    as if `--force` had been passed — preserving the existing terminal status.
    """
    validate_task_id(task_id)
    task_dir = cfg.data_root / "tasks" / task_id
    if not task_dir.exists():
        print(
            f"naiw-tasks: task {task_id!r} not found at {task_dir}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    data = store.read_task(task_dir)
    current_status = data.get("status")
    if current_status in TERMINAL_STATUSES and not force:
        # An explicit worktree-policy flag is the operator asking for a
        # specific cleanup action — most commonly the documented
        # `naiw-tasks finish <id> --delete-worktree` to reclaim disk from a
        # failed start (status=failed, container never came up but worktree
        # exists). Honor the request and bypass the short-circuit so the
        # teardown sequence runs the policy branch even when the container
        # is verifiably gone.
        if policy_override is not None:
            force = True
        else:
            container_name = data.get("container_name", _container_name(task_id))
            # Probe before short-circuit so a leaked container can't trap the
            # operator. NotFound → genuine no-op; APIError → assume gone and
            # short-circuit, operator can --force if needed.
            try:
                client.containers.get(container_name)
                container_present = True
            except docker.errors.NotFound:
                container_present = False
            except docker.errors.APIError as exc:
                logging.getLogger("naiw_tasks").warning(
                    "finish: task %s: cannot inspect container before short-circuit: %s",
                    task_id, exc,
                )
                container_present = False
            if not container_present:
                print(
                    f"naiw-tasks: task {task_id} is already {current_status}; nothing to do"
                )
                return
            # Fall through with terminal status preserved (auto-recovery).
            force = True

    # On force-retry of a task that is already terminal, preserve the recorded
    # outcome — re-running teardown should not silently flip `failed` to
    # `completed`. Status() raises on unknown strings; that is the right
    # behaviour because we should not invent a terminal status the operator
    # cannot see in the model.
    if current_status in TERMINAL_STATUSES and force:
        terminal_status = Status(current_status)
    else:
        terminal_status = Status.COMPLETED

    teardown_and_mark(
        cfg,
        client,
        task_id,
        task_dir,
        terminal_status=terminal_status,
        policy_override=policy_override,
        allow_prompt=True,
    )
