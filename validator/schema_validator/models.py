"""Shared data models for the schema validator."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SCHEMA_TYPES = ("AVRO", "JSON", "PROTOBUF")
COMPATIBILITY_LEVELS = (
    "BACKWARD",
    "BACKWARD_TRANSITIVE",
    "FORWARD",
    "FORWARD_TRANSITIVE",
    "FULL",
    "FULL_TRANSITIVE",
    "NONE",
)

# Stored without a trailing "#".
DEFAULT_JSON_DRAFTS: tuple[str, ...] = (
    "http://json-schema.org/draft-04/schema",
    "http://json-schema.org/draft-06/schema",
    "http://json-schema.org/draft-07/schema",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
)

# Import paths Schema Registry 7.9.0 resolves without a reference.
DEFAULT_BUILTIN_PROTO_IMPORTS: frozenset[str] = frozenset(
    {f"google/protobuf/{n}.proto" for n in (
        "any", "api", "descriptor", "duration", "empty", "field_mask",
        "source_context", "struct", "timestamp", "type", "wrappers",
    )}
    | {f"google/type/{n}.proto" for n in (
        "calendar_period", "color", "date", "datetime", "dayofweek", "decimal",
        "expr", "fraction", "interval", "latlng", "money", "month",
        "phone_number", "postal_address", "quaternion", "timeofday",
    )}
    | {"confluent/meta.proto", "confluent/type/decimal.proto"}
)


@dataclass(frozen=True)
class ReferenceInput:
    schema_name: str
    ref_string: str | None = None
    # The version the user picked. None exists only so stage A can report it.
    version: int | None = None


@dataclass(frozen=True)
class ValidationRequest:
    name: str
    schema_type: str
    schema: str
    compatibility: str
    references: list[ReferenceInput]
    previous_name: str | None
    existing_names: list[str]


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    validationType: str
    message: str


class ValidatorConfig(BaseSettings):
    """Every configurable value. Constructor arguments win over `SCHEMA_VALIDATOR_<FIELD>` variables."""

    model_config = SettingsConfigDict(env_prefix="SCHEMA_VALIDATOR_", frozen=True, extra="forbid")

    sr_url: str = ""
    sr_username: str | None = None
    sr_password: str | None = Field(None, repr=False)
    sr_timeout_seconds: float = Field(10, gt=0, allow_inf_nan=False)
    total_timeout_seconds: float = Field(30, gt=0, allow_inf_nan=False)
    max_schema_bytes: int = Field(500_000, ge=1)
    max_nesting_depth: int = Field(64, ge=1)
    # Levels below a referenced row's own schema (which is level 0).
    max_reference_depth: int = Field(20, ge=1)
    max_response_bytes: int = Field(5_000_000, ge=1)
    protoc_timeout_seconds: float = Field(10, gt=0, allow_inf_nan=False)
    # Must be a real Python interpreter; sys.executable is not one under uWSGI.
    python_executable: str = Field(default_factory=lambda: sys.executable)
    allowed_json_drafts: tuple[str, ...] = DEFAULT_JSON_DRAFTS
    builtin_proto_imports: frozenset[str] = DEFAULT_BUILTIN_PROTO_IMPORTS


@dataclass(frozen=True)
class FormatAnalysis:
    expected_name: str | None
    # None when the text could not be analysed (stages D4/D5 are skipped).
    external_refs: set[str] | None
    errors: list[str]
    # Messages for a schema that passes: the validator adds them first, before its own warnings.
    warnings: list[str] = field(default_factory=list)


def proto_import_path(schema_name: str) -> str:
    """'com.acme.Address' -> 'com/acme/Address.proto'."""
    return schema_name.replace(".", "/") + ".proto"
