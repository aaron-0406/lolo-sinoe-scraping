"""Shared pytest fixtures."""

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked @pytest.mark.live unless LIVE_TESTS=1 is set."""
    if os.environ.get("LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live tests disabled (set LIVE_TESTS=1 to run)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
