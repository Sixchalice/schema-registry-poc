"""Stage B for JSON Schema (spec section 5.2)."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft7Validator, validator_for

from schema_validator import messages
from schema_validator.models import FormatAnalysis, ValidatorConfig

_TITLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+")

# Keywords whose value maps names (not keywords) to subschemas.
_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "definitions", "$defs", "dependentSchemas", "dependencies"}
)
# Keywords whose value is data, not a schema.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})
_DRAFT04 = "http://json-schema.org/draft-04/schema"


class _BadUri(Exception):
    """A `$id`, `id` or `$ref` value that urllib cannot parse."""

    def __init__(self, keyword: str, value: str) -> None:
        super().__init__(keyword, value)
        self.keyword = keyword
        self.value = value


def _resolve(keyword: str, base: str, value: str) -> str:
    """`value` resolved against `base`, without its fragment."""
    try:
        # urljoin returns a value unparsed when the base is empty, so parse it explicitly.
        urlsplit(value)
        return urldefrag(urljoin(base, value)).url
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
    id_keyword = "$id"
    if "$schema" in schema:
        draft = schema["$schema"]
        if isinstance(draft, str) and draft.removesuffix("#") == _DRAFT04:
            id_keyword = "id"
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
        refs: set[str] | None = _external_refs(schema, id_keyword)
    except _BadUri as e:
        # The refs cannot be resolved, so stages D4 and D5 are skipped.
        errors.append(messages.JSON_INVALID.format(err=f"the {e.keyword} '{e.value}' is not a valid URI"))
        refs = None
    return FormatAnalysis(expected_name, refs, errors)


def _external_refs(schema: dict[str, Any], id_keyword: str) -> set[str]:
    """Every string `$ref` that points outside the document, resolved against its base URI, without its fragment.

    The base starts as the root's `id_keyword` and moves with each string `$id` (or `id` in draft-04) of a subschema.
    """
    root_id = schema.get(id_keyword)
    root_base = _resolve(id_keyword, "", root_id) if isinstance(root_id, str) else ""
    bases = {root_base}
    found: list[tuple[str, str]] = []  # (ref, base in scope)
    pending: list[tuple[Any, str]] = [(schema, root_base)]
    # Pass 1: collect every base set inside the document, and every ref with the base in scope.
    while pending:
        node, base = pending.pop()
        if isinstance(node, list):
            pending.extend((item, base) for item in node)
        elif isinstance(node, dict):
            node_id = node.get(id_keyword)
            if isinstance(node_id, str) and node is not schema:
                base = _resolve(id_keyword, base, node_id)
                bases.add(base)
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    found.append((value, base))
                elif key in _NAME_MAPS and isinstance(value, dict):
                    pending.extend((item, base) for item in value.values())
                elif key not in _DATA_KEYWORDS:
                    pending.append((value, base))
    # Pass 2: resolve the refs. "" is the document itself, a base set inside it is one of its subschemas.
    refs: set[str] = set()
    for ref, base in found:
        target = _resolve("$ref", base, ref)
        if target not in bases:
            refs.add(target)
    return refs
