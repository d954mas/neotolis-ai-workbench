"""Tests for disk.py (`naiw-tasks disk` subcommand + threshold helper)."""

import subprocess
from pathlib import Path

from naiw_tasks import disk
from naiw_tasks.config import Config


def _seed_naiw_data(root: Path, sizes: dict[str, int]) -> None:
    for name in (
        "projects.yaml", "secrets", "pi-packages", "workspace/repos", "tasks",
    ):
        p = root / name
        if name == "projects.yaml":
            p.write_bytes(b"x" * sizes.get(name, 0))
        else:
            p.mkdir(parents=True, exist_ok=True)
            if sizes.get(name, 0):
                (p / "f").write_bytes(b"x" * sizes[name])


def test_disk_per_subdir_breakdown(tmp_path, capsys):
    _seed_naiw_data(
        tmp_path,
        {
            "projects.yaml": 100,
            "secrets": 200,
            "pi-packages": 1024,
            "workspace/repos": 2048,
            "tasks": 4096,
        },
    )
    cfg = Config(data_root=tmp_path)
    rc = disk.run(cfg)
    assert rc == 0
    out = capsys.readouterr().out
    for name in (
        "projects.yaml", "secrets", "pi-packages",
        "workspace/repos", "tasks", "TOTAL",
    ):
        assert name in out
    # Locked order: each name appears once in the listed order.
    positions = [
        out.index(name)
        for name in (
            "projects.yaml", "secrets", "pi-packages",
            "workspace/repos", "tasks",
        )
    ]
    assert positions == sorted(positions)


def test_disk_includes_host_free_line(tmp_path, capsys):
    _seed_naiw_data(tmp_path, {})
    cfg = Config(data_root=tmp_path)
    disk.run(cfg)
    out = capsys.readouterr().out
    assert "host disk" in out
    assert "total=" in out and "used=" in out and "free=" in out


def test_disk_warns_at_80_percent_threshold(tmp_path, capsys):
    # 1 MiB max; write ~900 KiB into tasks/ so >80% triggers.
    _seed_naiw_data(tmp_path, {"tasks": 900 * 1024})
    cfg = Config(data_root=tmp_path, max_data_size=1024 * 1024)
    disk.run(cfg)
    out = capsys.readouterr().out
    assert "WARNING:" in out
    assert "naiw-tasks clean --older-than 30d" in out


def test_du_partial_marks_row_partial(tmp_path, capsys, monkeypatch):
    # Only the "secrets" subdir's du call returns non-zero; others must
    # report clean numeric sizes without "(partial)". This makes the
    # assertion specific instead of "any output has (partial)".
    _seed_naiw_data(
        tmp_path,
        {
            "projects.yaml": 50,
            "secrets": 200,
            "pi-packages": 100,
            "workspace/repos": 100,
            "tasks": 100,
        },
    )
    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        target_path = argv[2] if len(argv) >= 3 else ""
        # Only fail for the secrets/ subdir.
        if target_path.endswith("secrets") or target_path.endswith("secrets/"):
            class R:
                returncode = 1
                stdout = "200\t" + target_path + "\n"
                stderr = "du: cannot access ...: Permission denied"

            return R()
        # Delegate everything else to the real du so other rows are clean.
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = Config(data_root=tmp_path)
    disk.run(cfg)
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    secrets_lines = [
        ln for ln in lines if "secrets" in ln and "(partial)" in ln
    ]
    clean_lines = [
        ln
        for ln in lines
        if any(
            n in ln
            for n in ("projects.yaml", "pi-packages", "workspace/repos", "tasks")
        )
        and "(partial)" not in ln
    ]
    assert secrets_lines, (
        f"expected 'secrets' row with (partial), got: {lines!r}"
    )
    assert clean_lines, (
        f"expected at least one non-partial row, got: {lines!r}"
    )
    # Bug 6 regression: partial-row count must surface as a loud NOTE so
    # the operator does not silently miss that TOTAL undercounts.
    assert "NOTE: 1 subdir(s) returned partial size" in out, (
        f"partial run must emit a loud NOTE; got: {out!r}"
    )
    assert "secrets" in out.split("NOTE:", 1)[1], (
        f"NOTE must name the partial subdir(s); got: {out!r}"
    )
    assert "TOTAL may understate" in out


def test_no_partial_means_no_note(tmp_path, capsys):
    _seed_naiw_data(tmp_path, {"tasks": 100})
    cfg = Config(data_root=tmp_path)
    disk.run(cfg)
    out = capsys.readouterr().out
    assert "NOTE:" not in out, (
        f"all-clean run must not emit a partial NOTE; got: {out!r}"
    )


def test_check_threshold_returns_used_max_pct(tmp_path):
    _seed_naiw_data(tmp_path, {"tasks": 500 * 1024})
    cfg = Config(data_root=tmp_path, max_data_size=1024 * 1024)
    used, mx, pct = disk.threshold(cfg)
    assert mx == 1024 * 1024
    assert used > 0
    assert 0 < pct < 100
