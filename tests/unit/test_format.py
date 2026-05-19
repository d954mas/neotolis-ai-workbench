"""Tests for format.humanize_iec_bytes — single source of truth replacing
three near-identical _humanize_bytes copies."""

from naiw_tasks.format import humanize_iec_bytes


def test_bytes_unit_has_no_fractional_part():
    assert humanize_iec_bytes(0) == "0B"
    assert humanize_iec_bytes(1) == "1B"
    assert humanize_iec_bytes(1023) == "1023B"


def test_kib_at_boundary():
    assert humanize_iec_bytes(1024) == "1.0KiB"
    assert humanize_iec_bytes(1024 + 512) == "1.5KiB"


def test_precision_one_is_default_for_units_above_b():
    # B-unit ignores precision (no fractional bytes).
    assert humanize_iec_bytes(1000) == "1000B"
    # Just under 100KiB → 99.999KiB → .1f rounds to 100.0KiB.
    out = humanize_iec_bytes(100 * 1024 - 1)
    assert out.endswith("KiB")
    # Default precision is .1f for KiB and up.
    assert humanize_iec_bytes(1500) == "1.5KiB"


def test_explicit_precision_two():
    assert humanize_iec_bytes(1024, precision=2) == "1.00KiB"
    assert humanize_iec_bytes(1024 + 512, precision=2) == "1.50KiB"


def test_explicit_precision_zero():
    assert humanize_iec_bytes(1024, precision=0) == "1KiB"


def test_large_units():
    one_gib = 1024 ** 3
    assert humanize_iec_bytes(one_gib) == "1.0GiB"
    assert humanize_iec_bytes(50 * one_gib) == "50.0GiB"


def test_tib_does_not_overflow_into_unknown_unit():
    # 4 TiB stays in TiB even though loop is exhausted.
    four_tib = 4 * 1024 ** 4
    assert humanize_iec_bytes(four_tib).endswith("TiB")
    huge = 1024 ** 5  # 1 PiB — still rendered as TiB
    assert humanize_iec_bytes(huge).endswith("TiB")
