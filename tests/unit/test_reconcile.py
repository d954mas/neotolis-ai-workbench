"""Unit tests for naiw_tasks.reconcile — pure truth-table evaluator.

The truth-table parametrise below is the canonical fixture: each row is one cell
of the status × container-state × pending-event matrix. Adding a row here is the
first step when expanding the matrix; the production code in reconcile.py is
expected to satisfy every row without further per-cell branching.
"""

import re
from pathlib import Path

import pytest
from naiw_tasks.reconcile import ComputedRow, compute_status

# ---------- truth-table parametrise -----------------------------------------

# Each row covers one cell of the reconciliation matrix. The fixture intentionally
# enumerates every combination explicitly so adding/removing one is a single-line
# diff and the test report names the failing cell directly.
@pytest.mark.parametrize(
    ("task_status", "ctr_state", "ctr_exit_code", "event_kind",
     "expected_status", "expected_notes", "expected_failure_reason"),
    [
        ("running", "running", None, None, "running", (), None),
        ("running", "running", None, "done", "completed", (), None),
        ("running", "running", None, "fail", "failed", (), None),
        ("running", "running", None, "wait", "waiting_for_user", (), None),
        ("running", "exited", 0, None, "interrupted", (), None),
        ("running", "exited", 0, "done", "completed", (), None),
        ("running", "exited", 137, None, "failed", (), None),
        ("running", "exited", 137, "fail", "failed", (), None),
        ("running", "notfound", None, None, "interrupted", (), None),
        ("running", "notfound", None, "done", "completed", (), None),
        ("running", "notfound", None, "fail", "failed", (), None),
        ("created", "running", None, None, "running", (), None),
        ("created", "exited", 0, None, "failed", (), None),
        ("created", "notfound", None, None, "failed", (),
            "reconcile: container missing while status=created (controller crash?)"),
        # Controller crashed between containers.run and the to-running write;
        # Pi may have emitted a terminal event against the live container
        # while task.json is still `created`. The event must win — otherwise
        # list_cmd advances events_offset past it and the signal is gone.
        ("created", "running", None, "done", "completed", (), None),
        ("created", "running", None, "fail", "failed", (), None),
        ("created", "exited", 0, "done", "completed", (), None),
        ("created", "exited", 137, "fail", "failed", (), None),
        ("created", "notfound", None, "done", "completed", (), None),
        ("created", "notfound", None, "fail", "failed", (), None),
        ("completed", "running", None, None, "completed", ("leaked ctr",), None),
        ("completed", "notfound", None, None, "completed", (), None),
        ("failed", "running", None, None, "failed", ("leaked ctr",), None),
        ("failed", "notfound", None, None, "failed", (), None),
        ("cancelled", "running", None, None, "cancelled", ("leaked ctr",), None),
        ("cancelled", "notfound", None, None, "cancelled", (), None),
        ("interrupted", "running", None, None, "interrupted", (), None),
        ("interrupted", "notfound", None, None, "interrupted", (), None),
        ("waiting_for_user", "running", None, None, "waiting_for_user", (), None),
        ("waiting_for_user", "running", None, "done", "completed", (), None),
        ("waiting_for_user", "running", None, "fail", "failed", (), None),
        ("waiting_for_user", "running", None, "wait", "waiting_for_user", (), None),
        ("waiting_for_user", "exited", 0, None, "interrupted", (), None),
        ("waiting_for_user", "notfound", None, None, "interrupted", (), None),
        ("unknown_future_status", "running", None, None, "unknown_future_status",
            ("unknown status",), None),
        ("running", "paused", None, None, "interrupted", (), None),
        ("running", "restarting", None, None, "interrupted", (), None),
        ("running", "dead", None, None, "interrupted", (), None),
        ("running", "created", None, None, "interrupted", (), None),
        ("running", "removing", None, None, "interrupted", (), None),
    ],
)
def test_compute_status_truth_table(
    task_status,
    ctr_state,
    ctr_exit_code,
    event_kind,
    expected_status,
    expected_notes,
    expected_failure_reason,
):
    result = compute_status(
        task_dict={"status": task_status},
        ctr_state=ctr_state,
        ctr_exit_code=ctr_exit_code,
        pending_event_kind=event_kind,
    )
    assert result.status == expected_status
    assert result.notes == expected_notes
    assert result.failure_reason == expected_failure_reason


