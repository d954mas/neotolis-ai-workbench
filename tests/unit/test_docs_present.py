"""File-presence + JSON-ruleset smoke for Phase 6 docs.

These tests assert that the canonical Phase 6 documentation files exist
at the expected paths, that ``SECURITY.md`` has at least seven top-level
sections (the "Looks Done But Isn't" checklist), that the embedded
GitHub Ruleset JSON in ``docs/github-bot.md`` parses cleanly and
carries the load-bearing ``file_path_restriction`` rule, and that the
repo-root ``CHANGELOG.md`` ships the ``## [1.0.0]`` milestone entry
with the required Keep-a-Changelog ``### Added`` and ``### Security``
sections.
"""
import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent


def test_security_md_present() -> None:
    assert (_REPO / "SECURITY.md").is_file()


def test_security_md_has_seven_sections() -> None:
    text = (_REPO / "SECURITY.md").read_text(encoding="utf-8")
    # Match top-level "## " headings (not "### " sub-sections).
    h2 = [line for line in text.splitlines() if line.startswith("## ")]
    assert len(h2) >= 7, (
        f"SECURITY.md has {len(h2)} top-level sections; expected >= 7"
    )


def test_git_policy_doc_present() -> None:
    path = _REPO / "docs" / "git-policy.md"
    assert path.is_file()
    assert "GIT-04" in path.read_text(encoding="utf-8")


def test_github_bot_doc_present() -> None:
    path = _REPO / "docs" / "github-bot.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "GIT-05" in text
    # Four locked sections in order:
    for heading in (
        "## Trust model",
        "## Setup walkthrough",
        "## Operator runbook",
        "## Verification",
    ):
        assert heading in text, (
            f"docs/github-bot.md missing heading {heading!r}"
        )


def test_github_bot_doc_has_valid_json_ruleset() -> None:
    path = _REPO / "docs" / "github-bot.md"
    text = path.read_text(encoding="utf-8")
    # Extract the first fenced ```jsonc or ```json block.
    match = re.search(r"```jsonc?\n(.*?)\n```", text, re.DOTALL)
    assert match, (
        "docs/github-bot.md must embed a ```jsonc / ```json fenced block"
    )
    body = match.group(1)
    # Strip // line comments so json.loads accepts the jsonc body.
    cleaned = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )
    # Strip trailing-comma tolerance (jsonc tolerates them in some
    # toolchains; the import UI strips both comments and commas).
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    parsed = json.loads(cleaned)
    assert parsed.get("target") == "branch", (
        "ruleset target must be 'branch'"
    )
    assert parsed.get("enforcement") == "active"
    rule_types = {rule.get("type") for rule in parsed.get("rules", [])}
    assert "file_path_restriction" in rule_types, (
        "ruleset must include a file_path_restriction rule"
    )


def test_changelog_v1_0_0_present() -> None:
    path = _REPO / "CHANGELOG.md"
    assert path.is_file()
    assert "## [1.0.0]" in path.read_text(encoding="utf-8")


def test_changelog_v1_0_0_has_security_section() -> None:
    text = (_REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "### Security" in text


def test_changelog_v1_0_0_has_added_section() -> None:
    text = (_REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "### Added" in text
