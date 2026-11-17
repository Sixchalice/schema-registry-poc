"""Step 8 of spec 5.1: every field default is a value of the field's type.

Java Avro 1.11 (`Schema.isValidDefault`) checks this when it parses a schema, fastavro
only for primitive types. The rules are Java's, plus three from the Avro specification
that Java does not check: an enum default is one of its symbols, a fixed default has
exactly `size` characters, and a bytes or fixed default uses only U+0000 to U+00FF.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from schema_validator import messages

_NAMED_TYPES = ("record", "enum", "fixed")
_NON_FINITE = ("NaN", "Infinity", "-Infinity")


def _integer(low: int, high: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and low <= v <= high


def _floating(value: Any) -> bool:
    if isinstance(value, str):
        return value in _NON_FINITE
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _latin1(value: Any) -> bool:
    """A bytes value in JSON: a string whose characters are each one byte."""
    return isinstance(value, str) and all(ord(c) <= 0xFF for c in value)


_PRIMITIVES: dict[str, Callable[[Any], bool]] = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "int": _integer(-(2**31), 2**31 - 1),
    "long": _integer(-(2**63), 2**63 - 1),
    "float": _floating,
    "double": _floating,
    "string": lambda v: isinstance(v, str),
    "bytes": _latin1,
}


def default_errors(parsed: dict[str, Any], named: Mapping[str, Any]) -> list[str]:
    """One message per field whose default is not a value of its type, records in document order.

    `parsed` is fastavro's `parse_schema` output for the submitted text: its names are full
    names, and a later use of a named type is its full-name string, so only the records the
    text defines inline are walked. `named` holds every named type (the references' too),
    which is how a name string is checked.
    """
    errors: list[str] = []
    for record in _records(parsed):
        fields = record.get("fields")
        for f in fields if isinstance(fields, list) else ():
            if not isinstance(f, dict) or "default" not in f:
                continue
            type_ = f.get("type")
            union = isinstance(type_, list)
            checked = (type_[0] if type_ else None) if union else type_
            try:
                valid = _valid(f["default"], checked, named)
            except Exception:
                # Only a shape fastavro does not produce (a name cycle) gets here; not a pass.
                valid = False
            if valid:
                continue
            if union:
                errors.append(messages.AVRO_UNION_DEFAULT.format(
                    field=f.get("name"), record=record.get("name"), first=_describe(checked)))
            else:
                errors.append(messages.AVRO_DEFAULT_INVALID.format(
                    field=f.get("name"), record=record.get("name"), type=_describe(checked)))
    return errors


def _records(parsed: Any) -> list[dict[str, Any]]:
    """The records defined in the text, in document order (where each one starts)."""
    records: list[dict[str, Any]] = []
    # Children are pushed in reverse, so the stack pops them in document order.
    pending: list[Any] = [parsed]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(reversed(node))
        elif isinstance(node, dict):
            kind = node.get("type")
            if kind == "record":
                records.append(node)
                fields = node.get("fields")
                if isinstance(fields, list):
                    pending.extend(f.get("type") for f in reversed(fields) if isinstance(f, dict))
            elif kind == "array":
                pending.append(node.get("items"))
            elif kind == "map":
                pending.append(node.get("values"))
            else:
                # A primitive or logical type ({"type": "long", ...}), an enum or fixed: a
                # string, which adds nothing. Anything else is walked into.
                pending.append(kind)
    return records


def _valid(value: Any, schema: Any, named: Mapping[str, Any]) -> bool:
    """Whether `value` (JSON) is a value of `schema` (fastavro's parsed form)."""
    if isinstance(schema, list):  # a union: its first branch
        return bool(schema) and _valid(value, schema[0], named)
    if isinstance(schema, str):
        check = _PRIMITIVES.get(schema)
        if check is not None:
            return check(value)
        target = named.get(schema)
        return isinstance(target, dict) and _valid(value, target, named)
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if kind == "record":
        return isinstance(value, dict) and _record_value(value, schema.get("fields"), named)
    if kind == "enum":
        symbols = schema.get("symbols")
        return isinstance(value, str) and isinstance(symbols, list) and value in symbols
    if kind == "fixed":
        return _latin1(value) and len(value) == schema.get("size")
    if kind == "array":
        return isinstance(value, list) and all(_valid(v, schema.get("items"), named) for v in value)
    if kind == "map":
        return isinstance(value, dict) and all(_valid(v, schema.get("values"), named) for v in value.values())
    # A primitive written as an object, or a logical type: its underlying type.
    return _valid(value, kind, named)


def _record_value(value: dict[str, Any], fields: Any, named: Mapping[str, Any]) -> bool:
    """Each field is in `value` with a valid value, or has a default of its own. Extra keys are ignored."""
    if not isinstance(fields, list):
        return False
    for f in fields:
        if not isinstance(f, dict):
            return False
        name = f.get("name")
        if isinstance(name, str) and name in value:
            if not _valid(value[name], f.get("type"), named):
                return False
        elif "default" not in f:
            return False
    return True


def _describe(schema: Any) -> str:
    """A primitive's name, a named type's full name, "array" or "map"; a logical type is its underlying type."""
    while isinstance(schema, dict):
        name = schema.get("name")
        if schema.get("type") in _NAMED_TYPES and isinstance(name, str):
            return name
        schema = schema.get("type")
    if isinstance(schema, str):
        return schema
    return "union" if isinstance(schema, list) else "unknown"
