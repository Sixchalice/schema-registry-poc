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
    if not isinstance(schema, dict) or not isinstance(schema.get("type"), str) or schema["type"] not in _NAMED_TYPES:
        return FormatAnalysis(None, None, [messages.AVRO_TOP_LEVEL])

    errors: list[str] = []
    expected_name: str | None = None

    # Steps 3 to 5. A name that is not a string is left to fastavro (step 7).
    name = schema.get("name")
    if isinstance(name, str):
        full_name = _full_name(name, _own_namespace(schema, None))
        if _well_formed(full_name):
            expected_name = full_name
        else:
            errors.append(messages.AVRO_NAMESPACE)

    # Step 6
    defined, used, nested_errors = _names(schema, submitted=True)
    errors.extend(nested_errors)
    external_refs, unresolved, nested_in_reference = _external_refs(defined, used, referenced)
    errors.extend(nested_in_reference)

    # Step 7. With a nested-namespace error the walk and fastavro name that type
    # differently, so fastavro not finding a defined name is already explained.
    error = _parse_error(schema, referenced, defined, unresolved, explained=bool(nested_errors))
    if error is not None:
        errors.append(messages.AVRO_INVALID.format(err=error))
    elif not isinstance(name, str):
        # fastavro lets a list or dict through as a name, or stopped before looking at it.
        errors.append(messages.AVRO_INVALID.format(err=f'the {schema["type"]} "name" must be a string'))

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


def _external_refs(
    defined: set[str], used: set[str], referenced: dict[str, str]
) -> tuple[set[str], set[str], list[str]]:
    """The reference names the text needs, the used names nothing defines, and one
    error for every used name that is nested inside a referenced schema.

    A used name the text does not define itself is external. An unqualified name
    resolves against the enclosing namespace only: fastavro has no null-namespace
    fallback, so a schema relying on it does not parse in Python clients. A name
    defined inside a referenced schema (an enum nested in a referenced record) is an
    error: the registry resolves it, but neither the Python nor the Java serializer
    can produce with such a schema. It still counts as a use of that reference, so D4 and D5 add nothing.
    """
    top_level, nested = _reference_names(referenced)
    refs: set[str] = set()
    unresolved: set[str] = set()
    errors: list[str] = []
    for name in sorted(used - defined):
        if name in top_level:
            refs.add(name)
        elif name in nested:
            key, owner = nested[name]
            refs.add(key)
            errors.append(messages.AVRO_NESTED_IN_REFERENCE.format(name=name, schema=owner))
        else:
            refs.add(name)
            unresolved.add(name)
    return refs, unresolved, errors


def _reference_names(referenced: dict[str, str]) -> tuple[set[str], dict[str, tuple[str, str]]]:
    """The referenced texts' own (top-level) full names, and every other name they
    define mapped to the first text that defines it: (its key, its own name or else its key)."""
    top_level: set[str] = set()
    nested: dict[str, tuple[str, str]] = {}
    for key, text in referenced.items():
        try:
            root = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            continue  # not this schema's error (see _parse_error)
        defined, _, _ = _names(root)
        own: str | None = None
        if isinstance(root, dict) and isinstance(root.get("name"), str):
            own = _full_name(root["name"], _own_namespace(root, None))
            top_level.add(own)
        for name in defined - {own}:
            nested.setdefault(name, (key, own or key))
    return top_level, nested


def _well_formed(full_name: str) -> bool:
    """A full name with a namespace and no empty dotted part (not "Order", ".Order", "com..Order")."""
    parts = full_name.split(".")
    return len(parts) > 1 and all(parts)


