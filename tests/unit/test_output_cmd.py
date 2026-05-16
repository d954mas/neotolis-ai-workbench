"""Tests for naiw_tasks.output_cmd — host-side terminal.log tail.

Defends against symlink/fifo at io/terminal.log via O_NOFOLLOW + S_ISREG;
validates task id shape at the function boundary so direct callers cannot
bypass the regex check.
"""

import os
import re
from pathlib import Path

import click
import pytest
from naiw_tasks.config import Config

from naiw_tasks import output_cmd


def _cfg(data_root: Path) -> Config:
    return Config(data_root=data_root)


def _make_task_dir(data_root: Path, task_id: str) -> Path:
    task_dir = data_root / "tasks" / task_id
    (task_dir / "io").mkdir(parents=True, exist_ok=True)
    (task_dir / "meta").mkdir(parents=True, exist_ok=True)
    return task_dir


def _capture(capsys) -> tuple[str, str]:
    out_err = capsys.readouterr()
    return (out_err.out, out_err.err)


# ---------- default behaviour -----------------------------------------------


def test_output_reads_last_200_lines_by_default(tmp_naiw_data, capsys):
    task_dir = _make_task_dir(tmp_naiw_data, "task-001")
    log = "".join(f"line {i}\n" for i in range(1, 501))
    (task_dir / "io" / "terminal.log").write_text(log, encoding="utf-8")

    output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=200)
    out, _ = _capture(capsys)
    assert "line 500\n" in out
    assert "line 301\n" in out
    assert "line 300\n" not in out
    assert "line 1\n" not in out


def test_output_supports_lines_N_override(tmp_naiw_data, capsys):
    task_dir = _make_task_dir(tmp_naiw_data, "task-001")
    log = "".join(f"line {i}\n" for i in range(1, 501))
    (task_dir / "io" / "terminal.log").write_text(log, encoding="utf-8")

    output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=5)
    out, _ = _capture(capsys)
    for i in range(496, 501):
        assert f"line {i}\n" in out
    assert "line 495\n" not in out


def test_output_short_file_returns_all_lines(tmp_naiw_data, capsys):
    task_dir = _make_task_dir(tmp_naiw_data, "task-001")
    (task_dir / "io" / "terminal.log").write_text(
        "a\nb\nc\n", encoding="utf-8"
    )
    output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=200)
    out, _ = _capture(capsys)
    assert out == "a\nb\nc\n"


# ---------- --lines 0 usage error ------------------------------------------


def test_output_lines_zero_raises_usage_error(tmp_naiw_data):
    _make_task_dir(tmp_naiw_data, "task-001")
    with pytest.raises(click.UsageError):
        output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=0)


def test_output_negative_lines_raises_usage_error(tmp_naiw_data):
    _make_task_dir(tmp_naiw_data, "task-001")
    with pytest.raises(click.UsageError):
        output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=-1)


# ---------- direct-caller task_id validation -------------------------------


def test_output_run_rejects_invalid_task_id_shape(tmp_naiw_data):
    """First-statement validate_task_id defense: direct callers cannot bypass
    the regex check by skipping the CLI veneer. Path-shaped ids must raise
    ValueError before any FS op."""
    with pytest.raises(ValueError):
        output_cmd.run(_cfg(tmp_naiw_data), task_id="..bad/path", lines=200)


def test_output_run_rejects_uppercase_task_id(tmp_naiw_data):
    with pytest.raises(ValueError):
        output_cmd.run(_cfg(tmp_naiw_data), task_id="BadID", lines=200)


# ---------- missing terminal.log (D-16) -------------------------------------


def test_output_missing_terminal_log_returns_empty_stdout_and_stderr_warning(
    tmp_naiw_data, capsys
):
    _make_task_dir(tmp_naiw_data, "task-001")
    # No terminal.log created.
    output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=200)
    out, err = _capture(capsys)
    assert out == ""
    assert "terminal.log not yet written for task task-001" in err


