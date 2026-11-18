"""ValidatorConfig: defaults, environment variables, validation (audit 3 M5)."""
from __future__ import annotations

import sys
from typing import Any

import pytest
from pydantic import ValidationError

from schema_validator.models import (
    DEFAULT_BUILTIN_PROTO_IMPORTS,
    DEFAULT_JSON_DRAFTS,
    ValidatorConfig,
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(ValidatorConfig.model_fields):
        monkeypatch.delenv(f"SCHEMA_VALIDATOR_{name.upper()}", raising=False)


def test_defaults_unchanged() -> None:
    config = ValidatorConfig()
    assert config.sr_url == ""
    assert config.sr_username is None
    assert config.sr_password is None
    assert config.sr_timeout_seconds == 10
    assert config.total_timeout_seconds == 30
    assert config.max_schema_bytes == 500_000
    assert config.max_nesting_depth == 64
    assert config.max_reference_depth == 20
    assert config.max_response_bytes == 5_000_000
    assert config.protoc_timeout_seconds == 10
    assert config.python_executable == sys.executable
    assert config.allowed_json_drafts == DEFAULT_JSON_DRAFTS
    assert config.builtin_proto_imports == DEFAULT_BUILTIN_PROTO_IMPORTS


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEMA_VALIDATOR_MAX_REFERENCE_DEPTH", "5")
    monkeypatch.setenv("SCHEMA_VALIDATOR_SR_URL", "http://x")
    config = ValidatorConfig()
    assert config.max_reference_depth == 5
    assert config.sr_url == "http://x"


@pytest.mark.parametrize("bad", [
    {"sr_timeout_seconds": 0},
    {"total_timeout_seconds": float("nan")},
    {"total_timeout_seconds": -1},
    {"max_reference_depth": 0},
    {"unknown_field": 1},
])
def test_bad_values_raise(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ValidatorConfig(**bad)


def test_frozen() -> None:
    config = ValidatorConfig()
    with pytest.raises(ValidationError):
        config.sr_url = "y"  # type: ignore[misc]


def test_password_not_in_repr() -> None:
    assert "s3cret" not in repr(ValidatorConfig(sr_password="s3cret"))
