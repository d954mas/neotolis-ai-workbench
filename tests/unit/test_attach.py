"""Tests for naiw_tasks.attach — os.execvp-based docker attach with label lookup."""

from pathlib import Path
from unittest.mock import MagicMock, call

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


def test_attach_calls_execvp_when_running(
    mock_execvp: MagicMock,
) -> None:
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="running")

    # The real os.execvp does not return on success, so the function ends with
    # a defence-in-depth SystemExit(1) for the (impossible) post-execvp path.
    # With execvp mocked, that trailing exit fires — catch and ignore it.
    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://127.0.0.1:2375", "foo-001")

    # POSIX argv[0] convention — program name appears as argv[0] inside the
    # new process. `-H <proxy_url>` routes the CLI through the locked proxy
    # (same path as the SDK) so docker CLI does NOT fall back to the host
    # socket.
    assert mock_execvp.call_args == call(
        "docker",
        ["docker", "-H", "tcp://127.0.0.1:2375", "attach", "naiw-task-foo-001"],
    )


def test_attach_passes_proxy_url_via_dash_h(mock_execvp: MagicMock) -> None:
    """The -H flag is the only thing keeping `docker attach` on the proxy. If
    it disappears or the URL is wrong, docker CLI silently falls back to
    /var/run/docker.sock — bypassing every allowlist check."""
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="running")

    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://example.internal:2375", "foo-001")

    argv = mock_execvp.call_args.args[1]
    assert "-H" in argv
    h_index = argv.index("-H")
    assert argv[h_index + 1] == "tcp://example.internal:2375"


def test_attach_uses_label_list_filter(mock_execvp: MagicMock) -> None:
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


def test_attach_calls_list_with_all_true(mock_execvp: MagicMock) -> None:
    from naiw_tasks.attach import attach_to_task

    client, _container = _fake_client_with_container(state="running")

    with pytest.raises(SystemExit):
        attach_to_task(client, "tcp://127.0.0.1:2375", "foo-001")

    # all=True so a stopped-but-still-existing container surfaces and the
    # refusal hint fires (otherwise containers.list silently returns []).
    assert client.containers.list.call_args.kwargs["all"] is True


def test_attach_refuses_when_not_running(
    mock_execvp: MagicMock,
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

    mock_execvp.assert_not_called()


def test_attach_refuses_when_no_container_found(
    mock_execvp: MagicMock,
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

    mock_execvp.assert_not_called()


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
