"""Pytest wrapper around tests/smoke/run-image-smoke.sh (D-33).

Bash for the actual interaction (visible in plain text), pytest for the assertion frame.
"""

import os
import subprocess
import sys


def test_image_smoke():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script = os.path.join(repo_root, "tests", "smoke", "run-image-smoke.sh")
    result = subprocess.run(
        ["bash", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    # Print on failure for debuggability.
    if result.returncode != 0:
        sys.stderr.write("\n=== run-image-smoke.sh stdout ===\n" + result.stdout)
        sys.stderr.write("\n=== run-image-smoke.sh stderr ===\n" + result.stderr)
    assert result.returncode == 0, f"image smoke failed (exit={result.returncode})"