# ---------- transitioned flag ------------------------------------------------


def test_compute_status_transitioned_flag_true_when_status_changes():
    result = compute_status(
        task_dict={"status": "running"},
        ctr_state="exited",
        ctr_exit_code=0,
        pending_event_kind=None,
    )
    assert result.transitioned is True
    assert result.status == "interrupted"


def test_compute_status_transitioned_flag_false_when_status_unchanged():
    result = compute_status(
        task_dict={"status": "running"},
        ctr_state="running",
        ctr_exit_code=None,
        pending_event_kind=None,
    )
    assert result.transitioned is False
    assert result.status == "running"


def test_compute_status_transitioned_flag_false_for_leaked_ctr_terminal():
    # Terminal status with a still-running container: row keeps the terminal
    # status (no transition) but flags the leak in NOTES for the operator.
    result = compute_status(
        task_dict={"status": "completed"},
        ctr_state="running",
        ctr_exit_code=None,
        pending_event_kind=None,
    )
    assert result.transitioned is False
    assert result.notes == ("leaked ctr",)


def test_compute_status_transitioned_flag_false_for_unknown_status():
    # Unknown status: forward-compat passthrough — never compute transitions
    # for a string we do not recognise.
    result = compute_status(
        task_dict={"status": "future_xyz"},
        ctr_state="running",
        ctr_exit_code=None,
        pending_event_kind=None,
    )
    assert result.transitioned is False
    assert result.notes == ("unknown status",)


# ---------- purity / dependency guards --------------------------------------

_RECONCILE_SRC = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "naiw_tasks"
    / "naiw_tasks"
    / "reconcile.py"
).read_text(encoding="utf-8")


def test_compute_status_does_not_import_docker():
    assert "import docker" not in _RECONCILE_SRC
    assert "from docker" not in _RECONCILE_SRC


def test_compute_status_does_not_import_click():
    assert "import click" not in _RECONCILE_SRC
    assert "from click" not in _RECONCILE_SRC


def test_compute_status_does_not_open_files():
    # The module is a pure evaluator; any file-IO call here would defeat the
    # whole isolation premise (and break the unit-test contract that says
    # "same inputs → same outputs, no environment").
    assert "open(" not in _RECONCILE_SRC


def test_compute_status_is_idempotent():
    first = compute_status(
        task_dict={"status": "running"},
        ctr_state="exited",
        ctr_exit_code=0,
        pending_event_kind=None,
    )
    second = compute_status(
        task_dict={"status": "running"},
        ctr_state="exited",
        ctr_exit_code=0,
        pending_event_kind=None,
    )
    assert first == second
    assert isinstance(first, ComputedRow)


# ---------- select_latest_transition_kind -----------------------------------
#
# The reconciler reacts to four event kinds: done, fail, wait, and log.
# Only done/fail/wait drive status transitions; log is status-neutral by
# contract (events_tail validates it, the offset advances past it, but
# compute_status MUST NOT transition on log). The helper below is the
# single place that picks which kind feeds compute_status.
#
# These tests lock the load-bearing invariant: a log event emitted AFTER
# a terminal kind does not mask the terminal kind. If the helper returned
# `events[-1].kind` blindly, a `[done, log]` sequence would feed "log"
# into compute_status, no transition would fire, and the task would stay
# `running` forever.


