"""Stage B analysers, one per schema format."""
from __future__ import annotations

from collections.abc import Callable

from schema_validator.formats import avro, json_schema, protobuf
from schema_validator.models import FormatAnalysis, ValidatorConfig

ANALYSERS: dict[str, Callable[[str, dict[str, str], ValidatorConfig], FormatAnalysis]] = {
    "AVRO": avro.analyse,
    "JSON": json_schema.analyse,
    "PROTOBUF": protobuf.analyse,
}
