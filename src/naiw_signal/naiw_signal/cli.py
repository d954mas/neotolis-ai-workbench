"""naiw-signal CLI: append done/fail/wait events to /io/.naiw/events.jsonl."""

import click
from naiw_common.events import Event

from .writer import append_event


@click.group(help="naiw-signal — append a Pi -> controller event to /io/.naiw/events.jsonl")
def cli() -> None:
    """Top-level group; subcommands: done, fail, wait."""


@cli.command()
@click.option("--summary", default=None, help="Optional one-line summary of work completed.")
def done(summary: str | None) -> None:
    """Signal that the task is complete."""
    append_event(Event.done(summary).as_dict())


@cli.command()
@click.option("--reason", required=True, help="Reason for failure.")
def fail(reason: str) -> None:
    """Signal that the task has failed."""
    append_event(Event.fail(reason).as_dict())


@cli.command()
@click.option("--reason", required=True, help="Reason for waiting.")
def wait(reason: str) -> None:
    """Signal that the task is waiting for the user."""
    append_event(Event.wait(reason).as_dict())


if __name__ == "__main__":  # pragma: no cover
    cli()