class _Ev:
    """Minimal stand-in for naiw_common.events.Event.

    The selector only reads `.kind`; using a stub here keeps these tests
    cross-platform (test_reconcile must not import the Linux-only event
    pipeline from events_tail).
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind


def test_select_latest_transition_kind_empty_returns_none():
    from naiw_tasks.reconcile import select_latest_transition_kind
    assert select_latest_transition_kind([]) is None


def test_select_latest_transition_kind_only_log_returns_none():
    from naiw_tasks.reconcile import select_latest_transition_kind
    # log is status-neutral; if nothing else is in the batch, no transition
    # should be triggered. Returning "log" here would feed it into
    # compute_status, which has no log branch — silent no-op + offset
    # advance + log lost.
    assert select_latest_transition_kind([_Ev("log")]) is None
    assert select_latest_transition_kind([_Ev("log"), _Ev("log")]) is None


def test_select_latest_transition_kind_done_followed_by_log_returns_done():
    from naiw_tasks.reconcile import select_latest_transition_kind
    # The exact scenario where Pi emits `naiw-signal done` then
    # `naiw-signal log "wrapping up"`. The terminal kind MUST win.
    assert select_latest_transition_kind(
        [_Ev("done"), _Ev("log")]
    ) == "done"


def test_select_latest_transition_kind_fail_followed_by_log_returns_fail():
    from naiw_tasks.reconcile import select_latest_transition_kind
    assert select_latest_transition_kind(
        [_Ev("fail"), _Ev("log")]
    ) == "fail"


def test_select_latest_transition_kind_wait_followed_by_log_returns_wait():
    from naiw_tasks.reconcile import select_latest_transition_kind
    assert select_latest_transition_kind(
        [_Ev("wait"), _Ev("log")]
    ) == "wait"


def test_select_latest_transition_kind_log_followed_by_done_returns_done():
    from naiw_tasks.reconcile import select_latest_transition_kind
    # Reverse of the bug scenario: log first, then done. Latest-wins
    # behaviour preserved.
    assert select_latest_transition_kind(
        [_Ev("log"), _Ev("done")]
    ) == "done"


def test_select_latest_transition_kind_done_then_fail_picks_fail():
    from naiw_tasks.reconcile import select_latest_transition_kind
    # Latest TRANSITION kind wins (existing pre-log behaviour). Operator
    # changed mind from done to fail within one batch.
    assert select_latest_transition_kind(
        [_Ev("done"), _Ev("fail")]
    ) == "fail"


def test_select_latest_transition_kind_interleaved_picks_latest_transition():
    from naiw_tasks.reconcile import select_latest_transition_kind
    # log lines interleaved with transitions: latest transition (the fail)
    # wins, log events are transparently skipped.
    assert select_latest_transition_kind(
        [_Ev("done"), _Ev("log"), _Ev("log"), _Ev("fail"), _Ev("log")]
    ) == "fail"


def test_select_latest_transition_kind_unknown_kind_is_skipped():
    from naiw_tasks.reconcile import select_latest_transition_kind
    # Defensive: an unknown kind that somehow passed events_tail
    # validation must be transparent to the selector. The selector is a
    # filter, not a validator.
    assert select_latest_transition_kind(
        [_Ev("done"), _Ev("future_kind_v2")]
    ) == "done"


def test_transition_kinds_constant_excludes_log():
    from naiw_tasks.reconcile import TRANSITION_KINDS
    # Documents the contract as a constant: log MUST NOT be in the set.
    # If a future PR adds it here, this assertion fires before the silent
    # behaviour change ships.
    assert "log" not in TRANSITION_KINDS
    assert TRANSITION_KINDS == frozenset({"done", "fail", "wait"})


# ---------- module hygiene ---------------------------------------------------


def test_reconcile_module_has_no_gsd_refs():
    bad = re.search(
        r"\bD-[0-9]+|\bPhase [0-9]+|\bPlan [0-9]+|\bRESEARCH\b|"
        r"\bCTRL-[0-9]+|\bHARD-[0-9]+|\bDATA-[0-9]+|\bGIT-[0-9]+|"
        r"\bLIST-[0-9]+|\bSIG-[0-9]+|\bPROJ-[0-9]+|\bPROXY-[0-9]+|"
        r"\bIMG-[0-9]+",
        _RECONCILE_SRC,
    )
    assert bad is None, (
        f"forbidden token in reconcile.py: {bad.group(0) if bad else None}"
    )
