"""Version-lockstep guard for the three in-tree packages.

The MVP ships ``naiw_common``, ``naiw_signal`` and ``naiw_tasks`` in
version-lockstep: they live in the same repository, ship from the same
CHANGELOG entry, and the controller depends on a specific ``naiw_common``
version. Future patch releases MAY decouple them — when that happens this
test will fail loudly, forcing a deliberate decision rather than a silent
drift.
"""
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_PROJECTS = (
    _REPO / "src" / "naiw_common" / "pyproject.toml",
    _REPO / "src" / "naiw_signal" / "pyproject.toml",
    _REPO / "src" / "naiw_tasks" / "pyproject.toml",
)

_EXPECTED_VERSION = "1.0.0"


def _project_version(toml_path: Path) -> str:
    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


def test_naiw_common_version_is_1_0_0() -> None:
    assert _project_version(_PROJECTS[0]) == _EXPECTED_VERSION


def test_naiw_signal_version_is_1_0_0() -> None:
    assert _project_version(_PROJECTS[1]) == _EXPECTED_VERSION


def test_naiw_tasks_version_is_1_0_0() -> None:
    assert _project_version(_PROJECTS[2]) == _EXPECTED_VERSION


def test_all_three_versions_match() -> None:
    versions = {p.parent.name: _project_version(p) for p in _PROJECTS}
    assert len(set(versions.values())) == 1, (
        f"version-lockstep broken: {versions}"
    )
