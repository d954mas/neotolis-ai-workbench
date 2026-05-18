"""KISS formatters shared across the controller.

Previously every module that printed byte counts had its own _humanize:
disk used .2f, startup_checks used .1f, clean did integer-division. The
inconsistency leaked into operator-facing output (`48.21GiB` in `disk`
vs `48.2GiB` in the threshold-gate error). Single source of truth here.
"""


def humanize_iec_bytes(n: int, precision: int = 1) -> str:
    """Render an int byte count as <num><IEC unit>.

    `precision` controls fractional digits for KiB and up. The `B` unit
    is always rendered as a bare integer (no fractional bytes — they're
    meaningless at that scale). IEC binary units only (1024^k), matching
    config.yaml `max_data_size`.
    """
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(f)}{unit}"
            return f"{f:.{precision}f}{unit}"
        f /= 1024.0
    return f"{f:.{precision}f}TiB"  # unreachable
