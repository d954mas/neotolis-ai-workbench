"""Pure truth-table evaluation: derive computed status from task.json + container + event.

No I/O. No clock. No Docker. Same inputs always return the same ComputedRow.
See tests/unit/test_reconcile.py for the canonical truth table.

Status strings are used directly (not the Status enum) so the evaluator can
pass an unrecognised-but-stored status through unchanged — the lenient-read
contract established for task.json must survive forward-compat status additions
even when an older controller binary observes a newer task.json.
"""

from dataclasses import dataclass
from typing import Literal

ContainerState = Literal[
    "running", "exited", "paused", "restarting", "created",
    "dead", "removing", "notfound",
]

# Container states that map to "interrupted" when the stored status is `running`
# or `waiting_for_user`. `created` and `restarting` are transient and should
# resolve quickly under normal operation; if the controller sees them when it
# thought the container was running, the daemon lost the run.
_INTERRUPTED_CTR_STATES: frozenset[str] = frozenset(
    {"paused", "restarting", "created", "dead", "removing"}
)

# Known status set. Anything outside this set is rendered as-is with an
# "unknown status" note (forward-compat passthrough — never raise).
_KNOWN_STATUSES: frozenset[str] = frozenset(
    {
        "created",
        "running",
        "interrupted",
        "waiting_for_user",
        "completed",
        "failed",
        "cancelled",
    }
)

# Terminal statuses that NEVER auto-transition. A surviving container with a
# terminal stored status is a leak — flag the row, do not mutate state.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class ComputedRow:
    """One row in the rendered `naiw-tasks list` table.

    `status`: the value the renderer prints AND the value the caller persists
        to task.json when `transitioned is True`.
    `transitioned`: True iff `status != task_dict['status']`. Caller uses this
        flag to decide whether to write the new status (avoid no-op writes).
    `notes`: markers for the NOTES column: ("leaked ctr",), ("unknown status",),
        (). Multiple notes concat as tuple.
    `failure_reason`: filled only when computed status is `failed` AND the
        transition has a specific cause (currently only the
        'created + container NotFound' cell).
    """

    status: str
    transitioned: bool
    notes: tuple[str, ...]
    failure_reason: str | None


def compute_status(
    task_dict: dict,
    ctr_state: str,
    ctr_exit_code: int | None,
    pending_event_kind: str | None,
) -> ComputedRow:
    """Truth-table evaluator. Never raises. See tests for the canonical 28+ cells."""
    current = task_dict.get("status", "")

    # Unknown status passes through unchanged; no transitions computed.
    if current not in _KNOWN_STATUSES:
        return ComputedRow(
            status=current,
            transitioned=False,
            notes=("unknown status",),
            failure_reason=None,
        )

    # Terminal status + container present (any state) → keep terminal, mark leaked.
    if current in _TERMINAL_STATUSES:
        if ctr_state == "notfound":
            return ComputedRow(current, False, (), None)
        return ComputedRow(current, False, ("leaked ctr",), None)

    # `interrupted` never auto-flips. Recover is operator-driven.
    if current == "interrupted":
        return ComputedRow(current, False, (), None)

    if current == "running":
        return _compute_from_running(ctr_state, ctr_exit_code, pending_event_kind)

    if current == "created":
        return _compute_from_created(ctr_state, ctr_exit_code, pending_event_kind)

    if current == "waiting_for_user":
        return _compute_from_waiting(ctr_state, ctr_exit_code, pending_event_kind)

    # Defensive fallthrough — unreachable since `current` is in _KNOWN_STATUSES.
    return ComputedRow(current, False, (), None)


def _compute_from_running(
    ctr_state: str,
    ctr_exit_code: int | None,
    event: str | None,
) -> ComputedRow:
    # Terminal events first. An explicit `done`/`fail` from Pi wins over the
    # container's current state, because the event is the operator's signal of
    # intent — the container might still be tearing down.
    if event == "done":
        return ComputedRow("completed", True, (), None)
    if event == "fail":
        return ComputedRow("failed", True, (), None)

    if ctr_state == "running":
        if event == "wait":
            return ComputedRow("waiting_for_user", True, (), None)
        return ComputedRow("running", False, (), None)

    if ctr_state == "exited":
        if ctr_exit_code == 0:
            # Clean POSIX exit without a `done` event: absence of intent →
            # interrupted, not auto-completed. Operator must finish/recover.
            return ComputedRow("interrupted", True, (), None)
        return ComputedRow("failed", True, (), None)

    if ctr_state == "notfound":
        return ComputedRow("interrupted", True, (), None)

    if ctr_state in _INTERRUPTED_CTR_STATES:
        return ComputedRow("interrupted", True, (), None)

    return ComputedRow("running", False, (), None)


def _compute_from_created(
    ctr_state: str,
    ctr_exit_code: int | None,
    event: str | None,
) -> ComputedRow:
    # Terminal events first, same precedence as _compute_from_running: if the
    # controller crashed between containers.run and the to-running write, Pi
    # may have still emitted `done`/`fail` against the live container while
    # disk says `created`. Honoring the event here keeps the terminal signal
    # from being silently consumed when list_cmd advances events_offset.
    if event == "done":
        return ComputedRow("completed", True, (), None)
    if event == "fail":
        return ComputedRow("failed", True, (), None)
    if ctr_state == "running":
        # Controller crashed between containers.run and the to-running write.
        # Flip forward; container is genuinely up.
        return ComputedRow("running", True, (), None)
    if ctr_state == "notfound":
        return ComputedRow(
            "failed",
            True,
            (),
            "reconcile: container missing while status=created (controller crash?)",
        )
    # Any other container state from `created` means the daemon allocated the
    # container but it never reached `running` — treat as failed start.
    return ComputedRow("failed", True, (), None)


def _compute_from_waiting(
    ctr_state: str,
    ctr_exit_code: int | None,
    event: str | None,
) -> ComputedRow:
    if event == "done":
        return ComputedRow("completed", True, (), None)
    if event == "fail":
        return ComputedRow("failed", True, (), None)

    if ctr_state == "running":
        # `wait` events are idempotent while already waiting; no transition.
        return ComputedRow("waiting_for_user", False, (), None)
    if ctr_state == "exited":
        if ctr_exit_code == 0:
            return ComputedRow("interrupted", True, (), None)
        return ComputedRow("failed", True, (), None)
    if ctr_state == "notfound":
        return ComputedRow("interrupted", True, (), None)
    if ctr_state in _INTERRUPTED_CTR_STATES:
        return ComputedRow("interrupted", True, (), None)
    return ComputedRow("waiting_for_user", False, (), None)
