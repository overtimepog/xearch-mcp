"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xearch_mcp.runtime import reset_runtime

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fresh_runtime():
    """Give every test a runtime bound to its own event loop."""
    reset_runtime()
    yield
    reset_runtime()


@pytest.fixture
def fixture_json():
    def load(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def fixture_text():
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return load
