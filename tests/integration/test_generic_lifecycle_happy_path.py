"""Scenario 2: generic task happy path.

Same as scenario 1 but without the project alias arg — no worktree, no
agent/ branch, terminal.log still captured.
"""

import pytest

pytestmark = pytest.mark.integration


def test_generic_lifecycle_happy_path(compose_stack, run_naiw_tasks):
    pytest.skip("Implemented in Task 2")
