"""Stage B for JSON Schema (spec section 5.2)."""
from __future__ import annotations

import json
import re
from typing import Any

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

    # Steps 8 and 9 (a top-level javaType needs no check)
    return FormatAnalysis(expected_name, _external_refs(schema), errors)


def _external_refs(schema: dict[str, Any]) -> set[str]:
    """Every string `$ref` of a schema object that points outside the document, without its fragment."""
    refs: set[str] = set()
    pending: list[Any] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    target = value.partition("#")[0]
                    # "" (or "#...") is the document itself.
                    if target:
                        refs.add(target)
                elif key in _NAME_MAPS and isinstance(value, dict):
                    pending.extend(value.values())
                elif key not in _DATA_KEYWORDS:
                    pending.append(value)
    return refs
