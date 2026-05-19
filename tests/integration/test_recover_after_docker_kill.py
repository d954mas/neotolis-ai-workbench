"""Scenario 3: recover after docker kill.

Simulates a daemon-driven interruption (`docker kill --signal=SIGKILL`),
verifies list reconciles to interrupted, then recover spins a fresh
container with the recovery banner and bumps recovery_count.
"""

import pytest

pytestmark = pytest.mark.integration


def test_recover_after_docker_kill(compose_stack, run_naiw_tasks):
    pytest.skip("Implemented in Task 2")
