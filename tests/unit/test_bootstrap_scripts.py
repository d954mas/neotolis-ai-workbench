"""Tests for the host-side bootstrap scripts.

Both scripts use bash + POSIX file operations. The tests:
- Run each script via subprocess with NAIW_DATA pointed at pytest's tmp_path.
- Assert structural invariants (directories present, regex enforcement, idempotency).
- Verify POSIX modes by querying through bash itself (the same shell that ran
  the script), so the mode check reflects what the script can actually guarantee.
- Skip when bash is not available, or when filesystem semantics make a particular
  assertion meaningless (NTFS does not enforce POSIX modes).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SCRIPT = REPO_ROOT / "scripts" / "naiw-init-data.sh"
NEW_TASK_SCRIPT = REPO_ROOT / "scripts" / "naiw-new-task.sh"


def _resolve_bash() -> str | None:
    """Locate a bash that understands native Windows paths (Git Bash / MSYS2).

    On Windows, `bash.exe` from System32 is the WSL launcher — it sees Windows
    files only via /mnt/c/, which makes a unified path-conversion strategy hard
    and is slow to spawn. Prefer Git Bash / MSYS2 when available so the same
    path style works on every host. On Linux this just returns /usr/bin/bash.
    """
    candidates = [
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\msys64\usr\bin\bash.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return shutil.which("bash")


BASH = _resolve_bash()

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="bash not available",
)


def _bash_path(p: Path) -> str:
    """Return a path bash can consume on POSIX and Git Bash / MSYS2 on Windows."""
    return p.as_posix()


def _run(script: Path, *args: str, data_root: Path) -> subprocess.CompletedProcess[str]:
    # Use _bash_path for the script too: on Git Bash on Windows, backslashes
    # in argv get interpreted as bash escape sequences and shred the path.
    env = {
        **os.environ,
        "NAIW_DATA": _bash_path(data_root),
        # Disable MSYS path mangling on Windows; harmless no-op on Linux.
        "MSYS_NO_PATHCONV": "1",
        "MSYS2_ARG_CONV_EXCL": "*",
    }
    return subprocess.run(
        [BASH, _bash_path(script), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _bash_stat_mode(path: Path) -> str:
    """Read mode bits via the same bash the scripts use (so semantics match)."""
    r = subprocess.run(
        [BASH, "-c", f"stat -c %a {_bash_path(path)!r}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip()


# ── naiw-init-data.sh ───────────────────────────────────────────────────────


def test_init_data_creates_layout(tmp_path):
    data_root = tmp_path / "naiw-data"
    r = _run(INIT_SCRIPT, data_root=data_root)
    assert r.returncode == 0, r.stderr
    for sub in ("secrets", "pi-packages", "workspace/repos", "tasks"):
        assert (data_root / sub).is_dir(), f"missing {sub}"
    assert (data_root / "projects.yaml").is_file()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="NTFS does not enforce POSIX modes; chmod 0700 is advisory only",
)
def test_init_data_secrets_mode_0700(tmp_path):
    data_root = tmp_path / "naiw-data"
    r = _run(INIT_SCRIPT, data_root=data_root)
    assert r.returncode == 0, r.stderr
    assert _bash_stat_mode(data_root / "secrets") == "700"


def test_init_data_idempotent(tmp_path):
    data_root = tmp_path / "naiw-data"
    first = _run(INIT_SCRIPT, data_root=data_root)
    second = _run(INIT_SCRIPT, data_root=data_root)
    assert first.returncode == 0
    assert second.returncode == 0, second.stderr
    # Second run should report the existing projects.yaml as preserved.
    assert "preserved existing" in second.stdout or "projects.yaml preserved" in second.stdout


def test_init_data_preserves_existing_projects_yaml(tmp_path):
    data_root = tmp_path / "naiw-data"
    data_root.mkdir()
    sentinel = "# operator-edited content — must NOT be overwritten\n"
    (data_root / "projects.yaml").write_text(sentinel, encoding="utf-8")
    r = _run(INIT_SCRIPT, data_root=data_root)
    assert r.returncode == 0, r.stderr
    assert (data_root / "projects.yaml").read_text(encoding="utf-8") == sentinel


def test_init_data_help_exits_zero(tmp_path):
    r = _run(INIT_SCRIPT, "--help", data_root=tmp_path / "unused")
    assert r.returncode == 0
    assert "Usage" in r.stdout


# ── naiw-new-task.sh ────────────────────────────────────────────────────────


def _initialised_root(tmp_path: Path) -> Path:
    """Run naiw-init-data first so naiw-new-task has its precondition met."""
    data_root = tmp_path / "naiw-data"
    r = _run(INIT_SCRIPT, data_root=data_root)
    assert r.returncode == 0, r.stderr
    return data_root


def test_new_task_creates_skeleton(tmp_path):
    data_root = _initialised_root(tmp_path)
    r = _run(NEW_TASK_SCRIPT, "alpha-001", data_root=data_root)
    assert r.returncode == 0, r.stderr
    task_dir = data_root / "tasks" / "alpha-001"
    for sub in ("meta", "work", "io", "io/.naiw"):
        assert (task_dir / sub).is_dir(), f"missing {sub}"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="NTFS does not enforce POSIX modes; chmod 1777 is advisory only",
)
def test_new_task_chmods_all_bind_sources_1777(tmp_path):
    """Every bind-mount source (work/, io/, io/.naiw/, storage/) must be
    1777 so pi uid 1000 inside the container can write regardless of the
    operator's host uid. work/ was previously omitted, blocking Pi from
    writing /work on uid-mismatched hosts for generic tasks.
    """
    data_root = _initialised_root(tmp_path)
    r = _run(NEW_TASK_SCRIPT, "alpha-002", data_root=data_root)
    assert r.returncode == 0, r.stderr
    td = data_root / "tasks" / "alpha-002"
    for sub in ("work", "io", "io/.naiw", "storage"):
        mode = (td / sub).stat().st_mode & 0o7777
        assert mode == 0o1777, (
            f"{sub} mode = {oct(mode)}, expected 0o1777"
        )
    # meta/ stays at default (host-only, never bind-mounted into container).
    meta_mode = (td / "meta").stat().st_mode & 0o7777
    assert meta_mode != 0o1777, "meta/ must NOT be world-writable"


@pytest.mark.parametrize(
    "bad_id",
    [
        "Foo",          # uppercase
        "_leading",     # leading underscore
        "-leading",     # leading hyphen
        "trailing-",    # trailing hyphen (DNS-label style forbids it)
        "with/slash",   # path separator
        "has space",    # whitespace
        "x" * 65,       # too long (max 64)
        "",             # empty (handled as "no arg" by the script -> exit 2)
    ],
)
def test_new_task_rejects_invalid_id(tmp_path, bad_id):
    data_root = _initialised_root(tmp_path)
    r = _run(NEW_TASK_SCRIPT, bad_id, data_root=data_root)
    assert r.returncode != 0, f"expected non-zero exit for {bad_id!r}, got 0; stdout={r.stdout!r}"


def test_new_task_refuses_overwrite(tmp_path):
    data_root = _initialised_root(tmp_path)
    first = _run(NEW_TASK_SCRIPT, "dup-id", data_root=data_root)
    second = _run(NEW_TASK_SCRIPT, "dup-id", data_root=data_root)
    assert first.returncode == 0
    assert second.returncode == 1, second.stderr
    assert "already exists" in second.stderr


def test_new_task_requires_initialised_data_root(tmp_path):
    # Don't run naiw-init-data first — the script must refuse.
    data_root = tmp_path / "naiw-data-uninit"
    r = _run(NEW_TASK_SCRIPT, "alpha", data_root=data_root)
    assert r.returncode == 1
    assert "data root not found" in r.stderr


def test_new_task_no_arg_exits_two(tmp_path):
    data_root = _initialised_root(tmp_path)
    r = _run(NEW_TASK_SCRIPT, data_root=data_root)
    assert r.returncode == 2


def test_new_task_help_exits_zero(tmp_path):
    r = _run(NEW_TASK_SCRIPT, "--help", data_root=tmp_path / "unused")
    assert r.returncode == 0
    assert "Usage" in r.stdout
