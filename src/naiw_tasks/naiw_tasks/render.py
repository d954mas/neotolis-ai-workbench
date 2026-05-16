"""Plain-ASCII table rendering + --json payload serialisation for `naiw-tasks list`.

No external dependencies — str.ljust for column padding, json.dumps for JSON,
stdlib datetime for relative-time formatting. Keeps the surface tiny so the
JSON shape is the bridge to any future web consumer.

Last column is intentionally NOT padded so piping the table through grep/awk
stays clean: no trailing whitespace, lines compare equal across rows.
"""

import json
from datetime import UTC, datetime
from typing import Any

from naiw_common.events import Event

# Column order is the documented surface of `naiw-tasks list`. Keep stable —
# operators (and any downstream `awk` / `cut` glue scripts) rely on the order.
COLUMNS: tuple[str, ...] = (
    "ID",
    "STATUS",
    "CTR",
    "PROJECT",
    "STARTED",
    "IMAGE",
    "NOTES",
)


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Pad each column to max width, join with two spaces. No trailing whitespace.

    The last column is not padded (no trailing spaces) so `naiw-tasks list |
    grep foo` and similar pipelines stay clean. Width per column is the max
    of header + every row cell.
    """
    if not headers:
        return "\n"
    all_rows = [headers, *rows]
    cols = list(zip(*all_rows, strict=False))
    widths = [max(len(cell) for cell in col) for col in cols]
    lines: list[str] = []
    for row in all_rows:
        padded: list[str] = []
        for i, (cell, w) in enumerate(zip(row, widths, strict=False)):
            if i == len(widths) - 1:
                padded.append(cell)  # last column — no trailing pad
            else:
                padded.append(cell.ljust(w))
        lines.append("  ".join(padded).rstrip())
    return "\n".join(lines) + "\n"


def _humanize_delta(now_iso: str, reference_iso: str | None) -> str:
    """Human-relative time. None → em-dash. Thresholds: <60s 'just now',
    <60m 'Nm ago', <24h 'Nh ago', else 'Nd ago'.

    Malformed ISO strings pass through unchanged so the operator can spot the
    bad value in their list output instead of getting a confusing exception.
    """
    if reference_iso is None:
        return "—"
    try:
        ref = datetime.fromisoformat(reference_iso.replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except ValueError:
        return reference_iso
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    delta = now - ref
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _short_image_digest(digest: str | None) -> str:
    """First 12 hex chars of the digest (after stripping `sha256:` prefix).

    Matches `docker images --no-trunc=false` truncation convention so an
    operator can grep the rendered image cell directly against `docker images`
    output without re-truncating.
    """
    if digest is None:
        return "<missing>"
    if digest.startswith("sha256:"):
        return digest[len("sha256:") : len("sha256:") + 12]
    return digest[:12]


def _format_notes(notes: tuple[str, ...]) -> str:
    """Join NOTES markers as `(marker), (marker)` for the NOTES column."""
    if not notes:
        return ""
    return ", ".join(f"({n})" for n in notes)


def to_json_payload(
    rendered: list[tuple[dict, Any, str, int | None]],
) -> dict[str, Any]:
    """Serialise the rendered rows to the documented JSON shape.

    Input: list of (task_dict, computed_row, container_state_str, exit_code).
    Output: {"as_of": iso-ts, "tasks": [...]} — flat per-task fields plus the
    full raw task.json under `task_json` so downstream consumers do not need
    to re-read the file.
    """
    tasks_out: list[dict[str, Any]] = []
    for task_dict, computed, ctr_state, exit_code in rendered:
        tasks_out.append(
            {
                "id": task_dict.get("id", ""),
                "kind": task_dict.get("kind"),
                "status": computed.status,
                "container_state": ctr_state,
                "container_exit_code": exit_code,
                "project": task_dict.get("project"),
                "started_at": task_dict.get("started_at"),
                "image_digest": task_dict.get("image_digest"),
                "notes": list(computed.notes),
                "task_json": task_dict,
            }
        )
    return {"as_of": Event.now_iso(), "tasks": tasks_out}


def to_json_string(payload: dict[str, Any]) -> str:
    """json.dumps with indent=2 + sort_keys=True (matches store.update_task style)."""
    return json.dumps(payload, indent=2, sort_keys=True)
