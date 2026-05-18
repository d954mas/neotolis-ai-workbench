"""Tests for naiw_tasks.render — plain-ASCII table + JSON payload serialiser.

Pure formatting module: no I/O, no docker, no click. Source-text guards
enforce the import discipline.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from naiw_tasks import render

# ---------- ComputedRow stand-in --------------------------------------------
# Tests use a tiny structural stand-in instead of importing
# naiw_tasks.reconcile.ComputedRow to keep this test file independent of the
# reconcile module. The render module only reads .status and .notes from the
# object — duck typing is the contract.


@dataclass(frozen=True)
class _Row:
    status: str
    transitioned: bool
    notes: tuple[str, ...]
    failure_reason: str | None


# ---------- render_table column widths -------------------------------------


def test_render_table_pads_columns_to_max_width():
    out = render.render_table(
        headers=["ID", "STATUS"],
        rows=[["alpha-001", "running"], ["task-123", "interrupted"]],
    )
    # "alpha-001" is 9 chars; padded to width 9 then joined with 2 spaces.
    assert "alpha-001  " in out
    # "interrupted" is the longest in the STATUS column; it is the last column
    # so no trailing pad whitespace.
    assert "interrupted" in out


def test_render_table_handles_empty_rows():
    out = render.render_table(headers=["ID", "STATUS"], rows=[])
    assert out == "no tasks\n"


def test_render_table_handles_single_column():
    out = render.render_table(headers=["ID"], rows=[["x"]])
    # Single-column table prints header then row, each rstripped.
    assert out == "ID\nx\n"


def test_render_table_does_not_trail_whitespace_on_last_column():
    out = render.render_table(
        headers=["ID", "STATUS"],
        rows=[["alpha-001", "running"], ["task-123", "interrupted"]],
    )
    for line in out.splitlines():
        assert not line.endswith(" "), f"trailing space on line: {line!r}"


def test_render_table_columns_aligned_consistently():
    out = render.render_table(
        headers=["A", "BB"],
        rows=[["aaa", "b"], ["c", "dddd"]],
    )
    # All lines stripped of trailing whitespace must align.
    lines = out.rstrip("\n").splitlines()
    # Verify header "A    BB" (A padded to 3-wide) and rows match.
    assert lines[0] == "A    BB"
    assert lines[1] == "aaa  b"
    assert lines[2] == "c    dddd"


# ---------- humanize delta --------------------------------------------------


def test_humanize_delta_minutes():
    out = render.humanize_delta(
        now_iso="2026-05-16T10:00:00.000Z",
        reference_iso="2026-05-16T09:55:00.000Z",
    )
    assert out == "5m ago"


def test_humanize_delta_hours():
    out = render.humanize_delta(
        now_iso="2026-05-16T10:00:00.000Z",
        reference_iso="2026-05-16T08:00:00.000Z",
    )
    assert out == "2h ago"


def test_humanize_delta_days():
    out = render.humanize_delta(
        now_iso="2026-05-16T10:00:00.000Z",
        reference_iso="2026-05-13T10:00:00.000Z",
    )
    assert out == "3d ago"


def test_humanize_delta_just_now():
    out = render.humanize_delta(
        now_iso="2026-05-16T10:00:30.000Z",
        reference_iso="2026-05-16T10:00:00.000Z",
    )
    assert out == "just now"


def test_humanize_delta_none_returns_dash():
    out = render.humanize_delta(
        now_iso="2026-05-16T10:00:00.000Z",
        reference_iso=None,
    )
    assert out == "-"


def test_humanize_delta_malformed_returns_raw():
    """Garbage in -> raw string passthrough; caller's problem to spot."""
    out = render.humanize_delta(
        now_iso="2026-05-16T10:00:00.000Z",
        reference_iso="not-a-timestamp",
    )
    assert out == "not-a-timestamp"


# ---------- JSON payload shape ----------------------------------------------


def _sample_task_dict(task_id: str = "alpha-001") -> dict:
    return {
        "id": task_id,
        "kind": "project",
        "status": "running",
        "project": "alpha",
        "started_at": "2026-05-16T09:00:00.000Z",
        "image_digest": "sha256:deadbeefcafebabe1234567890",
        "events_offset": 0,
        "terminal_log_max_size": 0,
        "labels": {"naiw.managed": "1"},
        "secrets": [],
        "schema_version": 1,
    }


def test_to_json_payload_has_as_of_and_tasks_keys():
    rendered = [
        (_sample_task_dict(), _Row("running", False, (), None), "running", None),
    ]
    payload = render.to_json_payload(rendered)
    assert "as_of" in payload
    assert "tasks" in payload
    assert isinstance(payload["as_of"], str)
    assert isinstance(payload["tasks"], list)
    # ISO-8601 ms Z shape (YYYY-MM-DDTHH:MM:SS.mmmZ)
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        payload["as_of"],
    ), payload["as_of"]


