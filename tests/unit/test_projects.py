"""Tests for naiw_tasks.projects — projects.yaml safe_load + strict schema +
workspace/repos/ prefix enforcement.
"""

from pathlib import Path

import pytest

import naiw_tasks.projects as projects_mod
from naiw_tasks.path_validation import BindMountEscapeError
from naiw_tasks.projects import load


def _write_yaml(tmp_naiw_data: Path, content: str) -> Path:
    p = tmp_naiw_data / "projects.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_safe_load_map(tmp_naiw_data):
    repo = tmp_naiw_data / "workspace" / "repos" / "alpha"
    repo.mkdir()
    yaml_path = _write_yaml(
        tmp_naiw_data,
        f"projects:\n  alpha:\n    path: {repo}\n",
    )

    result = load(yaml_path, tmp_naiw_data)

    assert set(result.keys()) == {"alpha"}
    assert Path(result["alpha"]["path"]).resolve() == repo.resolve()


def test_load_uses_safe_load_not_yaml_load():
    src = Path(projects_mod.__file__).resolve().read_text(encoding="utf-8")
    assert "safe_load" in src
    # Naked `yaml.load(` (without the safe_ prefix) MUST NOT appear.
    assert "yaml.load(" not in src


def test_load_rejects_non_dict_top_level(tmp_naiw_data):
    yaml_path = _write_yaml(tmp_naiw_data, "- alpha\n- beta\n")

    with pytest.raises(ValueError) as excinfo:
        load(yaml_path, tmp_naiw_data)

    msg = str(excinfo.value)
    assert "projects" in msg


def test_load_unparseable_yaml_raises_value_error(tmp_naiw_data):
    """yaml.YAMLError must be normalised to ValueError so callers do not need
    to import yaml — keeps the projects-load contract one-liner: FileNotFoundError
    or ValueError, nothing else from this module."""
    yaml_path = _write_yaml(tmp_naiw_data, "this is: not: valid: yaml: }}}\n")

    with pytest.raises(ValueError) as excinfo:
        load(yaml_path, tmp_naiw_data)

    msg = str(excinfo.value)
    assert "projects.yaml" in msg
    assert "cannot parse" in msg


def test_load_rejects_missing_projects_key(tmp_naiw_data):
    yaml_path = _write_yaml(tmp_naiw_data, "other: 1\n")

    with pytest.raises(ValueError) as excinfo:
        load(yaml_path, tmp_naiw_data)

    assert "projects" in str(excinfo.value)


def test_load_rejects_non_dict_projects(tmp_naiw_data):
    yaml_path = _write_yaml(tmp_naiw_data, "projects: [a, b]\n")

    with pytest.raises(ValueError) as excinfo:
        load(yaml_path, tmp_naiw_data)

    assert "must be a mapping" in str(excinfo.value)


def test_load_rejects_non_dict_spec(tmp_naiw_data):
    yaml_path = _write_yaml(
        tmp_naiw_data, 'projects:\n  alpha: "/some/path"\n'
    )

    with pytest.raises(ValueError) as excinfo:
        load(yaml_path, tmp_naiw_data)

    assert "must be a mapping" in str(excinfo.value)


def test_unknown_keys_fail_fast(tmp_naiw_data):
    repo = tmp_naiw_data / "workspace" / "repos" / "alpha"
    repo.mkdir()
    yaml_path = _write_yaml(
        tmp_naiw_data,
        f"projects:\n  alpha:\n    path: {repo}\n    extra: y\n",
    )

    with pytest.raises(ValueError) as excinfo:
        load(yaml_path, tmp_naiw_data)

    msg = str(excinfo.value)
    assert "unknown keys" in msg
    assert "['extra']" in msg


def test_missing_path_key_fails(tmp_naiw_data):
    yaml_path = _write_yaml(tmp_naiw_data, "projects:\n  alpha: {}\n")

    with pytest.raises(ValueError) as excinfo:
        load(yaml_path, tmp_naiw_data)

    assert "missing required 'path'" in str(excinfo.value)


def test_path_outside_prefix_rejected(tmp_naiw_data, tmp_path):
    # Exists on disk but lives outside <data_root>/workspace/repos/
    foreign = tmp_path / "foreign-repo"
    foreign.mkdir()
    yaml_path = _write_yaml(
        tmp_naiw_data,
        f"projects:\n  alpha:\n    path: {foreign}\n",
    )

    with pytest.raises(BindMountEscapeError):
        load(yaml_path, tmp_naiw_data)


def test_path_inside_workspace_repos_accepted(tmp_naiw_data):
    repo = tmp_naiw_data / "workspace" / "repos" / "alpha"
    repo.mkdir()
    yaml_path = _write_yaml(
        tmp_naiw_data,
        f"projects:\n  alpha:\n    path: {repo}\n",
    )

    result = load(yaml_path, tmp_naiw_data)
    assert "alpha" in result


def test_nonexistent_path_rejected(tmp_naiw_data):
    missing = tmp_naiw_data / "workspace" / "repos" / "never-cloned"
    # Deliberately NOT created
    yaml_path = _write_yaml(
        tmp_naiw_data,
        f"projects:\n  alpha:\n    path: {missing}\n",
    )

    with pytest.raises(BindMountEscapeError):
        load(yaml_path, tmp_naiw_data)
