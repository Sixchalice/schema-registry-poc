"""Shared data models for the schema validator."""
from __future__ import annotations

from dataclasses import dataclass, field

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
    "https://json-schema.org/draft-07/schema",
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


@dataclass(frozen=True)
class ValidatorConfig:
    sr_url: str = ""
    sr_username: str | None = None
    sr_password: str | None = field(default=None, repr=False)
    sr_timeout_seconds: float = 10
    max_schema_bytes: int = 500_000
    allowed_json_drafts: tuple[str, ...] = DEFAULT_JSON_DRAFTS
    builtin_proto_imports: frozenset[str] = DEFAULT_BUILTIN_PROTO_IMPORTS


@dataclass(frozen=True)
class FormatAnalysis:
    expected_name: str | None
    # None when the text could not be analysed (stages D4/D5 are skipped).
    external_refs: set[str] | None
    errors: list[str]


def proto_import_path(schema_name: str) -> str:
    """'com.acme.Address' -> 'com/acme/Address.proto'."""
    return schema_name.replace(".", "/") + ".proto"
