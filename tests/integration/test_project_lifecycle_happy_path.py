"""Scenario 1: project task happy path.

Full lifecycle via the wrapper: start project task -> naiw-signal done ->
list applies the event -> finish captures artifacts -> clean removes the
task folder. Exercises the Phase 4 D-12 reconcile loop AND the Phase 5
D-08 artifact-capture path through the deployed compose stack.
"""

import pytest

pytestmark = pytest.mark.integration


def test_project_lifecycle_happy_path(compose_stack, run_naiw_tasks):
    pytest.skip("Implemented in Task 2")