# ---------- missing task dir (D-16) ----------------------------------------


def test_output_missing_task_dir_raises_exit_1(tmp_naiw_data, capsys):
    with pytest.raises(SystemExit) as exc_info:
        output_cmd.run(_cfg(tmp_naiw_data), "nonexistent-001", lines=200)
    assert exc_info.value.code == 1
    _, err = _capture(capsys)
    assert "task 'nonexistent-001' not found" in err


# ---------- UTF-8 errors='replace' (CTRL-08) -------------------------------


def test_output_decodes_invalid_utf8_with_replace(tmp_naiw_data, capsys):
    task_dir = _make_task_dir(tmp_naiw_data, "task-001")
    (task_dir / "io" / "terminal.log").write_bytes(b"hello \xff world\n")
    output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=10)
    out, _ = _capture(capsys)
    # Unicode replacement char on the bad byte; no UnicodeDecodeError.
    assert "hello � world\n" in out


# ---------- O_NOFOLLOW symlink defense -------------------------------------


def test_output_refuses_symlink_at_terminal_log(tmp_naiw_data, tmp_path, capsys):
    task_dir = _make_task_dir(tmp_naiw_data, "task-001")
    target = tmp_path / "leaked.txt"
    target.write_text("secret\n", encoding="utf-8")
    log_path = task_dir / "io" / "terminal.log"
    try:
        log_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this filesystem/platform")

    # Should refuse — either OSError on os.open with O_NOFOLLOW, or
    # SystemExit(1) after the lstat+S_ISREG guard.
    raised = False
    try:
        output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=10)
    except (OSError, SystemExit):
        raised = True

    out, _ = _capture(capsys)
    assert "secret" not in out, "symlink target content leaked to stdout"
    assert raised, "expected OSError or SystemExit when terminal.log is a symlink"


def test_output_refuses_fifo_at_terminal_log(tmp_naiw_data, capsys):
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo unavailable on this platform")
    task_dir = _make_task_dir(tmp_naiw_data, "task-001")
    try:
        os.mkfifo(str(task_dir / "io" / "terminal.log"))
    except OSError:
        pytest.skip("mkfifo failed on this filesystem")

    raised = False
    try:
        output_cmd.run(_cfg(tmp_naiw_data), "task-001", lines=10)
    except (OSError, SystemExit):
        raised = True
    assert raised, "expected refusal when terminal.log is a fifo"


def test_output_source_uses_O_NOFOLLOW():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "output_cmd.py"
    ).read_text(encoding="utf-8")
    assert "O_NOFOLLOW" in src


def test_output_source_uses_S_ISREG():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "output_cmd.py"
    ).read_text(encoding="utf-8")
    assert "S_ISREG" in src


# ---------- no Docker call (proxy EXEC=0) -----------------------------------


def test_output_does_not_call_docker(tmp_naiw_data, capsys):
    """output_cmd.run signature must not take a docker client. Host-side only."""
    import inspect

    sig = inspect.signature(output_cmd.run)
    for param in sig.parameters.values():
        assert param.name != "client", (
            "output_cmd.run must NOT take a docker client — host-side only"
        )


# ---------- module hygiene --------------------------------------------------


def test_output_cmd_module_has_no_gsd_refs():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "output_cmd.py"
    ).read_text(encoding="utf-8")
    bad = re.search(
        r"\bD-[0-9]+|\bPhase [0-9]+|\bPlan [0-9]+|\bRESEARCH\b|"
        r"\bCTRL-[0-9]+|\bHARD-[0-9]+|\bDATA-[0-9]+|\bGIT-[0-9]+|"
        r"\bPROJ-[0-9]+|\bPROXY-[0-9]+|\bIMG-[0-9]+|\bSIG-[0-9]+|"
        r"\bLIST-[0-9]+",
        src,
    )
    assert bad is None, (
        f"forbidden token in output_cmd.py: {bad.group(0) if bad else None}"
    )
