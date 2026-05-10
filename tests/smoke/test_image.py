"""Pytest wrapper around tests/smoke/run-image-smoke.sh.

Bash does the actual interaction (visible in plain text); pytest provides the
assertion frame so a single `pytest -q` covers smoke + unit tests together.
"""

import os
import subprocess
import sys

import pytest


def test_image_smoke():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    # Use forward-slash relative path so bash on Windows (Git Bash / WSL) can
    # find the script regardless of the host pytest's path style. cwd is the
    # repo root, so a relative POSIX path resolves correctly in either env.
    script = "tests/smoke/run-image-smoke.sh"
    result = subprocess.run(
        ["bash", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    # Don't let environmental skips (no docker, no compose v2) masquerade as PASS:
    # the smoke script exits 0 with a `[smoke] SKIP:` marker on stdout when its
    # preconditions aren't met. Translate that into a real pytest skip.
    if "[smoke] SKIP" in result.stdout:
        skip_line = next(
            (line for line in result.stdout.splitlines() if "[smoke] SKIP" in line),
            "[smoke] SKIP",
        )
        pytest.skip(skip_line.strip())
    # Print on failure for debuggability.
    if result.returncode != 0:
        sys.stderr.write("\n=== run-image-smoke.sh stdout ===\n" + result.stdout)
        sys.stderr.write("\n=== run-image-smoke.sh stderr ===\n" + result.stderr)
    assert result.returncode == 0, f"image smoke failed (exit={result.returncode})"
