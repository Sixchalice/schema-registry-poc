"""Stage B for Avro (spec section 5.1)."""
from __future__ import annotations

import json
from typing import Any

import fastavro
from fastavro.schema import UnknownType

from schema_validator import messages
from schema_validator.models import FormatAnalysis, ValidatorConfig

# Type strings that are not names: primitives and complex-type keywords.
_BUILTIN_TYPES = frozenset(
    {"null", "boolean", "int", "long", "float", "double", "bytes", "string",
     "record", "enum", "fixed", "array", "map"}
)
_NAMED_TYPES = frozenset({"record", "enum", "fixed"})


def analyse(schema_text: str, referenced: dict[str, str], config: ValidatorConfig) -> FormatAnalysis:
    """Steps 1 to 7 of spec 5.1. `referenced` maps reference names to schema texts, in dependency order."""
    # Step 1
    try:
        schema = json.loads(schema_text)
    except (ValueError, RecursionError) as e:
        return FormatAnalysis(None, None, [messages.NOT_JSON.format(err=e)])

    # Step 2
    if not isinstance(schema, dict) or schema.get("type") != "record":
        return FormatAnalysis(None, None, [messages.AVRO_TOP_LEVEL])

    errors: list[str] = []
    expected_name: str | None = None

    # Steps 3 to 5. A name that is not a string is left to fastavro (step 7).
    name = schema.get("name")
    if isinstance(name, str):
        full_name = _full_name(name, _own_namespace(schema, None))
        if "." in full_name:
            expected_name = full_name
        else:
            errors.append(messages.AVRO_NAMESPACE)

    # Step 6
    external_refs = _external_refs(schema)

    # Step 7
    error = _parse_error(schema, referenced)
    if error is not None:
        errors.append(messages.AVRO_INVALID.format(err=error))

    return FormatAnalysis(expected_name, external_refs, errors)


def _own_namespace(named_type: dict[str, Any], enclosing: str | None) -> str | None:
    """The namespace a named type declares, or the enclosing one. "" means no namespace."""
    namespace = named_type.get("namespace")
    if isinstance(namespace, str):
        return namespace or None
    return enclosing


def _full_name(name: str, namespace: str | None) -> str:
    if "." in name or not namespace:
        return name
    return f"{namespace}.{name}"


def _external_refs(schema: dict[str, Any]) -> set[str]:
    """Names used in type positions that the text does not define itself."""
    defined: set[str] = set()
    used: set[str] = set()
    # Each entry is a value in a type position and the namespace enclosing it.
    pending: list[tuple[Any, str | None]] = [(schema, None)]
    while pending:
        node, namespace = pending.pop()
        if isinstance(node, str):
            if node not in _BUILTIN_TYPES:
                used.add(_full_name(node, namespace))
        elif isinstance(node, list):
            pending.extend((member, namespace) for member in node)
        elif isinstance(node, dict):
            kind = node.get("type")
            # `kind` may be a list or dict (pushed back below), so test str before hashing.
            if isinstance(kind, str) and kind in _NAMED_TYPES:
                name = node.get("name")
                inner = namespace
                if isinstance(name, str):
                    full = _full_name(name, _own_namespace(node, namespace))
                    defined.add(full)
                    inner = full.rpartition(".")[0] or None
                fields = node.get("fields") if kind == "record" else None
                if isinstance(fields, list):
                    pending.extend((f["type"], inner) for f in fields if isinstance(f, dict) and "type" in f)
            elif kind == "array":
                if "items" in node:
                    pending.append((node["items"], namespace))
            elif kind == "map":
                if "values" in node:
                    pending.append((node["values"], namespace))
            elif kind is not None:
                pending.append((kind, namespace))
    return used - defined


def _parse_error(schema: dict[str, Any], referenced: dict[str, str]) -> str | None:
    """fastavro's error for the schema, parsed after its references; None if it parses."""
    named: dict[str, Any] = {}
    for text in referenced.values():
        try:
            fastavro.parse_schema(json.loads(text), named_schemas=named)
        except Exception:
            # A reference fastavro cannot parse is not this schema's error.
            pass
    try:
        fastavro.parse_schema(schema, named_schemas=named)
    except UnknownType:
        # A missing reference; stage D explains it. Caught first: it is not a SchemaParseException.
        return None
    except Exception as e:
        # SchemaParseException, and also TypeError, ValueError etc. on malformed schemas.
        return str(e)
    return None
