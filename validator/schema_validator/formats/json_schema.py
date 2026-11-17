"""Stage B for JSON Schema (spec section 5.2)."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft7Validator, validator_for

from schema_validator import messages
from schema_validator.formats._depth import json_depth
from schema_validator.models import FormatAnalysis, ValidatorConfig

_TITLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+")

# Keywords whose value maps names (not keywords) to subschemas.
_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "definitions", "$defs", "dependentSchemas", "dependencies"}
)
# Keywords whose value is data, not a schema.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})
_DRAFT_04 = "http://json-schema.org/draft-04/schema"
# Drafts in which a "$ref" makes its sibling keywords, the id among them, ignored.
_REF_SIBLINGS_IGNORED = frozenset(
    {_DRAFT_04, "http://json-schema.org/draft-06/schema", "http://json-schema.org/draft-07/schema"}
)


class _BadUri(Exception):
    """A `$id`, `id` or `$ref` value that urllib cannot parse."""

    def __init__(self, keyword: str, value: str) -> None:
        super().__init__(keyword, value)
        self.keyword = keyword
        self.value = value


def _join(keyword: str, base: str, value: str) -> str:
    """`urljoin(base, value)`, with a value urllib cannot parse reported as _BadUri."""
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
    except RecursionError:
        return FormatAnalysis(None, None, [messages.SCHEMA_TOO_DEEP.format(max=config.max_nesting_depth)])
    except ValueError as e:
        return FormatAnalysis(None, None, [messages.NOT_JSON.format(err=e)])
    if json_depth(schema, config.max_nesting_depth) > config.max_nesting_depth:
        return FormatAnalysis(None, None, [messages.SCHEMA_TOO_DEEP.format(max=config.max_nesting_depth)])
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
    declared = schema.get("$schema")
    if "$schema" in schema:
        if not isinstance(declared, str) or declared.removesuffix("#") not in config.allowed_json_drafts:
            shown = declared if isinstance(declared, str) else json.dumps(declared)
            errors.append(messages.JSON_DRAFT.format(x=shown))
            draft_ok = False
    draft = declared.removesuffix("#") if draft_ok and isinstance(declared, str) else None
    id_keyword = "id" if draft == _DRAFT_04 else "$id"

    # Step 6
    if draft_ok:
        try:
            validator_for(schema, default=Draft7Validator).check_schema(schema)
        except SchemaError as e:
            errors.append(messages.JSON_INVALID.format(err=e.message.partition("\n")[0]))
        except RecursionError:
            errors.append(messages.JSON_TOO_DEEP)

    # Steps 6a to 6d: where "$id" and "$schema" may be (the order is C, D, A, B)
    ref_values, nested_ids, nested_schemas = _walk(schema, id_keyword)
    if "$schema" not in schema:
        errors.append(messages.JSON_SCHEMA_MISSING)
    rule_errors = False
    if draft in _REF_SIBLINGS_IGNORED and "$ref" in schema and id_keyword in schema:
        errors.append(messages.JSON_ROOT_ID_WITH_REF.format(kw=id_keyword))
        rule_errors = True
    if nested_ids:
        errors.append(messages.JSON_NESTED_ID.format(kw=id_keyword, paths=_paths(nested_ids)))
        rule_errors = True
    if nested_schemas:
        errors.append(messages.JSON_NESTED_SCHEMA.format(paths=_paths(nested_schemas)))
        rule_errors = True
    if rule_errors:
        # The references cannot be trusted, so stages D4 and D5 are skipped.
        return FormatAnalysis(expected_name, None, errors)

    # Steps 8 and 9 (a top-level javaType needs no check)
    root_id = schema.get(id_keyword)
    try:
        base = urldefrag(_join(id_keyword, "", root_id)).url if isinstance(root_id, str) else ""
        refs = {target for ref in ref_values if (target := _target(base, ref)) not in ("", base)}
    except _BadUri as e:
        errors.append(messages.JSON_INVALID.format(err=f"the {e.keyword} '{e.value}' is not a valid URI"))
        return FormatAnalysis(expected_name, None, errors)
    return FormatAnalysis(expected_name, refs, errors)


def _escape(token: str) -> str:
    """One JSON Pointer token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _walk(schema: dict[str, Any], id_keyword: str) -> tuple[list[str], list[str], list[str]]:
    """The string `$ref` values, and the JSON Pointers of the nested objects that have the id keyword
    and of those that have `$schema`.

    Visits the objects a schema can hold: name maps (`properties` and the like) are names, not
    keywords; `enum`, `const`, `default` and `examples` are data; lists are walked. It is iterative,
    so a deep schema costs no recursion.
    """
    refs: list[str] = []
    with_id: list[str] = []
    with_schema: list[str] = []
    pending: list[tuple[Any, str]] = [(schema, "")]
    while pending:
        value, pointer = pending.pop()
        if isinstance(value, list):
            pending.extend((item, f"{pointer}/{index}") for index, item in enumerate(value))
            continue
        if not isinstance(value, dict):
            continue
        if pointer:  # the root is allowed both
            if id_keyword in value:
                with_id.append(pointer)
            if "$schema" in value:
                with_schema.append(pointer)
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                refs.append(child)
            elif key in _DATA_KEYWORDS:
                continue
            elif key in _NAME_MAPS and isinstance(child, dict):
                pending.extend((each, f"{pointer}/{_escape(key)}/{_escape(name)}") for name, each in child.items())
            else:
                pending.append((child, f"{pointer}/{_escape(key)}"))
    return refs, with_id, with_schema


def _paths(pointers: list[str]) -> str:
    """Up to three sorted pointers in single quotes, then how many more."""
    ordered = sorted(pointers)
    shown = ", ".join(f"'{each}'" for each in ordered[:3])
    return shown + (f" and {len(ordered) - 3} more" if len(ordered) > 3 else "")


def _target(base: str, ref: str) -> str:
    """The document a `$ref` points at, without its fragment."""
    return urldefrag(_join("$ref", base, ref)).url
