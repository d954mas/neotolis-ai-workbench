"""Tests for naiw_tasks.config — defaults, NAIW_DATA env, config.yaml loader."""

from pathlib import Path

import pytest

from naiw_tasks import config as config_mod


def test_naiw_data_env_expands_tilde(monkeypatch, tmp_path) -> None:
    """`export NAIW_DATA=~/somewhere` should resolve to the operator's home,
    same as shell semantics. `Path("~/...")` alone does NOT expand the tilde
    — expanduser() is required."""
    fake_home = tmp_path
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    # ~/data is a literal NAIW_DATA value the operator might export.
    monkeypatch.setenv("NAIW_DATA", "~/data")

    cfg = config_mod.load()
    assert cfg.data_root == fake_home / "data"


def test_load_defaults_when_no_config_file(tmp_naiw_data: Path) -> None:
    cfg = config_mod.load()
    assert cfg.data_root == tmp_naiw_data
    assert cfg.docker_proxy_url == "tcp://naiw-docker-proxy:2375"
    assert cfg.task_image == "ghcr.io/d954mas/naiw-task-image:latest"


def test_load_reads_config_yaml(tmp_naiw_data: Path) -> None:
    (tmp_naiw_data / "config.yaml").write_text(
        "docker_proxy_url: tcp://example:2375\n"
        "task_image: foo:bar\n"
        "schema_version: 1\n",
        encoding="utf-8",
    )
    cfg = config_mod.load()
    assert cfg.docker_proxy_url == "tcp://example:2375"
    assert cfg.task_image == "foo:bar"
    assert cfg.data_root == tmp_naiw_data


