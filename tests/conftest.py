"""Test-wide isolation from a delegated cross-harness environment."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_cross_harness_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited cross-harness state for each test.

    Tests that need to exercise recursion prevention set the relevant variables
    explicitly (for example with ``patch.dict``) after this fixture runs.
    """
    for name in list(os.environ):
        if name.startswith("CROSS_HARNESS_"):
            monkeypatch.delenv(name)
