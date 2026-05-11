"""Pytest wrapper around tests/smoke/run-hardened-smoke.sh.

Bash does all the docker run/exec/curl work; pytest provides the assertion
frame so a single `pytest -q` covers smoke + unit + hardened smoke together.

The bash gate is Linux-only (it requires Linux-FS-backed bind-mounts and the
kernel's mount-namespace semantics for the hardened lifecycle assertions). On
non-Linux hosts the gate prints `[hardened-smoke] SKIP: …` and exits 0; this
wrapper translates that into a pytest skip so CI on non-Linux runners is honest.
"""

import os
import subprocess
import sys

import pytest


@pytest.mark.smoke
@pytest.mark.linux_only
def test_hardened_smoke():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script = "tests/smoke/run-hardened-smoke.sh"
    result = subprocess.run(
        ["bash", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    # The gate exits 0 with a `[hardened-smoke] SKIP:` marker on stdout when its
    # preconditions aren't met (non-Linux host, /mnt/c-backed $HOME, missing
    # docker, missing compose v2). Translate that into a real pytest skip rather
    # than masquerading as PASS.
    if "[hardened-smoke] SKIP" in result.stdout:
        skip_line = next(
            (line for line in result.stdout.splitlines() if "[hardened-smoke] SKIP" in line),
            "[hardened-smoke] SKIP",
        )
        pytest.skip(skip_line.strip())
    # Print on failure for debuggability.
    if result.returncode != 0:
        sys.stderr.write("\n=== run-hardened-smoke.sh stdout ===\n" + result.stdout)
        sys.stderr.write("\n=== run-hardened-smoke.sh stderr ===\n" + result.stderr)
    assert result.returncode == 0, f"hardened smoke failed (exit={result.returncode})"