def test_to_json_payload_flat_fields_present():
    rendered = [
        (_sample_task_dict(), _Row("running", False, (), None), "running", None),
    ]
    payload = render.to_json_payload(rendered)
    task = payload["tasks"][0]
    for field in (
        "id",
        "kind",
        "status",
        "container_state",
        "container_exit_code",
        "project",
        "started_at",
        "image_digest",
        "notes",
        "task_json",
    ):
        assert field in task, f"missing field {field!r} in {task}"


def test_to_json_payload_task_json_carries_full_raw_dict():
    rendered = [
        (_sample_task_dict(), _Row("running", False, (), None), "running", None),
    ]
    payload = render.to_json_payload(rendered)
    raw = payload["tasks"][0]["task_json"]
    for key in (
        "schema_version",
        "events_offset",
        "terminal_log_max_size",
        "labels",
        "secrets",
    ):
        assert key in raw, f"task_json missing key {key!r}"


def test_to_json_payload_sort_keys_deterministic():
    rendered = [
        (_sample_task_dict(), _Row("running", False, (), None), "running", None),
    ]
    a = render.to_json_payload(rendered)
    b = render.to_json_payload(rendered)
    # `as_of` will differ; compare body only via tasks.
    assert json.dumps(a["tasks"], sort_keys=True) == json.dumps(
        b["tasks"], sort_keys=True
    )


def test_to_json_payload_notes_serialised_as_list():
    """notes is a tuple in ComputedRow; JSON output must convert to list."""
    rendered = [
        (
            _sample_task_dict(),
            _Row("running", False, ("leaked ctr", "log shrunk"), None),
            "running",
            None,
        ),
    ]
    payload = render.to_json_payload(rendered)
    assert payload["tasks"][0]["notes"] == ["leaked ctr", "log shrunk"]


def test_to_json_string_is_indented_sorted():
    """JSON string output is deterministic for diffing in tests."""
    rendered = [
        (_sample_task_dict(), _Row("running", False, (), None), "running", None),
    ]
    payload = render.to_json_payload(rendered)
    s = render.to_json_string(payload)
    # Indented (multi-line); sorted (alphabetical keys); ends parseable.
    assert "\n" in s
    parsed = json.loads(s)
    assert parsed == payload


# ---------- image digest abbreviation ---------------------------------------


def test_image_digest_first_12_hex_chars():
    out = render.short_image_digest(
        "sha256:deadbeefcafebabe1234567890"
    )
    assert out == "deadbeefcafe"


def test_image_digest_none_returns_missing_marker():
    assert render.short_image_digest(None) == "<missing>"


def test_image_digest_no_prefix_truncates_first_12():
    """Digests without `sha256:` prefix (legacy/unusual) still truncate."""
    out = render.short_image_digest("abcdef0123456789xxxx")
    assert out == "abcdef012345"


# ---------- notes formatting ------------------------------------------------


def test_format_notes_joins_with_commas():
    out = render.format_notes(("leaked ctr", "log shrunk"))
    assert out == "(leaked ctr), (log shrunk)"


def test_format_notes_empty_returns_empty_string():
    assert render.format_notes(()) == ""


def test_format_notes_single_note():
    assert render.format_notes(("unknown status",)) == "(unknown status)"


# ---------- COLUMNS constant ------------------------------------------------


def test_columns_match_d12_order():
    assert render.COLUMNS == (
        "ID",
        "STATUS",
        "CTR",
        "PROJECT",
        "STARTED",
        "IMAGE",
        "NOTES",
    )


# ---------- module hygiene --------------------------------------------------


def test_render_module_has_no_gsd_refs():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "render.py"
    ).read_text(encoding="utf-8")
    bad = re.search(
        r"\bD-[0-9]+|\bPhase [0-9]+|\bPlan [0-9]+|\bRESEARCH\b|"
        r"\bCTRL-[0-9]+|\bHARD-[0-9]+|\bDATA-[0-9]+|\bGIT-[0-9]+|"
        r"\bPROJ-[0-9]+|\bPROXY-[0-9]+|\bIMG-[0-9]+|\bSIG-[0-9]+|"
        r"\bLIST-[0-9]+",
        src,
    )
    assert bad is None, f"forbidden token in render.py: {bad.group(0) if bad else None}"


def test_render_does_not_import_docker_or_click():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "render.py"
    ).read_text(encoding="utf-8")
    assert "import docker" not in src
    assert "from docker" not in src
    assert "import click" not in src
    assert "from click" not in src


def test_render_does_not_use_external_table_libs():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "naiw_tasks"
        / "naiw_tasks"
        / "render.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("tabulate", "rich", "humanize"):
        assert f"import {forbidden}" not in src
        assert f"from {forbidden}" not in src
