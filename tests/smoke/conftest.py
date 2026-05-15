"""Pytest configuration for tests/smoke/."""

import platform
from pathlib import Path

import pytest


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


def pytest_runtest_setup(item):
    if "linux_only" not in item.keywords:
        return
    if platform.system() != "Linux":
        pytest.skip("requires Linux")
    if str(Path.home().resolve()).startswith("/mnt/"):
        pytest.skip("requires Linux-FS backed home")
