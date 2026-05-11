"""Tests for naiw_tasks.projects — projects.yaml safe_load + strict schema +
workspace/repos/ prefix enforcement.
"""

from pathlib import Path

import naiw_tasks.projects as projects_mod
import pytest
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


def test_tilde_path_is_expanded_before_validation(tmp_naiw_data, monkeypatch):
    """The shipped `scripts/projects.yaml.example` uses
    `path: ~/naiw-data/workspace/repos/<alias>`. Path.resolve() does NOT
    expand `~` (it treats it as a literal component), so without expanduser
    the documented example would always be rejected with
    `bind mount source does not exist`. projects.load() must expand the
    tilde to the operator's home before the prefix check."""
    # Point HOME at tmp_naiw_data's parent so `~/naiw-data/...` resolves into
    # our test data root. tmp_naiw_data is `<tmp_path>/naiw-data`, so HOME =
    # tmp_path makes `~/naiw-data` == tmp_naiw_data.
    fake_home = tmp_naiw_data.parent
    monkeypatch.setenv("HOME", str(fake_home))
    # On Windows expanduser uses USERPROFILE; set it too for cross-platform
    # safety in case tests run on win32 (most do skip but be defensive).
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    repo_under_home = tmp_naiw_data / "workspace" / "repos" / "alpha"
    repo_under_home.mkdir()

    yaml_path = _write_yaml(
        tmp_naiw_data,
        "projects:\n  alpha:\n    path: ~/naiw-data/workspace/repos/alpha\n",
    )

    result = load(yaml_path, tmp_naiw_data)
    assert "alpha" in result
    # The resolved path is the absolute, tilde-expanded one.
    assert Path(result["alpha"]["path"]).resolve() == repo_under_home.resolve()


def test_nonexistent_path_rejected(tmp_naiw_data):
    missing = tmp_naiw_data / "workspace" / "repos" / "never-cloned"
    # Deliberately NOT created
    yaml_path = _write_yaml(
        tmp_naiw_data,
        f"projects:\n  alpha:\n    path: {missing}\n",
    )

    with pytest.raises(BindMountEscapeError):
        load(yaml_path, tmp_naiw_data)
