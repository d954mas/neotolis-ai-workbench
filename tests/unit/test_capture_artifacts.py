"""Tests for lifecycle._capture_artifacts (finish-time artifact bundle).

Wave 0 stubs — populated by the finish-artifacts implementation downstream.
"""

import pytest


@pytest.mark.xfail(reason="_capture_artifacts not implemented yet", strict=False)
def test_capture_writes_all_six_artifacts_for_project_task():
    raise NotImplementedError


@pytest.mark.xfail(reason="_capture_artifacts not implemented yet", strict=False)
def test_generic_task_skips_git_artifacts():
    raise NotImplementedError


@pytest.mark.xfail(reason="_capture_artifacts not implemented yet", strict=False)
def test_capture_refuses_pi_planted_symlink():
    raise NotImplementedError


@pytest.mark.xfail(reason="_capture_artifacts not implemented yet", strict=False)
def test_output_capture_uses_hardlink_when_same_filesystem():
    raise NotImplementedError


@pytest.mark.xfail(reason="_capture_artifacts not implemented yet", strict=False)
def test_output_capture_falls_back_to_copy_on_exdev():
    raise NotImplementedError


@pytest.mark.xfail(reason="_capture_artifacts not implemented yet", strict=False)
def test_capture_failure_does_not_block_teardown():
    raise NotImplementedError