def _names(schema: Any, submitted: bool = False) -> tuple[set[str], set[str], list[str]]:
    """The full names a text defines, the full names it uses in type positions, and
    one error (in document order) for every nested named type that ends up without a
    usable namespace although the type enclosing it has one."""
    defined: set[str] = set()
    used: set[str] = set()
    nested_errors: list[str] = []
    # Each entry is a value in a type position and the namespace enclosing it.
    # Children are pushed in reverse, so the stack pops them in document order.
    pending: list[tuple[Any, str | None]] = [(schema, None)]
    while pending:
        node, namespace = pending.pop()
        if isinstance(node, str):
            if node not in _BUILTIN_TYPES:
                used.add(_full_name(node, namespace))
        elif isinstance(node, list):
            pending.extend((member, namespace) for member in reversed(node))
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
                    # The root is step 4's; under a root without a namespace a plain
                    # name is only inheriting the gap, which step 4 already reports.
                    if node is not schema:
                        own = node.get("namespace")
                        if "." in name:
                            if not _well_formed(name):
                                nested_errors.append(messages.AVRO_NESTED_NAME.format(name=name))
                        elif namespace and (not _well_formed(full) or (own is not None or "namespace" in node)
                                            and not isinstance(own, str)):
                            # "", a malformed namespace, or one that is not a string: Java Avro
                            # inherits the enclosing namespace for null, fastavro uses none.
                            nested_errors.append(messages.AVRO_NESTED_NAMESPACE.format(name=name))
                            # In the submitted schema only (the error is reported there): the
                            # name it gets once the entry is removed, so that a bare use of
                            # it is local and D4 does not repeat this error in other words.
                            # A referenced text defines exactly what it says.
                            if submitted:
                                defined.add(f"{namespace}.{name}")
                fields = node.get("fields") if kind == "record" else None
                if isinstance(fields, list):
                    pending.extend(
                        (f["type"], inner) for f in reversed(fields) if isinstance(f, dict) and "type" in f
                    )
            elif kind == "array":
                if "items" in node:
                    pending.append((node["items"], namespace))
            elif kind == "map":
                if "values" in node:
                    pending.append((node["values"], namespace))
            elif kind is not None:
                pending.append((kind, namespace))
    return defined, used, nested_errors


def _parse_error(
    schema: dict[str, Any], referenced: dict[str, str], defined: set[str], unresolved: set[str],
    explained: bool = False,
) -> str | None:
    """fastavro's error for the schema, parsed after its references; None if it parses.

    `defined` are the names the text defines and `unresolved` the used names the walk
    found no definition for. fastavro stops at the first unknown type, so after one the
    rest of the schema is not checked: it is let through only when stage D reports it,
    which it does not when a reference row carries the name (D4 matches rows by name).
    """
    named: dict[str, Any] = {}
    for text in referenced.values():
        try:
            fastavro.parse_schema(json.loads(text), named_schemas=named)
        except Exception:
            # A reference fastavro cannot parse is not this schema's error.
            pass
    try:
        fastavro.parse_schema(schema, named_schemas=named)
    except UnknownType as e:
        # Caught first: it is not a SchemaParseException.
        name = e.name
        while isinstance(name, dict):  # fastavro gives {"type": "X"} for a wrapped name
            name = name.get("type")
        missing = isinstance(name, str) and name in unresolved and name not in referenced
        if isinstance(e.name, dict) and isinstance(name, str) and not missing:
            # The registry resolves a wrapped name; fastavro (Python clients) does not.
            return f"write the type '{name}' as a plain string, not as {{\"type\": \"{name}\"}}"
        if isinstance(name, str) and name in unresolved:
            if name not in referenced:
                return None  # A missing reference; stage D (D4) explains it.
            # A row carries this name, so D4 stays silent, but its text does not define it.
            return f"type '{name}' is not defined by the reference selected for it"
        if isinstance(name, str) and name in defined:
            if explained:
                return None
            return f"type '{name}' is used before it is defined"
        # The walk took this name as provided, so nothing else would report the stop.
        return f"unknown type {name!r}" if isinstance(name, str) else f"unknown type {e.name!r}"
    except KeyError as e:
        # A type without a required entry: {"type": "array"} has no "items".
        return f"a type is missing its {e.args[0]!r} entry" if e.args else "a type is missing an entry"
    except Exception as e:
        # SchemaParseException, and also TypeError, ValueError etc. on malformed schemas.
        return str(e)
    return None
