"""Tests for naiw_tasks.config — defaults, NAIW_DATA env, config.yaml loader."""

from pathlib import Path

import pytest

from naiw_tasks import config as config_mod


def test_load_defaults_when_no_config_file(tmp_naiw_data: Path) -> None:
    cfg = config_mod.load()
    assert cfg.data_root == tmp_naiw_data
    assert cfg.docker_proxy_url == "tcp://127.0.0.1:2375"
    assert cfg.task_image == "naiw-task-image:latest"


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
