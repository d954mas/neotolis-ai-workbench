"""Host-side tail of terminal.log with O_NOFOLLOW + S_ISREG defense.

Pi has rw access to /io inside the container. A malicious or buggy Pi could
replace terminal.log with a symlink to a controller-side file. Two layered
defenses:

  1. `lstat()` + `stat.S_ISREG()` BEFORE opening — refuses symlinks, fifos,
     sockets early with a clear stderr message.
  2. `os.open(..., O_RDONLY | O_NOFOLLOW)` — kernel-level refusal (ELOOP)
     if anything slipped past the pre-check (TOCTOU window between lstat
     and open).

No Docker call (host-only by design — events.jsonl tail is intentionally
not surfaced here; operator can cat it directly when needed).
"""

import os
import stat
import sys

import click

from naiw_tasks.config import Config
from naiw_tasks.ids import validate_task_id


def run(cfg: Config, task_id: str, lines: int) -> None:
    """Print last `lines` lines of terminal.log to stdout.

    Validates task_id shape as the first statement so direct (non-CLI)
    callers cannot bypass the regex check — the CLI veneer already
    translates ValueError to exit 3, but a direct programmatic caller
    that skipped the click layer would otherwise be able to walk paths
    via `..` / `/`.
    """
    validate_task_id(task_id)
    if lines <= 0:
        raise click.UsageError("--lines must be positive")

    task_dir = cfg.data_root / "tasks" / task_id
    if not task_dir.exists():
        print(
            f"naiw-tasks: task {task_id!r} not found at {task_dir}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)

    terminal_log = task_dir / "io" / "terminal.log"
    try:
        st = terminal_log.lstat()
    except FileNotFoundError:
        # Early-life task: container started but tmux has not yet flushed
        # the first pipe-pane line. Empty stdout + diagnostic to stderr +
        # exit 0 so shell pipelines don't trip.
        print(
            f"naiw-tasks: terminal.log not yet written for task {task_id}",
            file=sys.stderr,
            flush=True,
        )
        return
    if not stat.S_ISREG(st.st_mode):
        print(
            f"naiw-tasks: terminal.log for task {task_id} is not a regular "
            f"file (mode={oct(st.st_mode)}); refusing to read",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)

    # O_NOFOLLOW is the kernel-level closer of the lstat→open TOCTOU window.
    # ELOOP on symlink; the lstat+S_ISREG check above is belt-and-braces.
    try:
        fd = os.open(str(terminal_log), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        print(
            f"naiw-tasks: cannot open terminal.log for task {task_id}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc
    try:
        # errors='replace' so binary garbage (Pi prints raw bytes,
        # terminal control sequences, etc.) decodes without raising —
        # operator's job to grep through, not the controller's job to
        # filter.
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        os.close(fd)
        raise

    all_lines = content.splitlines(keepends=True)
    tail = all_lines[-lines:] if lines < len(all_lines) else all_lines
    sys.stdout.write("".join(tail))
    sys.stdout.flush()
