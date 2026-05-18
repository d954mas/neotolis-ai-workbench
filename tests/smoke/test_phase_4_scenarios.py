"""Pytest wrapper around tests/smoke/run-phase-4-scenarios.sh.

Drives the four Phase 4 live-Docker operator-gate scenarios non-interactively.
Bash does the docker+tmux work; this wrapper provides the pytest assertion
frame so `pytest tests/smoke/ -m smoke` covers Phase 4 wire-level evidence
alongside the existing image / hardened / containerized harnesses.

Linux-only — the harness exits 0 with `[phase-4-scenarios] SKIP:` on
non-Linux hosts (no Docker bind-mount semantics) and we translate that into
a real pytest skip.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "smoke" / "run-phase-4-scenarios.sh"


@pytest.mark.smoke
@pytest.mark.linux_only
def test_phase_4_scenarios():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH")
    result = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if "[phase-4-scenarios] SKIP" in result.stdout:
        skip_line = next(
            (line for line in result.stdout.splitlines() if "[phase-4-scenarios] SKIP" in line),
            "[phase-4-scenarios] SKIP",
        )
        pytest.skip(skip_line.strip())
    if result.returncode != 0:
        sys.stderr.write("\n=== run-phase-4-scenarios.sh stdout ===\n" + result.stdout)
        sys.stderr.write("\n=== run-phase-4-scenarios.sh stderr ===\n" + result.stderr)
    assert result.returncode == 0, f"phase-4 scenarios failed (exit={result.returncode})"
