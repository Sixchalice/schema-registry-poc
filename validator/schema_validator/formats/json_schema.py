"""Stage B for JSON Schema (spec section 5.2)."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft7Validator, validator_for
from referencing import Specification
from referencing.jsonschema import DRAFT3, DRAFT4, DRAFT7, DRAFT202012, specification_with

from schema_validator import messages
from schema_validator.models import FormatAnalysis, ValidatorConfig

_TITLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+")

# Keywords whose value maps names (not keywords) to subschemas.
_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "definitions", "$defs", "dependentSchemas", "dependencies"}
)
# Keywords whose value is data, not a schema.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})


class _BadUri(Exception):
    """A `$id`, `id` or `$ref` value that urllib cannot parse."""

    def __init__(self, keyword: str, value: str) -> None:
        super().__init__(keyword, value)
        self.keyword = keyword
        self.value = value


def _join(keyword: str, base: str, value: str) -> str:
    """`urljoin(base, value)`, as referencing computes a base or a resource's URI."""
    try:
        # urljoin returns a value unparsed when the base is empty, so parse it explicitly.
        urlsplit(value)
        return urljoin(base, value)
    except ValueError:
        raise _BadUri(keyword, value) from None


def analyse(schema_text: str, referenced: dict[str, str], config: ValidatorConfig) -> FormatAnalysis:
    """Steps 1 to 9 of spec 5.2. `referenced` is not used for JSON."""
    # Step 1
    try:
        schema = json.loads(schema_text)
    except (ValueError, RecursionError) as e:
        return FormatAnalysis(None, None, [messages.NOT_JSON.format(err=e)])
    if not isinstance(schema, dict):
        return FormatAnalysis(None, None, [messages.JSON_TOP_LEVEL])

    errors: list[str] = []
    expected_name: str | None = None

    # Steps 2, 3 and 7
    title = schema.get("title")
    if not isinstance(title, str) or not title:
        errors.append(messages.JSON_TITLE_MISSING)
    elif not _TITLE_PATTERN.fullmatch(title):
        errors.append(messages.JSON_TITLE_FORMAT)
    else:
        expected_name = title

    # Step 4
    if schema.get("type") != "object":
        errors.append(messages.JSON_TOP_LEVEL)

    # Step 5
    draft_ok = True
    if "$schema" in schema:
        draft = schema["$schema"]
        if not isinstance(draft, str) or draft.removesuffix("#") not in config.allowed_json_drafts:
            shown = draft if isinstance(draft, str) else json.dumps(draft)
            errors.append(messages.JSON_DRAFT.format(x=shown))
            draft_ok = False

    # Step 6
    if draft_ok:
        try:
            validator_for(schema, default=Draft7Validator).check_schema(schema)
        except SchemaError as e:
            errors.append(messages.JSON_INVALID.format(err=e.message.partition("\n")[0]))
        except RecursionError:
            errors.append(messages.JSON_TOO_DEEP)

    # Steps 8 and 9 (a top-level javaType needs no check)
    try:
        refs: set[str] | None = set().union(*(_external_refs(schema, spec) for spec in _readings(schema)))
    except _BadUri as e:
        # The refs cannot be resolved, so stages D4 and D5 are skipped.
        errors.append(messages.JSON_INVALID.format(err=f"the {e.keyword} '{e.value}' is not a valid URI"))
        refs = None
    return FormatAnalysis(expected_name, refs, errors)


def _readings(schema: dict[str, Any]) -> list[Specification[Any]]:
    """The drafts whose rules decide how the `$ref`s resolve.

    The Python client resolves with the draft of the validator class jsonschema picks:
    the `$schema` one, 2020-12 for an unknown value, and 2020-12 without `$schema`.
    Without `$schema` the schema is also read as draft-07, which Confluent assumes.
    """
    if "$schema" not in schema:
        return [DRAFT7, DRAFT202012]
    draft = schema["$schema"]
    return [specification_with(draft, default=DRAFT202012) if isinstance(draft, str) else DRAFT202012]


def _id_of(spec: Specification[Any], keyword: str, node: dict[str, Any]) -> str | None:
    """The node's id as referencing reads it in this draft, or None. Non-string ids are ignored."""
    if not isinstance(node.get(keyword), str):
        return None
    return spec.create_resource(node).id()


def _subschema_ids(spec: Specification[Any], node: dict[str, Any]) -> set[int]:
    """The `id()` of each value that referencing treats as a subschema of `node` in this draft."""
    try:
        return {id(each) for each in spec.subresources_of(node)}
    except (AttributeError, TypeError):
        # A keyword value of the wrong type for this draft. Its meta-schema rejects it,
        # in stage B or in the client, so the schema is never used with this draft.
        return set()


def _dicts(values: Iterable[Any]) -> Iterator[dict[str, Any]]:
    """The objects in `values`, looking inside nested arrays."""
    pending = list(values)
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            yield value
        elif isinstance(value, list):
            pending.extend(value)


def _external_refs(schema: dict[str, Any], spec: Specification[Any]) -> set[str]:
    """Every string `$ref` that the Python client cannot resolve inside the document, as the URI it looks up.

    This follows `referencing`, which the client resolves with. A subschema's `$id` (`id` in
    draft-04) counts only where the draft knows the keyword above it, and in draft-07 and
    earlier not next to a `$ref`. The document's own resources are the root and every such
    `$id` reached from the root through known keywords alone. Below an unknown keyword the
    client's base depends on how it got there: a JSON pointer keeps the base of the last known
    resource, a descent from the node a pointer reached applies the `$id`s on the way. So a
    node there has several possible bases, and a `$ref` is external if any of them is.
    """
    keyword = "id" if spec in (DRAFT3, DRAFT4) else "$id"
    root_id = _id_of(spec, keyword, schema)
    root = _join(keyword, "", root_id) if root_id is not None else ""
    internal = {root}  # the URIs the client's registry holds for this document
    found: list[tuple[str, frozenset[str]]] = []  # (ref, possible bases)
    # (node, possible bases, base of the last known resource, reached through known keywords,
    #  and so far without a nested $schema, which makes referencing read the subtree by another draft)
    pending: list[tuple[dict[str, Any], frozenset[str], str, bool, bool]] = [
        (schema, frozenset({root}), root, True, True)]
    while pending:
        node, bases, anchor, known, listed = pending.pop()
        subschemas = _subschema_ids(spec, node)
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append((value, bases))
                continue
            if key in _DATA_KEYWORDS:
                continue
            children = value.values() if key in _NAME_MAPS and isinstance(value, dict) else [value]
            for child in _dicts(children):
                if id(child) not in subschemas:
                    # Only a JSON pointer reaches it, and the pointer stops moving the base here.
                    pending.append((child, frozenset({anchor}), anchor, False, False))
                    continue
                child_id = _id_of(spec, keyword, child)
                moved = bases if child_id is None else frozenset(_join(keyword, b, child_id) for b in bases)
                if known:
                    (base,) = moved
                    child_listed = listed and "$schema" not in child
                    if child_listed and child_id is not None:
                        internal.add(base)
                    pending.append((child, moved, base, True, child_listed))
                else:
                    # A pointer to the child itself does not apply its $id.
                    pending.append((child, moved | {anchor}, anchor, False, False))
    refs: set[str] = set()
    for ref, bases in found:
        for base in bases:
            # As Resolver.lookup: a "#..." ref looks up the base itself, any other ref drops its fragment.
            target = base if ref.startswith("#") else urldefrag(_join("$ref", base, ref)).url
            if target not in internal:
                refs.add(target)
    return refs
