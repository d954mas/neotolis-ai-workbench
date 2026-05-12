"""Tests for naiw_tasks.attach — os.execvpe-based docker attach with label lookup."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _fake_client_with_container(
    state: str = "running",
    name: str = "naiw-task-foo-001",
) -> tuple[MagicMock, MagicMock]:
    container = MagicMock()
    container.name = name
    container.attrs = {"State": {"Status": state}}
    client = MagicMock()
    client.containers.list = MagicMock(return_value=[container])
    return client, container


def _execvpe_call(mock_execvpe: MagicMock) -> tuple[str, list[str], dict[str, str]]:
    """Unpack the positional args from the recorded execvpe call.

    Tolerant of both `execvpe("docker", argv, env)` and
    `execvpe("docker", argv=argv, env=env)` shapes.
    """
    args = mock_execvpe.call_args.args
    kwargs = mock_execvpe.call_args.kwargs
    program = args[0]
    argv = args[1] if len(args) >= 2 else kwargs["argv"]
    env = args[2] if len(args) >= 3 else kwargs["env"]
    return program, argv, env


def test_attach_calls_execvpe_when_running(mock_execvpe: MagicMock) -> None:
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="running")

    # The real os.execvpe does not return on success, so the function ends
    # with a defence-in-depth SystemExit(1) for the (impossible) post-execvpe
    # path. With execvpe mocked, that trailing exit fires — catch and ignore.
    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://127.0.0.1:2375", "foo-001")

    # POSIX argv[0] convention — program name appears as argv[0] inside the
    # new process. `-H <proxy_url>` routes the CLI through the locked proxy
    # (same path as the SDK) so docker CLI does NOT fall back to the host
    # socket.
    program, argv, env = _execvpe_call(mock_execvpe)
    assert program == "docker"
    assert argv == [
        "docker",
        "-H",
        "tcp://127.0.0.1:2375",
        "attach",
        "naiw-task-foo-001",
    ]
    # env contains the pinned API version so docker CLI does not negotiate
    # via /_ping (blocked by proxy) and does not fall back to its bundled-
    # client version (often 1.45+ on docker CLI 26, may mismatch daemon).
    from naiw_tasks.docker_client import PINNED_DOCKER_API_VERSION

    assert env.get("DOCKER_API_VERSION") == PINNED_DOCKER_API_VERSION


def test_attach_pins_docker_api_version_via_env(mock_execvpe: MagicMock) -> None:
    """Regression guard: the pinned API version MUST travel via env (not via
    argv). Modern docker CLI honors DOCKER_API_VERSION even when -H is set;
    without it, the CLI default (e.g., 1.45 on docker CLI 26) sits above the
    declared minimum and can fail against an older daemon or the proxy's
    expected paths."""
    import os as os_mod

    from naiw_tasks.attach import attach_to_task
    from naiw_tasks.docker_client import PINNED_DOCKER_API_VERSION

    client, _container = _fake_client_with_container(state="running")

    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://example:2375", "foo-001")

    _, argv, env = _execvpe_call(mock_execvpe)
    # The version is NOT smuggled through argv (CLI flag) — that would be a
    # different code path and we want a single source of truth.
    assert not any("DOCKER_API_VERSION" in arg for arg in argv)
    assert env["DOCKER_API_VERSION"] == PINNED_DOCKER_API_VERSION
    # Operator's PATH / HOME / etc. must be preserved — we do NOT replace the
    # environment, we extend it. Pick a stable widely-set var as the witness.
    for var in ("PATH",):
        if var in os_mod.environ:
            assert env.get(var) == os_mod.environ[var]


def test_attach_passes_proxy_url_via_dash_h(mock_execvpe: MagicMock) -> None:
    """The -H flag is the only thing keeping `docker attach` on the proxy. If
    it disappears or the URL is wrong, docker CLI silently falls back to
    /var/run/docker.sock — bypassing every allowlist check."""
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="running")

    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://example.internal:2375", "foo-001")

    _, argv, _ = _execvpe_call(mock_execvpe)
    assert "-H" in argv
    h_index = argv.index("-H")
    assert argv[h_index + 1] == "tcp://example.internal:2375"


def test_attach_passes_internal_dns_url_when_default_proxy_url(
    mock_execvpe: MagicMock,
) -> None:
    """Sentinel: when the caller passes config.DEFAULT_DOCKER_PROXY_URL, the
    `-H` value in the spawned docker CLI is `tcp://naiw-docker-proxy:2375`.

    Regression guard against re-introducing the host-localhost default. The
    attach module itself does not import config — but cli.py / lifecycle.py
    construct `Config` and pass `cfg.docker_proxy_url` into attach_to_task;
    this test pins the value at the wire."""
    from naiw_tasks.attach import attach_to_task

    from naiw_tasks import config

    client, _container = _fake_client_with_container(state="running")

    with pytest.raises(SystemExit):
        attach_to_task(client, config.DEFAULT_DOCKER_PROXY_URL, "foo-001")

    _, argv, _ = _execvpe_call(mock_execvpe)
    h_index = argv.index("-H")
    assert argv[h_index + 1] == "tcp://naiw-docker-proxy:2375"
    # Belt-and-braces: the default constant itself MUST be the internal DNS URL.
    assert config.DEFAULT_DOCKER_PROXY_URL == "tcp://naiw-docker-proxy:2375"


def test_attach_uses_label_list_filter(mock_execvpe: MagicMock) -> None:
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="running")

    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://127.0.0.1:2375", "foo-001")

    list_kwargs = client.containers.list.call_args.kwargs
    label_filter = list_kwargs["filters"]["label"]
    # Must be a list (not a comma-string) so docker-py builds two ?label= query
    # params and docker AND-matches them. A comma-string is silently misinterpreted.
    assert isinstance(label_filter, list), (
        f"label filter must be a list, got {type(label_filter).__name__}"
    )
    assert set(label_filter) == {"naiw.task-id=foo-001", "naiw.managed=1"}


def test_attach_calls_list_with_all_true(mock_execvpe: MagicMock) -> None:
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="running")

    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://127.0.0.1:2375", "foo-001")

    # all=True so a stopped-but-still-existing container surfaces and the
    # refusal hint fires (otherwise containers.list silently returns []).
    assert client.containers.list.call_args.kwargs["all"] is True


def test_attach_refuses_when_not_running(
    mock_execvpe: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="exited")

    with pytest.raises(SystemExit) as exc:
        attach_to_task(client, "tcp://127.0.0.1:2375", "foo-001")
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "is not running" in err
    assert "docker state: exited" in err
    assert "finish" in err
    assert "foo-001" in err

    mock_execvpe.assert_not_called()


def test_attach_refuses_when_no_container_found(
    mock_execvpe: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from naiw_tasks.attach import attach_to_task

    client = MagicMock()
    client.containers.list = MagicMock(return_value=[])

    with pytest.raises(SystemExit) as exc:
        attach_to_task(client, "tcp://127.0.0.1:2375", "foo-001")
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "no container found for task-id=foo-001" in err

    mock_execvpe.assert_not_called()


def test_attach_does_not_create_its_own_docker_client() -> None:
    """attach.py must NOT import or call make_client — it consumes the client
    constructed once in the CLI group (symmetry with start/finish). Importing
    make_client would also re-open a TCP session to the proxy on every attach."""
    source_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "attach.py"
    )
    source = source_path.read_text(encoding="utf-8")
    assert "make_client" not in source, (
        "attach must receive client from caller, not construct its own"
    )
    assert "DockerClient" not in source, (
        "attach must not construct a DockerClient directly"
    )


def test_attach_does_not_request_exec() -> None:
    # Proxy EXEC=0 is permanent — attach must never silently fall back to
    # `docker exec -it` or invoke the SDK's exec_create. Static guard.
    source_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "attach.py"
    )
    source = source_path.read_text(encoding="utf-8")
    assert '"exec"' not in source
    assert "containers.exec_create" not in source
    assert "exec_create" not in source
    assert "exec -it" not in source
