"""max_data_size parser + Config field tests.

Locks the IEC-binary-only contract for the per-namespace cap on
~/naiw-data/ that drives `naiw-tasks disk` warnings and `naiw-tasks start`
threshold refusals.
"""

import pytest

from naiw_tasks.config import (
    DEFAULT_MAX_DATA_SIZE_BYTES,
    Config,
    _parse_max_data_size,
    load,
)


def test_parses_iec_units_and_rejects_KB_with_hint():
    """IEC binary suffixes accepted; SI-style suffixes rejected with a hint
    that names the supported form (KiB/MiB/GiB/TiB)."""
    assert _parse_max_data_size("1KiB") == 1024
    assert _parse_max_data_size("1MiB") == 1024 ** 2
    assert _parse_max_data_size("50GiB") == 50 * 1024 ** 3
    assert _parse_max_data_size("2TiB") == 2 * 1024 ** 4

    with pytest.raises(ValueError) as exc:
        _parse_max_data_size("50GB")
    assert "use IEC binary units (KiB/MiB/GiB/TiB)" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        _parse_max_data_size("50")
    # Bare integer hits the catch-all branch; error must surface the
    # supported unit set so the operator knows what shape to use.
    assert "KiB" in str(exc.value)


def test_max_data_size_default_is_50_GiB(tmp_path):
    """Config() default carries the canonical 50 GiB byte count; the
    constant matches the literal computation 50 * 1024 ** 3."""
    cfg = Config(data_root=tmp_path)
    assert cfg.max_data_size == DEFAULT_MAX_DATA_SIZE_BYTES
    assert DEFAULT_MAX_DATA_SIZE_BYTES == 50 * 1024 ** 3


def test_config_load_parses_max_data_size_yaml(tmp_naiw_data, monkeypatch):
    """config.yaml max_data_size string flows through the parser into the
    Config.max_data_size byte count."""
    cfg_path = tmp_naiw_data / "config.yaml"
    cfg_path.write_text("max_data_size: 100GiB\n", encoding="utf-8")
    cfg = load()
    assert cfg.max_data_size == 100 * 1024 ** 3


def test_config_load_max_data_size_rejects_si_typo(tmp_naiw_data):
    """End-to-end through load(): an SI-style suffix in config.yaml surfaces
    the same typo-hint as the raw parser does."""
    cfg_path = tmp_naiw_data / "config.yaml"
    cfg_path.write_text("max_data_size: 50GB\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load()
    assert "use IEC binary units" in str(exc.value)


def test_max_data_size_zero_is_rejected():
    """max_data_size=0KiB would silently disable both the >80% warning and
    the >95% start-refusal gate. Reject at parse time so a config typo
    cannot accidentally nuke the cap."""
    with pytest.raises(ValueError) as exc:
        _parse_max_data_size("0KiB")
    assert "must be > 0" in str(exc.value)
    assert "silently disables" in str(exc.value)
    # 0 in other units rejected the same way.
    with pytest.raises(ValueError):
        _parse_max_data_size("0GiB")
