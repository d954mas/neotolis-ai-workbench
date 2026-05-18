"""Tests for naiw_tasks.startup_checks — four fatal probes that gate every CLI call.

docker is monkey-imported lazily so these tests can run even when docker SDK's
real socket is unreachable. The Linux symlink test is skipped on Windows.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import naiw_tasks.startup_checks as startup_checks
import pytest
from naiw_tasks.startup_checks import (
    StartupCheckFailed,
    check_docker_reachable,
    check_naiw_data_not_symlink,
    check_not_on_windows_fs_on_linux,
    check_proxy_allowlist_drift,
    run_all,
)

# ---------------------------------------------------------------------------
# StartupCheckFailed
# ---------------------------------------------------------------------------


def test_startup_check_failed_writes_naiw_tasks_prefix_to_stderr(capsys):
    with pytest.raises(SystemExit) as excinfo:
        raise StartupCheckFailed("hello world")

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == "naiw-tasks: hello world\n"


# ---------------------------------------------------------------------------
# check_naiw_data_not_symlink
# ---------------------------------------------------------------------------


def test_check_naiw_data_not_symlink_passes_for_real_dir(tmp_naiw_data):
    # No raise = success
    assert check_naiw_data_not_symlink(tmp_naiw_data) is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics differ on Windows")
def test_check_naiw_data_not_symlink_rejects_symlink(tmp_path, capsys):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "naiw-data"
    link.symlink_to(target)

    with pytest.raises(SystemExit) as excinfo:
        check_naiw_data_not_symlink(link)

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("naiw-tasks: ")
    assert "must not be a symlink" in captured.err
    assert str(target) in captured.err


# ---------------------------------------------------------------------------
# check_not_on_windows_fs_on_linux
# ---------------------------------------------------------------------------


def test_check_not_on_windows_fs_skips_on_non_linux(tmp_naiw_data, monkeypatch):
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Darwin"
    )
    # Even though resolved path may include /mnt/c/, non-Linux returns immediately.
    assert check_not_on_windows_fs_on_linux(tmp_naiw_data) is None


def test_check_not_on_windows_fs_passes_for_normal_linux_path(tmp_naiw_data, monkeypatch):
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Linux"
    )
    # tmp_naiw_data is under tmp_path, not /mnt/<letter>/
    assert check_not_on_windows_fs_on_linux(tmp_naiw_data) is None


@pytest.mark.parametrize(
    "windows_fs_path",
    [
        "/mnt/c/Users/foo/naiw-data",
        "/mnt/C/Users/foo/naiw-data",
        "/mnt/d/data/naiw-data",
        "/mnt/e/foo",
        "/mnt/z/share/x",
    ],
)
def test_check_not_on_windows_fs_rejects_all_mnt_letter_paths_on_linux(
    windows_fs_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Linux"
    )

    class FakeDataRoot:
        def is_symlink(self) -> bool:
            return False

        def resolve(self) -> Path:
            return Path(windows_fs_path)

    with pytest.raises(SystemExit) as excinfo:
        check_not_on_windows_fs_on_linux(FakeDataRoot())

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert windows_fs_path in captured.err
    assert "is not supported" in captured.err


@pytest.mark.parametrize(
    "linux_fs_path",
    [
        "/home/user/naiw-data",
        "/opt/naiw-data",
        # /mntfoo/ — different parent, not WSL mount.
        "/mntfoo/x",
        # /mnt/multi-letter — not a single drive letter, not WSL mount.
        "/mnt/ab/x",
    ],
)
def test_check_not_on_windows_fs_passes_for_lookalike_paths(
    linux_fs_path, monkeypatch
):
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Linux"
    )

    class FakeDataRoot:
        def is_symlink(self) -> bool:
            return False

        def resolve(self) -> Path:
            return Path(linux_fs_path)

    assert check_not_on_windows_fs_on_linux(FakeDataRoot()) is None


# ---------------------------------------------------------------------------
# NAIW_ACCEPT_WINDOWS_FS_RISK ack semantics
# ---------------------------------------------------------------------------


def test_winfs_check_fatal_without_ack(monkeypatch, capsys, tmp_path):
    """Missing/unset ack on a /mnt/<letter>/ path -> fatal exit 2 with
    opt-in hint."""
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Linux"
    )
    monkeypatch.delenv("NAIW_ACCEPT_WINDOWS_FS_RISK", raising=False)

    class FakeRoot:
        def is_symlink(self): return False
        def resolve(self): return Path("/mnt/c/Users/foo/naiw-data")

    with pytest.raises(SystemExit) as exc:
        check_not_on_windows_fs_on_linux(FakeRoot())
    assert exc.value.code == 2

    err = capsys.readouterr().err
    assert "/mnt/c/Users/foo/naiw-data" in err
    assert "NAIW_ACCEPT_WINDOWS_FS_RISK" in err
    assert "set NAIW_ACCEPT_WINDOWS_FS_RISK=1" in err  # opt-in hint


def test_winfs_check_warn_with_ack(monkeypatch, capsys, tmp_path):
    """Ack set + marker absent -> WARNING printed; returns None; marker created."""
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Linux"
    )
    monkeypatch.setenv("NAIW_ACCEPT_WINDOWS_FS_RISK", "1")
    marker = tmp_path / ".naiw-winfs-acked"

    class FakeRoot:
        def is_symlink(self): return False
        def resolve(self): return Path("/mnt/c/Users/foo/naiw-data")
        def __truediv__(self, name): return marker

    assert check_not_on_windows_fs_on_linux(FakeRoot()) is None

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "NAIW_ACCEPT_WINDOWS_FS_RISK=1" in err
    assert "/mnt/c/Users/foo/naiw-data" in err
    assert marker.exists()


def test_winfs_warn_marker_suppression(monkeypatch, capsys, tmp_path):
    """Ack set + marker pre-exists -> returns None; stderr empty (suppressed)."""
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Linux"
    )
    monkeypatch.setenv("NAIW_ACCEPT_WINDOWS_FS_RISK", "1")
    marker = tmp_path / ".naiw-winfs-acked"
    marker.touch()  # pre-existing marker

    class FakeRoot:
        def is_symlink(self): return False
        def resolve(self): return Path("/mnt/c/Users/foo/naiw-data")
        def __truediv__(self, name): return marker

    assert check_not_on_windows_fs_on_linux(FakeRoot()) is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("ack_value", ["true", "yes", "0", " 1", "1 ", "TRUE", ""])
def test_winfs_check_only_exact_one_acks(monkeypatch, ack_value, tmp_path):
    """Exact-match semantics: only the literal string '1' enables the ack.
    Truthy-looking strings (true/yes), padded values, and even the empty string
    must NOT bypass the fatal branch."""
    monkeypatch.setattr(
        "naiw_tasks.startup_checks.platform.system", lambda: "Linux"
    )
    monkeypatch.setenv("NAIW_ACCEPT_WINDOWS_FS_RISK", ack_value)

    class FakeRoot:
        def is_symlink(self): return False
        def resolve(self): return Path("/mnt/d/data/naiw-data")

    with pytest.raises(SystemExit):
        check_not_on_windows_fs_on_linux(FakeRoot())


# ---------------------------------------------------------------------------
# check_docker_reachable
# ---------------------------------------------------------------------------


def test_check_docker_reachable_passes():
    fake_client = SimpleNamespace(
        containers=SimpleNamespace(list=lambda **kwargs: [])
    )
    assert check_docker_reachable(fake_client, "tcp://127.0.0.1:2375") is None


def test_check_docker_reachable_rejects_on_exception(capsys):
    import docker

    def raise_docker(**kwargs):
        raise docker.errors.DockerException("connection refused")

    fake_client = SimpleNamespace(containers=SimpleNamespace(list=raise_docker))

    with pytest.raises(SystemExit) as excinfo:
        check_docker_reachable(
            fake_client, "tcp://127.0.0.1:2375", attempts=2, delay_s=0
        )

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "cannot reach Docker via proxy at tcp://127.0.0.1:2375" in captured.err


def test_check_docker_reachable_retries_before_failing(monkeypatch):
    import docker

    calls = {"count": 0}

    def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise docker.errors.DockerException("proxy warming up")
        return []

    fake_client = SimpleNamespace(containers=SimpleNamespace(list=flaky))

    assert (
        check_docker_reachable(
            fake_client, "tcp://127.0.0.1:2375", attempts=3, delay_s=0
        )
        is None
    )
    assert calls["count"] == 3


# ---------------------------------------------------------------------------
# check_proxy_allowlist_drift
# ---------------------------------------------------------------------------


def test_check_proxy_allowlist_drift_passes_when_proxy_contract_matches(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and "/exec/naiw-probe-" in url and url.endswith("/start"):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if method == "POST" and (
            ("/containers/naiw-probe-" in url and url.endswith("/start"))
            or ("/containers/naiw-probe-" in url and url.endswith("/stop"))
        ):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        raise AssertionError(f"unexpected probe: {method} {url}")

    monkeypatch.setattr(
        "naiw_tasks.startup_checks.urllib.request.urlopen", fake_urlopen
    )
    assert check_proxy_allowlist_drift("tcp://127.0.0.1:2375") is None


def test_check_proxy_allowlist_drift_rejects_when_allowed_container_op_is_403(
    monkeypatch, capsys
):
    import urllib.error

    def fake_urlopen(req, timeout):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and "/exec/naiw-probe-" in url and url.endswith("/start"):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if (
            method == "POST"
            and "/containers/naiw-probe-" in url
            and url.endswith("/start")
        ):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        raise AssertionError(f"unexpected probe: {method} {url}")

    monkeypatch.setattr(
        "naiw_tasks.startup_checks.urllib.request.urlopen", fake_urlopen
    )

    with pytest.raises(SystemExit) as excinfo:
        check_proxy_allowlist_drift("tcp://127.0.0.1:2375")

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "proxy allowlist drift" in captured.err
    assert "POST /containers/naiw-probe-" in captured.err
    assert "expected 404" in captured.err


def test_check_proxy_allowlist_drift_rejects_when_200(monkeypatch, capsys):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a, **kw):
            return b""

    monkeypatch.setattr(
        "naiw_tasks.startup_checks.urllib.request.urlopen",
        lambda req, timeout: FakeResp(),
    )

    with pytest.raises(SystemExit) as excinfo:
        check_proxy_allowlist_drift("tcp://127.0.0.1:2375")

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "proxy allowlist drift" in captured.err
    assert "EXEC=0" in captured.err


def test_check_proxy_allowlist_drift_rejects_when_other_http_error(monkeypatch, capsys):
    import urllib.error

    def raise_500(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

    monkeypatch.setattr(
        "naiw_tasks.startup_checks.urllib.request.urlopen", raise_500
    )

    with pytest.raises(SystemExit) as excinfo:
        check_proxy_allowlist_drift("tcp://127.0.0.1:2375")

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "500" in captured.err


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------


def test_run_all_runs_in_documented_order(tmp_naiw_data, monkeypatch):
    order: list[str] = []

    monkeypatch.setattr(
        startup_checks,
        "check_naiw_data_not_symlink",
        lambda d: order.append("symlink"),
    )
    monkeypatch.setattr(
        startup_checks,
        "check_not_on_windows_fs_on_linux",
        lambda d: order.append("windows_fs"),
    )
    monkeypatch.setattr(
        startup_checks,
        "check_docker_reachable",
        lambda c, u: order.append("docker_reachable"),
    )
    monkeypatch.setattr(
        startup_checks,
        "check_proxy_allowlist_drift",
        lambda u: order.append("allowlist_drift"),
    )

    cfg = SimpleNamespace(
        data_root=tmp_naiw_data, docker_proxy_url="tcp://127.0.0.1:2375"
    )
    client = SimpleNamespace()
    run_all(cfg, client)

    # uid-mismatch warning runs at the end and is non-fatal — it just appends
    # to stderr, so it's expected to NOT appear in the order list above (the
    # other checks block monkey-patches on themselves; uid warning has no
    # specific monkey-patch in this test). We don't assert its absence; the
    # dedicated test below verifies the warn behaviour.
    assert order == ["symlink", "windows_fs", "docker_reachable", "allowlist_drift"]


# ---------------------------------------------------------------------------
# warn_uid_mismatch_with_image
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="getuid is POSIX-only")
def test_warn_uid_mismatch_emits_when_uid_not_1000(monkeypatch, capsys):
    """When operator uid != 1000 the warning surfaces with key phrases."""
    monkeypatch.setattr("naiw_tasks.startup_checks.os.getuid", lambda: 5000)

    startup_checks.warn_uid_mismatch_with_image()

    captured = capsys.readouterr()
    assert "operator uid=5000" in captured.err
    assert "pi uid=1000" in captured.err
    assert "1777" in captured.err  # mentions the chmod fix
    assert "rebuild the image" in captured.err  # points to long-term fix


@pytest.mark.skipif(sys.platform == "win32", reason="getuid is POSIX-only")
def test_warn_uid_mismatch_silent_when_uid_is_1000(monkeypatch, capsys):
    """uid 1000 is the happy case (matches image's baked pi user)."""
    monkeypatch.setattr("naiw_tasks.startup_checks.os.getuid", lambda: 1000)

    startup_checks.warn_uid_mismatch_with_image()

    captured = capsys.readouterr()
    assert captured.err == ""


def test_warn_uid_mismatch_skips_on_non_posix(monkeypatch, capsys):
    """Windows-native Python lacks os.getuid — function must early-return,
    NOT crash with AttributeError."""
    # Simulate non-POSIX: drop getuid attribute from the imported os module.
    monkeypatch.delattr("naiw_tasks.startup_checks.os.getuid", raising=False)

    startup_checks.warn_uid_mismatch_with_image()  # must not raise

    assert capsys.readouterr().err == ""


@pytest.mark.skipif(sys.platform == "win32", reason="getuid is POSIX-only")
def test_warn_uid_mismatch_is_non_fatal(monkeypatch):
    """Even when the warning fires, it returns None — must NOT raise
    StartupCheckFailed (which would block startup). The whole point of the
    warning is to inform without breaking operators on uid != 1000 hosts."""
    monkeypatch.setattr("naiw_tasks.startup_checks.os.getuid", lambda: 5000)

    # Just call — anything raised here would surface as test failure.
    result = startup_checks.warn_uid_mismatch_with_image()
    assert result is None


# ---------------------------------------------------------------------------
# Disk-threshold gate (refuses start/recover at >95% of max_data_size)
# ---------------------------------------------------------------------------


def _stub_all_other_checks(monkeypatch):
    """Stub the four pre-existing run_all checks so only the disk gate fires."""
    monkeypatch.setattr(
        startup_checks, "check_naiw_data_not_symlink", lambda d: None,
    )
    monkeypatch.setattr(
        startup_checks, "check_not_on_windows_fs_on_linux", lambda d: None,
    )
    monkeypatch.setattr(
        startup_checks,
        "check_docker_reachable",
        lambda c, url, **kw: None,
    )
    monkeypatch.setattr(
        startup_checks, "check_proxy_allowlist_drift", lambda url: None,
    )
    monkeypatch.setattr(
        startup_checks, "warn_uid_mismatch_with_image", lambda: None,
    )


def test_start_refuses_above_95_percent_data_threshold(
        tmp_path, monkeypatch, capsys,
):
    from unittest.mock import MagicMock

    from naiw_tasks.config import Config

    cfg = Config(data_root=tmp_path, max_data_size=1024)
    _stub_all_other_checks(monkeypatch)
    # Force threshold() to return >95%.
    from naiw_tasks import disk as disk_mod
    monkeypatch.setattr(
        disk_mod, "threshold",
        lambda c: (1000, 1024, 97.65),
    )
    client = MagicMock()
    with pytest.raises(SystemExit) as exc:
        startup_checks.run_all(cfg, client, gate_disk_threshold=True)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "max_data_size" in captured.err
    # `:.1f` rounds 97.65 -> 97.7 (banker's rounding -> nearest)
    assert "97.7%" in captured.err or "97.65" in captured.err
    assert "naiw-tasks clean" in captured.err


def test_disk_threshold_gate_off_by_default(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from naiw_tasks.config import Config

    cfg = Config(data_root=tmp_path, max_data_size=1024)
    _stub_all_other_checks(monkeypatch)
    from naiw_tasks import disk as disk_mod
    # Even at 99% the default-off gate doesn't raise.
    monkeypatch.setattr(
        disk_mod, "threshold",
        lambda c: (1010, 1024, 98.6),
    )
    client = MagicMock()
    # No gate flag - default-off; must not raise.
    startup_checks.run_all(cfg, client)


def test_disk_threshold_gate_passes_below_95_percent(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from naiw_tasks.config import Config

    cfg = Config(data_root=tmp_path, max_data_size=1024)
    _stub_all_other_checks(monkeypatch)
    from naiw_tasks import disk as disk_mod
    monkeypatch.setattr(
        disk_mod, "threshold",
        lambda c: (500, 1024, 48.8),
    )
    client = MagicMock()
    # Gate ON, but usage is below 95% - must not raise.
    startup_checks.run_all(cfg, client, gate_disk_threshold=True)


def test_disk_threshold_gate_recover_verb_in_message(
        tmp_path, monkeypatch, capsys,
):
    """`disk_threshold_verb="recover"` surfaces 'cannot recover' instead of
    'cannot start' in the operator-visible stderr line."""
    from unittest.mock import MagicMock

    from naiw_tasks.config import Config

    cfg = Config(data_root=tmp_path, max_data_size=1024)
    _stub_all_other_checks(monkeypatch)
    from naiw_tasks import disk as disk_mod
    monkeypatch.setattr(
        disk_mod, "threshold",
        lambda c: (1000, 1024, 97.65),
    )
    client = MagicMock()
    with pytest.raises(SystemExit) as exc:
        startup_checks.run_all(
            cfg, client,
            gate_disk_threshold=True,
            disk_threshold_verb="recover",
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "cannot recover" in err
    assert "cannot start" not in err
