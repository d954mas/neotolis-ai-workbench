"""naiw-tasks CLI: start, attach, finish.

Thin click veneer over lifecycle/attach. The group invoke gates every
subcommand on startup_checks.run_all so the operator gets one error story
regardless of which command they typed.

Exit codes
----------
  0  success
  1  runtime error (lifecycle.StartFailed and similar; cause already on stderr)
  2  startup-check failed (or click usage error — message tells you which)
  3  invalid task id (TASK_ID_RE mismatch)
"""

import sys

import click

from naiw_tasks import attach as attach_mod
from naiw_tasks import config, lifecycle, startup_checks
from naiw_tasks.docker_client import make_client
from naiw_tasks.ids import validate_task_id


@click.group(
    help=(
        "naiw-tasks - controller for hardened per-task Docker sessions.\n\n"
        "Exit codes: 0=success; 1=runtime error; 2=startup-check failed "
        "(or click usage error - message tells you which); 3=invalid task id."
    )
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    cfg = config.load()
    client = make_client(cfg.docker_proxy_url)
    startup_checks.run_all(cfg, client)
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = cfg
    ctx.obj["client"] = client


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
@click.pass_context
def start(
    ctx: click.Context,
    project: str | None,
    base_ref: str | None,
    finish_policy: str,
    secrets: tuple[str, ...],
) -> None:
    """Start a project task (with PROJECT alias) or generic task (no alias)."""
    try:
        lifecycle.start(
            ctx.obj["cfg"],
            ctx.obj["client"],
            project=project,
            base_ref=base_ref,
            finish_policy=finish_policy,
            secrets=list(secrets),
        )
    except lifecycle.StartFailed:
        # lifecycle.start has already written task.json.status=failed and
        # printed the cause+hint to stderr. Click just needs the right exit code.
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
    attach_mod.attach_to_task(ctx.obj["cfg"], task_id)


@cli.command()
@click.argument("task_id")
@click.option(
    "--keep-worktree",
    "policy_override",
    flag_value="keep_worktree",
    default=None,
    help="Force keep_worktree (overrides task.json.finish_policy).",
)
@click.option(
    "--delete-worktree",
    "policy_override",
    flag_value="delete_worktree",
    help="Force delete_worktree (overrides task.json.finish_policy).",
)
@click.pass_context
def finish(
    ctx: click.Context,
    task_id: str,
    policy_override: str | None,
) -> None:
    """Stop+remove the container, apply worktree policy, mark completed."""
    try:
        validate_task_id(task_id)
    except ValueError as exc:
        click.echo(f"naiw-tasks: {exc}", err=True)
        sys.exit(3)
    lifecycle.finish(
        ctx.obj["cfg"],
        ctx.obj["client"],
        task_id,
        policy_override=policy_override,
    )


if __name__ == "__main__":  # pragma: no cover
    cli()
