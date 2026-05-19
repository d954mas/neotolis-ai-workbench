"""Scenario 5: doctor surfaces drift.

Starts a project task, mutates the live container via `docker update
--pids-limit=0` (simulating "daemon lost a hardening flag"), then asserts
`naiw-tasks list` surfaces `(drift)` AND `naiw-tasks doctor <id>` prints
a PidsLimit diff and exits 1.
"""

import pytest

pytestmark = pytest.mark.integration


def test_doctor_surfaces_drift(compose_stack, run_naiw_tasks):
    pytest.skip("Implemented in Task 2")
