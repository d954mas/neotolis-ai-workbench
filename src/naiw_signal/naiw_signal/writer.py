"""Atomic JSONL append (D-05, SIG-03).

Writer uses POSIX os.open(O_WRONLY|O_APPEND|O_CREAT) + os.write + os.fsync.
No Python text-mode open() — Pitfall 2 (buffering can split a write into multiple
syscalls, breaking the < PIPE_BUF atomicity guarantee).
"""

import json
import os
import sys
from typing import Any, Mapping

from naiw_common.paths import EVENTS_PATH

PIPE_BUF_LIMIT = 4096


def _serialise(event: Mapping[str, Any]) -> bytes:
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
    return line.encode("utf-8")


def append_event(event: Mapping[str, Any]) -> None:
    """Append one JSON event line to EVENTS_PATH atomically.

    D-05: O_WRONLY|O_APPEND|O_CREAT, single os.write, os.fsync before os.close.
    D-06: any I/O error -> stderr line + sys.exit(1), no retry loop.
    """
    encoded = _serialise(event)
    if len(encoded) >= PIPE_BUF_LIMIT:
        print(
            f"naiw-signal: event too large ({len(encoded)} bytes >= {PIPE_BUF_LIMIT}); refusing",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        fd = os.open(EVENTS_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    except OSError as exc:
        print(f"naiw-signal: open failed: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        n = os.write(fd, encoded)
        if n != len(encoded):
            print(
                f"naiw-signal: short write {n} of {len(encoded)} bytes",
                file=sys.stderr,
            )
            sys.exit(1)
        os.fsync(fd)
    except OSError as exc:
        print(f"naiw-signal: write/fsync failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            print(f"naiw-signal: close failed: {exc}", file=sys.stderr)
            sys.exit(1)
