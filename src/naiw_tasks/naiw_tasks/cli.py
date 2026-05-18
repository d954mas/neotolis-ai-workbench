"""naiw-tasks CLI: start, attach, finish.

Thin click veneer over lifecycle/attach. Docker-touching commands run
startup_checks.run_all on first client use; host-only commands like `output`
can still work while Docker/proxy is down.

Exit codes
----------
  0  success
  1  runtime error (lifecycle.StartFailed and similar; cause already on stderr)
  2  startup-check failed, config.yaml parse/schema error, or click usage error
     (message tells you which)
  3  invalid task id or project alias (DNS-label shape mismatch)
"""

import sys

import click

from naiw_tasks import attach as attach_mod
from naiw_tasks import clean as clean_mod
from naiw_tasks import config, lifecycle, startup_checks
from naiw_tasks import disk as disk_mod
from naiw_tasks import list_cmd as list_cmd_module
from naiw_tasks import output_cmd as output_cmd_module
from naiw_tasks.docker_client import make_client
from naiw_tasks.ids import (
    validate_project_alias,
    validate_secret_name,
    validate_task_id,
)


@click.group(
    help=(
        "naiw-tasks - controller for hardened per-task Docker sessions.\n\n"
        "Exit codes: 0=success; 1=runtime error; 2=startup-check failed, "
        "config.yaml parse/schema error, or click usage error; "
        "3=invalid task id or project alias."
    )
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    # Keep config errors uniform across every subcommand.
    try:
        cfg = config.load()
    except ValueError as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(2)
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = cfg


def _client_for(
    ctx: click.Context,
    gate_disk_threshold: bool = False,
    disk_threshold_verb: str = "start",
):
    """Create/cache the Docker client and run startup checks on first use.

    `gate_disk_threshold` enables the >95% max_data_size refusal for `start`
    and `recover`. Other commands (attach, finish, list, output, clean,
    disk) leave the gate off - they may free disk; gating them would lock
    the operator out of recovery.
    """
    if "client" not in ctx.obj:
        cfg = ctx.obj["cfg"]
        client = make_client(cfg.docker_proxy_url)
        startup_checks.run_all(
            cfg,
            client,
            gate_disk_threshold=gate_disk_threshold,
            disk_threshold_verb=disk_threshold_verb,
        )
        ctx.obj["client"] = client
    return ctx.obj["client"]


@cli.command()
@click.argument("project", required=False)
@click.option(
    "--base",
    "base_ref",
    default=None,
    help="Git ref to base the worktree on (default: autodetect origin/main -> HEAD).",
)
@click.option(
    "--finish-policy",
    type=click.Choice(["ask", "keep_worktree", "delete_worktree"]),
    default="ask",
    help="Default finish policy (recorded in task.json).",
)
@click.option(
    "--secret",
    "secrets",
    multiple=True,
    help="Secret name to mount at /run/secrets/<name>:ro (repeatable).",
)
@click.option(
    "--auto-finish",
    is_flag=True,
    default=False,
    help="Let `naiw-tasks reap` close the task after done/fail signal events.",
)
@click.pass_context
def start(
    ctx: click.Context,
    project: str | None,
    base_ref: str | None,
    finish_policy: str,
    secrets: tuple[str, ...],
    auto_finish: bool,
) -> None:
    """Start a project task (with PROJECT alias) or generic task (no alias)."""
    # Validate at the CLI boundary so bad input gets a clean exit 3.
    if project is not None:
        try:
            validate_project_alias(project)
        except ValueError as exc:
            click.echo(f"naiw-tasks: {exc}", err=True)
            sys.exit(3)
    # _build_volumes narrows the bind-source prefix too; this is the clean
    # operator-facing error path.
    for secret_name in secrets:
        try:
            validate_secret_name(secret_name)
        except ValueError as exc:
            click.echo(f"naiw-tasks: {exc}", err=True)
            sys.exit(3)
    try:
        lifecycle.start(
            ctx.obj["cfg"],
            _client_for(ctx, gate_disk_threshold=True),
            project=project,
            base_ref=base_ref,
            finish_policy=finish_policy,
            secrets=list(secrets),
            auto_finish=auto_finish,
        )
    except lifecycle.StartFailed:
        # lifecycle.start already wrote task.json and stderr.
        sys.exit(1)


@cli.command("attach")
@click.argument("task_id")
@click.pass_context
def attach_cmd(ctx: click.Context, task_id: str) -> None:
    """Attach the operator's terminal to the running task container."""
    try:
        validate_task_id(task_id)
    except ValueError as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(3)
    attach_mod.attach_to_task(
        _client_for(ctx), ctx.obj["cfg"].docker_proxy_url, task_id
    )


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Verify config, proxy reachability, and proxy allowlist."""
    _client_for(ctx)
    click.echo("naiw-tasks: doctor OK")


_LIST_STATUSES: tuple[str, ...] = (
    "created",
    "running",
    "interrupted",
    "waiting_for_user",
    "completed",
    "failed",
    "cancelled",
)


def _parse_statuses(
    ctx: click.Context, param: click.Parameter, value: tuple[str, ...]
) -> list[str]:
    """Accept repeated `--status` AND comma-separated values per call.

    task.md documents `naiw-tasks list --status running,interrupted,...` —
    click.Choice can't validate a comma-joined string, so we split here and
    validate each piece. Empty pieces are dropped so `--status running,`
    is forgiving rather than a usage error.
    """
    out: list[str] = []
    for item in value:
        for piece in item.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if piece not in _LIST_STATUSES:
                raise click.BadParameter(
                    f"{piece!r} is not a valid status; "
                    f"choose from {', '.join(_LIST_STATUSES)}"
                )
            if piece not in out:
                out.append(piece)
    return out


@cli.command("list")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=10,
    help="Max rows after filtering (default 10; must be >= 1).",
)
@click.option(
    "--status",
    "statuses",
    multiple=True,
    callback=_parse_statuses,
    help=(
        "Filter by status (repeatable; OR within filter). Each occurrence "
        "may carry one status or a comma-separated list, e.g. "
        "'--status running,interrupted'."
    ),
)
@click.option(
    "--project",
    "project_filter",
    default=None,
    help="Filter to one project alias.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show every task; uncaps --limit unless --limit is given explicitly.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help=(
        "Emit JSON {as_of, tasks: [{id, status, container_state, "
        "container_exit_code, project, started_at, image_digest, notes, "
        "task_json}]} instead of the plain table."
    ),
)
@click.pass_context
def list_command(
    ctx: click.Context,
    limit: int,
    statuses: list[str],
    project_filter: str | None,
    show_all: bool,
    as_json: bool,
) -> None:
    """List tasks with reconciled status (default shows terminal too)."""
    # --all uncaps rows unless the operator explicitly supplied --limit.
    limit_was_explicit = (
        ctx.get_parameter_source("limit")
        == click.core.ParameterSource.COMMANDLINE
    )
    list_cmd_module.run(
        ctx.obj["cfg"],
        _client_for(ctx),
        list_cmd_module.ListRequest(
            limit=limit,
            statuses=statuses,
            project_filter=project_filter,
            show_all=show_all,
            as_json=as_json,
            limit_was_explicit=limit_was_explicit,
            apply_auto_finish=False,
        ),
    )


@cli.command("reap")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of the plain table.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show tasks that would be reaped without closing containers.",
)
@click.pass_context
def reap_command(ctx: click.Context, as_json: bool, dry_run: bool) -> None:
    """Apply auto_finish terminal events and close ready tasks."""
    result = list_cmd_module.run(
        ctx.obj["cfg"],
        _client_for(ctx),
        list_cmd_module.ListRequest(
            limit=None,
            statuses=[],
            project_filter=None,
            show_all=True,
            as_json=as_json,
            limit_was_explicit=False,
            apply_auto_finish=True,
            dry_run=dry_run,
        ),
    )
    if dry_run:
        click.echo(f"naiw-tasks: would reap {result.would_reap} task(s)", err=True)
    else:
        click.echo(f"naiw-tasks: reaped {result.reaped} task(s)", err=True)


@cli.command("output")
@click.argument("task_id")
@click.option(
    "--lines",
    "lines",
    type=int,
    default=200,
    help="Number of trailing lines (default 200; must be positive).",
)
@click.pass_context
def output_command(ctx: click.Context, task_id: str, lines: int) -> None:
    """Print the last lines of terminal.log for TASK_ID (host-side read)."""
    # CLI veneer validates id and translates ValueError → exit 3. The
    # output_cmd.run first-statement validate_task_id is defense-in-depth
    # for direct (non-CLI) callers that skip this veneer.
    try:
        validate_task_id(task_id)
    except ValueError as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(3)
    output_cmd_module.run(ctx.obj["cfg"], task_id, lines=lines)


@cli.command()
@click.argument("task_id")
@click.option(
    "--keep-worktree",
    "keep_worktree",
    is_flag=True,
    default=False,
    help="Force keep_worktree (overrides task.json.finish_policy).",
)
@click.option(
    "--delete-worktree",
    "delete_worktree",
    is_flag=True,
    default=False,
    help="Force delete_worktree (overrides task.json.finish_policy).",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help=(
        "Re-run teardown even when task.json already records a terminal "
        "status. Use when a previous auto_finish or finish wrote `failed` "
        "but the container is still running (e.g., docker rm was rejected "
        "by the proxy). The existing terminal status is preserved."
    ),
)
@click.pass_context
def finish(
    ctx: click.Context,
    task_id: str,
    keep_worktree: bool,
    delete_worktree: bool,
    force: bool,
) -> None:
    """Stop+remove the container, apply worktree policy, mark completed."""
    # Conflict is a usage error, NOT silent last-flag-wins. Otherwise an
    # operator who typed `--keep-worktree --delete-worktree` (or set one
    # in a shell alias and the other on the command line) silently performs
    # the destructive operation. Better to refuse and force an explicit choice.
    if keep_worktree and delete_worktree:
        raise click.UsageError(
            "--keep-worktree and --delete-worktree are mutually exclusive; "
            "pass at most one (or neither, to fall back to "
            "task.json.finish_policy)."
        )
    policy_override: str | None = None
    if keep_worktree:
        policy_override = "keep_worktree"
    elif delete_worktree:
        policy_override = "delete_worktree"

    try:
        validate_task_id(task_id)
    except ValueError as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(3)
    lifecycle.finish(
        ctx.obj["cfg"],
        _client_for(ctx),
        task_id,
        policy_override=policy_override,
        force=force,
    )


@cli.command("recover")
@click.argument("task_id")
@click.pass_context
def recover_command(ctx: click.Context, task_id: str) -> None:
    """Recover an interrupted task: fresh container, same name/labels/mounts."""
    try:
        validate_task_id(task_id)
    except ValueError as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(3)
    try:
        lifecycle.recover(
            ctx.obj["cfg"],
            _client_for(
                ctx,
                gate_disk_threshold=True,
                disk_threshold_verb="recover",
            ),
            task_id,
        )
    except lifecycle.RecoverNotInterrupted as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(2)
    except lifecycle.RecoverFailed as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(1)


@cli.command("clean")
@click.option(
    "--older-than",
    "older_than",
    required=True,
    help="Duration: <int><unit> where unit is s, m, h, d, w (e.g. '30d').",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="List candidates and orphans; do not remove.",
)
@click.option(
    "--yes",
    "skip_prompt",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt (cron-friendly).",
)
@click.pass_context
def clean_command(
    ctx: click.Context,
    older_than: str,
    dry_run: bool,
    skip_prompt: bool,
) -> None:
    """Remove terminal-state tasks older than DURATION; prune orphan containers.

    Graceful-degrades when Docker is unreachable: disk-side cleanup still
    runs (so the operator can reclaim space before fixing Docker); the
    orphan-container scan is skipped with a clear notice.
    """
    try:
        td = clean_mod.parse_older_than(older_than)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    # Graceful-degrade ONLY on Docker reachability / proxy-drift failures
    # so disk reclaim still works when the daemon is down. Other
    # StartupCheckFailed variants (symlinked data root, Windows-FS gating)
    # signal an unsafe local config and MUST still hard-stop — they
    # propagate untouched.
    try:
        client = _client_for(ctx)
    except startup_checks.DockerCheckFailed:
        client = None
    rc = clean_mod.run(
        ctx.obj["cfg"],
        client,
        older_than=td,
        dry_run=dry_run,
        skip_prompt=skip_prompt,
    )
    sys.exit(rc)


@cli.command("disk")
@click.pass_context
def disk_command(ctx: click.Context) -> None:
    """Print per-subdirectory disk usage of ~/naiw-data/; warn at >80%."""
    rc = disk_mod.run(ctx.obj["cfg"])
    sys.exit(rc)


if __name__ == "__main__":  # pragma: no cover
    cli()
