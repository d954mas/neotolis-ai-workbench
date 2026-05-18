"""Tests for lifecycle.recover and recover-race serialization.

Wave 0 stubs — populated by the recover() implementation downstream.
Each test is xfail-strict=False so the downstream agent can fill in
real assertions without first removing the xfail decorator.
"""

import pytest


@pytest.mark.xfail(reason="recover() not implemented yet", strict=False)
def test_recover_refuses_non_interrupted_status():
    raise NotImplementedError


@pytest.mark.xfail(reason="recover() not implemented yet", strict=False)
def test_recovery_banner_appended_with_canonical_shape():
    raise NotImplementedError


@pytest.mark.xfail(reason="recover() not implemented yet", strict=False)
@pytest.mark.parametrize(
    "marker",
    [
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        None,
    ],
)
def test_recovery_banner_surfaces_git_state(marker):
    raise NotImplementedError


@pytest.mark.xfail(reason="recover() not implemented yet", strict=False)
def test_recover_never_auto_resets_worktree():
    raise NotImplementedError


@pytest.mark.xfail(reason="recover() not implemented yet", strict=False)
def test_recover_reuses_labels_and_mounts():
    raise NotImplementedError


@pytest.mark.xfail(reason="recover() not implemented yet", strict=False)
def test_recover_does_not_truncate_terminal_log():
    raise NotImplementedError


@pytest.mark.xfail(reason="recover() not implemented yet", strict=False)
def test_recover_finish_race_does_not_corrupt_task_json():
    raise NotImplementedError