@pytest.mark.parametrize(
    "yaml_body, field",
    [
        ("docker_proxy_url: null\n", "docker_proxy_url"),
        ("docker_proxy_url: 12345\n", "docker_proxy_url"),
        ('docker_proxy_url: ""\n', "docker_proxy_url"),
        ("docker_proxy_url: [tcp, 127, 0, 0, 1]\n", "docker_proxy_url"),
        ("task_image: null\n", "task_image"),
        ("task_image: 123\n", "task_image"),
        ('task_image: ""\n', "task_image"),
        ("task_image:\n  name: foo\n  tag: bar\n", "task_image"),
    ],
)
def test_load_rejects_non_string_field_values(
    yaml_body, field, tmp_naiw_data: Path
) -> None:
    """Wrong-typed config values (yaml null, int, list, dict, empty string)
    must raise ValueError from config.load — not flow through to a raw
    TypeError when DockerClient or containers.run gets handed the bad value."""
    (tmp_naiw_data / "config.yaml").write_text(yaml_body, encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        config_mod.load()

    msg = str(excinfo.value)
    assert field in msg
    assert "must be a non-empty string" in msg


def test_load_unparseable_yaml_raises_value_error(tmp_naiw_data: Path) -> None:
    """yaml.YAMLError must be normalised to ValueError so cli.py does not need
    to depend on yaml internals when wrapping config.load() errors."""
    (tmp_naiw_data / "config.yaml").write_text(
        "this is: not: valid: yaml: }}}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError) as excinfo:
        config_mod.load()
    msg = str(excinfo.value)
    assert "config.yaml" in msg
    assert "cannot be parsed" in msg


def test_load_rejects_unknown_key(tmp_naiw_data: Path) -> None:
    (tmp_naiw_data / "config.yaml").write_text(
        "nonsense: 1\n", encoding="utf-8"
    )
    with pytest.raises(ValueError) as exc:
        config_mod.load()
    msg = str(exc.value)
    assert "nonsense" in msg
    assert "schema_version=1" in msg
    # All supported keys mentioned to help operator fix typos.
    for key in ("docker_proxy_url", "task_image", "schema_version"):
        assert key in msg


def test_load_rejects_unknown_schema_version(tmp_naiw_data: Path) -> None:
    (tmp_naiw_data / "config.yaml").write_text(
        "schema_version: 2\n", encoding="utf-8"
    )
    with pytest.raises(ValueError) as exc:
        config_mod.load()
    assert "unsupported config schema_version=2" in str(exc.value)


def test_naiw_data_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom-naiw"
    custom.mkdir()
    monkeypatch.setenv("NAIW_DATA", str(custom))
    cfg = config_mod.load()
    assert cfg.data_root == custom


def test_config_yaml_must_be_mapping(tmp_naiw_data: Path) -> None:
    (tmp_naiw_data / "config.yaml").write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        config_mod.load()
    assert "must be a mapping" in str(exc.value)


def test_default_data_root_is_home_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NAIW_DATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = config_mod.load()
    assert cfg.data_root == tmp_path / "naiw-data"


def test_default_docker_proxy_url_uses_internal_dns(tmp_naiw_data: Path) -> None:
    """Default proxy URL is the internal DNS name. Sentinel against a regression
    that reintroduces the host-published 127.0.0.1:2375 pattern."""
    cfg = config_mod.load()
    assert cfg.docker_proxy_url == "tcp://naiw-docker-proxy:2375"


def test_default_task_image_uses_ghcr(tmp_naiw_data: Path) -> None:
    """Default task image is the ghcr-published artifact. Sentinel against a
    regression that reintroduces a local-build name like `naiw-task-image:latest`."""
    cfg = config_mod.load()
    assert cfg.task_image == "ghcr.io/d954mas/naiw-task-image:latest"


def test_docker_proxy_url_env_overrides_default(
    monkeypatch, tmp_naiw_data: Path
) -> None:
    """NAIW_DOCKER_PROXY_URL env mirrors the NAIW_DATA contract — operator can
    override without editing config.yaml. Path: env > yaml > default."""
    monkeypatch.setenv("NAIW_DOCKER_PROXY_URL", "tcp://debug-proxy:9999")
    cfg = config_mod.load()
    assert cfg.docker_proxy_url == "tcp://debug-proxy:9999"


def test_docker_proxy_url_env_overrides_yaml(
    monkeypatch, tmp_naiw_data: Path
) -> None:
    """Env wins over config.yaml. Order: env > yaml > default."""
    (tmp_naiw_data / "config.yaml").write_text(
        "docker_proxy_url: tcp://from-yaml:1111\n", encoding="utf-8"
    )
    monkeypatch.setenv("NAIW_DOCKER_PROXY_URL", "tcp://from-env:2222")
    cfg = config_mod.load()
    assert cfg.docker_proxy_url == "tcp://from-env:2222"


def test_docker_proxy_url_yaml_used_when_env_unset(
    monkeypatch, tmp_naiw_data: Path
) -> None:
    """config.yaml docker_proxy_url is honored when env var is absent."""
    monkeypatch.delenv("NAIW_DOCKER_PROXY_URL", raising=False)
    (tmp_naiw_data / "config.yaml").write_text(
        "docker_proxy_url: tcp://from-yaml:3333\n", encoding="utf-8"
    )
    cfg = config_mod.load()
    assert cfg.docker_proxy_url == "tcp://from-yaml:3333"


# ---------------------------------------------------------------------------
# data_root_host / host_root — for containerized controller bind-mount paths
# ---------------------------------------------------------------------------


def test_host_root_falls_back_to_data_root_when_env_unset(
    monkeypatch, tmp_naiw_data: Path
) -> None:
    """Direct host invocation (tests, ad-hoc CLI): NAIW_DATA_HOST not set,
    host_root must equal data_root so existing call sites work unchanged."""
    monkeypatch.delenv("NAIW_DATA_HOST", raising=False)
    cfg = config_mod.load()
    assert cfg.data_root_host is None
    assert cfg.host_root == cfg.data_root


def test_host_root_uses_naiw_data_host_when_set(
    monkeypatch, tmp_naiw_data: Path
) -> None:
    """Compose path: NAIW_DATA=/naiw-data + NAIW_DATA_HOST=/home/op/naiw-data
    — host_root must surface the host-side path so containers.run hands the
    daemon a path it can actually resolve."""
    monkeypatch.setenv("NAIW_DATA_HOST", "/home/op/naiw-data")
    cfg = config_mod.load()
    assert cfg.data_root_host == Path("/home/op/naiw-data")
    assert cfg.host_root == Path("/home/op/naiw-data")


def test_host_root_env_expands_tilde(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("NAIW_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("NAIW_DATA_HOST", "~/host-data")
    cfg = config_mod.load()
    assert cfg.data_root_host == tmp_path / "host-data"
