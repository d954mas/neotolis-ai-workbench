"""Scenario 4: auto_finish via fail event.

Starts a task with --auto-finish, sends `naiw-signal fail` inside the
container, then runs `naiw-tasks reap` to drive the teardown path. Asserts
status=failed, artifacts captured, container gone.
"""

import pytest

pytestmark = pytest.mark.integration


def test_auto_finish_via_fail_event(compose_stack, run_naiw_tasks):
    pytest.skip("Implemented in Task 2")
