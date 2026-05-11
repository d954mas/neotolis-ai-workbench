"""Pytest configuration for tests/smoke/.

Registers smoke-suite custom markers so pytest's collection phase doesn't emit
PytestUnknownMarkWarning, and selectivity via `-m` works as documented.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "smoke: marks tests as smoke tests (subprocess-call bash harnesses).",
    )
    config.addinivalue_line(
        "markers",
        "linux_only: marks tests that require a Linux host with Linux-FS "
        "(SKIPs cleanly on Windows-native and on WSL2 with /mnt/c-backed $HOME).",
    )
