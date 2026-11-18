"""Shared setup for the unit tests."""
from __future__ import annotations

import os

import pytest

_PREFIX = "SCHEMA_VALIDATOR_"

# Some test modules build ValidatorConfig() at import, before any fixture runs, so the
# shell's SCHEMA_VALIDATOR_* variables go for the whole session as soon as this loads.
for _name in [n for n in os.environ if n.startswith(_PREFIX)]:
    del os.environ[_name]


@pytest.fixture(autouse=True)
def _clean_validator_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SCHEMA_VALIDATOR_* variable reaches ValidatorConfig (or a subprocess) in a test."""
    for name in list(os.environ):
        if name.startswith(_PREFIX):
            monkeypatch.delenv(name)
