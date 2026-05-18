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
from naiw_tasks import config, lifecycle, startup_checks
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


def _client_for(ctx: click.Context):
    """Create/cache the Docker client and run startup checks on first use."""
    if "client" not in ctx.obj:
        cfg = ctx.obj["cfg"]
        client = make_client(cfg.docker_proxy_url)
        startup_checks.run_all(cfg, client)
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
            _client_for(ctx),
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
    type=click.Choice(
        [
            "created",
            "running",
            "interrupted",
            "waiting_for_user",
            "completed",
            "failed",
            "cancelled",
        ]
    ),
    multiple=True,
    help="Filter by status (repeatable; OR within filter, AND across filters).",
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
    "--completed",
    "include_completed",
    is_flag=True,
    default=False,
    help="Include terminal-state tasks (completed/failed/cancelled).",
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
    statuses: tuple[str, ...],
    project_filter: str | None,
    show_all: bool,
    include_completed: bool,
    as_json: bool,
) -> None:
    """List tasks with reconciled status."""
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
            statuses=list(statuses),
            project_filter=project_filter,
            show_all=show_all,
            include_completed=include_completed,
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
            include_completed=True,
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


if __name__ == "__main__":  # pragma: no cover
    cli()
