"""Replace the controller process with `docker attach <container>`.

Why os.execvp rather than docker-py's container.attach: the SDK's
attach/exec_run path is not a real TTY (long-standing upstream limitation).
Process replacement is the only correct path that hands the operator's
terminal directly to the docker CLI.

Why attach rather than the SDK exec_run + interactive path: the latter
requires the proxy to expose EXEC=1, which is permanently 0 — the
controller never asks for arbitrary-code-execution capability. attach only
shares STDIO with the container's pid 1 (tmux), which is exactly what we want.
"""

import os
import sys
from contextlib import suppress


def attach_to_task(client, proxy_url: str, task_id: str) -> None:
    """Attach the operator to the running task container, or refuse with a hint.

    On success this function does not return — os.execvp replaces the current
    process image. The trailing SystemExit is defence-in-depth.

    `client` is supplied by the CLI group's already-constructed docker client
    (same one used by start/finish) — symmetrical injection across the package
    and avoids opening a second TCP session to the proxy.

    `proxy_url` is passed as `docker -H <proxy_url> attach <name>` so the CLI
    talks to the same locked proxy as the SDK. Without -H, docker CLI would
    fall back to the host socket (/var/run/docker.sock), bypassing the proxy
    entirely — that breaks the engine boundary.
    """
    # filters={"label": [...]} as a list (not a comma-joined string) so docker-py
    # emits two ?label= query params and the daemon AND-matches both labels.
    matches = client.containers.list(
        all=True,
        filters={"label": [f"naiw.task-id={task_id}", "naiw.managed=1"]},
    )
    if not matches:
        print(
            f"naiw-tasks: no container found for task-id={task_id}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    container = matches[0]

    # Refresh attrs in case the list cache is stale; a NotFound/APIError between
    # list and reload is benign — fall through to the state check which will
    # report the last known status (or <unknown>) and refuse cleanly.
    with suppress(Exception):
        container.reload()

    state = container.attrs.get("State", {}).get("Status", "<unknown>")
    if state != "running":
        print(
            f"naiw-tasks: container {container.name} is not running\n"
            f"  docker state: {state}\n"
            f"  task-id:      {task_id}\n"
            f"\n"
            f"Recovery of stopped tasks is not yet implemented in this controller.\n"
            f"For now, finish the task to clean up: naiw-tasks finish {task_id}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # POSIX argv[0] convention: the program name appears as the first element
    # of argv; execvp uses argv[1:] as the actual command arguments docker sees.
    # -H routes the CLI through the locked proxy (matches the SDK path).
    os.execvp(
        "docker", ["docker", "-H", proxy_url, "attach", container.name]
    )
    # execvp does not return on success.
    raise SystemExit(1)
