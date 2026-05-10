"""Atomic JSONL append.

Uses POSIX os.open(O_WRONLY|O_APPEND|O_CREAT) + a single os.write + os.fsync.
Python text-mode open() is deliberately avoided — its buffering can split a
single logical write into multiple syscalls, defeating the atomic-append
guarantee that O_APPEND provides for writes within a kernel page.
"""

import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from naiw_common.paths import EVENTS_PATH

PIPE_BUF_LIMIT = 4096


def _serialise(event: Mapping[str, Any]) -> bytes:
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
    return line.encode("utf-8")


def append_event(event: Mapping[str, Any]) -> None:
    """Append one JSON event line to EVENTS_PATH atomically.

    Open with O_WRONLY|O_APPEND|O_CREAT, do a single os.write, fsync before close.
    Any I/O error prints a `naiw-signal: ...` line to stderr and exits 1.
    There is no retry loop — the controller is the only consumer and re-issues
    on its own schedule if the journal is briefly unavailable.
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
