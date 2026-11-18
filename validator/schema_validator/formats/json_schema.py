"""Stage B for JSON Schema (spec section 5.2)."""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote_to_bytes, urldefrag, urljoin, urlsplit

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft7Validator, validator_for
from referencing import Registry
from referencing.jsonschema import DRAFT7, specification_with

from schema_validator import messages
from schema_validator.formats._depth import json_depth
from schema_validator._text import json_values, shorten
from schema_validator.models import FormatAnalysis, ValidatorConfig

if TYPE_CHECKING:
    from collections.abc import Iterable

    from referencing._core import Resolver

# The most characters of a parser's own text that go into one message.
_ERR_LIMIT = 1000
_TITLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+")

# Keywords whose value maps names (not keywords) to subschemas.
_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "definitions", "$defs", "dependentSchemas", "dependencies"}
)
# Keywords whose value is data, not a schema.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples", "example"})
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
    """Steps 1 to 11 of spec 5.2. `referenced` maps reference names to schema texts; step 11 reads
    the ones a "$ref" with a fragment points at."""
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

    # Step 5a: the walk comes first, because check_schema takes quadratic time on some long lists
    ref_places, nested_ids, nested_schemas, format_places, long_lists = _walk(
        schema, id_keyword, draft == _DRAFT_04
    )
    if long_lists:
        errors.append(messages.JSON_ARRAY_TOO_LONG.format(paths=_paths(long_lists)))

    # Step 6
    if draft_ok and not long_lists:
        try:
            validator_for(schema, default=Draft7Validator).check_schema(schema)
        except SchemaError as e:
            errors.append(messages.JSON_INVALID.format(err=_metaschema_text(e.message, e.path)))
        except RecursionError:
            errors.append(messages.JSON_TOO_DEEP)

    # Steps 6a to 6d: where "$id" and "$schema" may be (the order is C, D, A, B)
    if "$schema" not in schema:
        errors.append(messages.JSON_SCHEMA_MISSING)
    root_id = schema.get(id_keyword)
    rule_errors = False
    if draft in _REF_SIBLINGS_IGNORED and "$ref" in schema and id_keyword in schema:
        errors.append(messages.JSON_ROOT_ID_WITH_REF.format(kw=id_keyword))
        rule_errors = True
    if draft in _REF_SIBLINGS_IGNORED and isinstance(root_id, str) and _has_root_fragment(id_keyword, root_id):
        errors.append(messages.JSON_ROOT_ID_FRAGMENT.format(kw=id_keyword, value=root_id))
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
    try:
        base = urldefrag(_join(id_keyword, "", root_id)).url if isinstance(root_id, str) else ""
        targets = [(pointer, ref, _target(base, ref)) for pointer, ref in ref_places]
    except _BadUri as e:
        errors.append(messages.JSON_INVALID.format(err=shorten(f"the {e.keyword} '{e.value}' is not a valid URI", _ERR_LIMIT)))
        return FormatAnalysis(expected_name, None, errors)
    refs = {target for _, _, target in targets if target not in ("", base)}
    # The registry rejects a "$ref" to this schema written with its own URL and a "#" under
    # draft-04 to 07 (and with no "$schema", which it reads as draft-07); under 2019-09 and
    # 2020-12 it reads one as a "#" ref, so its fragment gets the same pointer check.
    self_url_rejected = draft in _REF_SIBLINGS_IGNORED or "$schema" not in schema
    self_urls = []
    not_found = []
    differ = []
    root = _python_resolver(base or _ROOT_URI, schema)
    documents: dict[str, tuple[_PythonReading, Any] | None] = {}
    for pointer, ref, target in targets:
        fragment = urldefrag(ref).fragment
        if target not in ("", base):
            # Step 11, external: the Python client must find the fragment in the referenced schema,
            # and where the registry's reading is pinned, both must reach the same place there. A
            # fragment the registry cannot follow is left to the registry (stage D4 or a rejection).
            if fragment and target not in documents:
                documents[target] = _reference_resolver(target, referenced)
            document = documents.get(target)
            if document is not None:
                reading, contents = document
                in_python = _python_target(reading, "#" + fragment)
                in_registry = _registry_target(contents, fragment)
                if in_python is _MISSING or (in_registry is not _MISSING and in_registry is not _UNCHECKED
                                             and in_registry is not in_python):
                    differ.append(pointer)
        elif self_url_rejected and "#" in ref and not ref.startswith("#"):
            self_urls.append(pointer)
        else:
            # Step 11: the registry and the Python client must both resolve it, to the same place.
            in_registry = _registry_target(schema, fragment)
            if in_registry is _MISSING:
                not_found.append(pointer)
                continue
            in_python = _python_target(root, ref)
            if in_python is _MISSING or (in_registry is not _UNCHECKED and in_registry is not in_python):
                differ.append(pointer)
    if self_urls:
        errors.append(messages.JSON_REF_SELF_URL.format(kw=id_keyword, paths=_paths(self_urls)))
    if not_found:
        errors.append(messages.JSON_REF_NOT_FOUND.format(paths=_paths(not_found)))
    if differ:
        errors.append(messages.JSON_REF_CLIENTS_DIFFER.format(paths=_paths(differ)))
    warnings = [messages.JSON_FORMAT_NOT_CHECKED.format(paths=_paths(format_places))] if format_places and not errors else []
    return FormatAnalysis(expected_name, refs, errors, warnings)


# A "%" that is not followed by two upper-case hex digits: the registry's reading of it is not pinned.
_ODD_PERCENT = re.compile(r"%(?![0-9A-F]{2})")
# What `_registry_target` returns when the registry would not resolve the fragment, and when its
# reading is not pinned (an anchor, an odd encoding, non-UTF-8). `_python_target` uses `_MISSING` too.
_MISSING = object()
_UNCHECKED = object()
# The URI the root is registered under when it has no id.
_ROOT_URI = "urn:schema-validator:root"


def _token(raw: str) -> str | None:
    """One pointer token as the registry reads it: "+" is a space, then percent-decoding, then
    `~1` and `~0`. None when the bytes are not UTF-8."""
    try:
        text = unquote_to_bytes(raw.replace("+", " ")).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.replace("~1", "/").replace("~0", "~")


def _registry_target(schema: Any, fragment: str) -> object:
    """The node the registry reaches for a "#" fragment of this schema, `_MISSING` when it would
    not resolve it, and `_UNCHECKED` for an anchor, an odd encoding or non-UTF-8.

    The registry splits on "/" first, decodes each token, and ignores empty tokens at the end.
    A list index is ASCII digits, leading zeros allowed, below the length.
    """
    if not fragment:
        return schema
    if not fragment.startswith("/") or _ODD_PERCENT.search(fragment):
        return _UNCHECKED
    raw_tokens = fragment[1:].split("/")
    while raw_tokens and not raw_tokens[-1]:
        raw_tokens.pop()
    node: Any = schema
    for raw in raw_tokens:
        token = _token(raw)
        if token is None:
            return _UNCHECKED
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isascii() and token.isdecimal() and int(token) < len(node):
            node = node[int(token)]
        else:
            return _MISSING
    return node


# A `referencing` resolver for one document, and whether crawling the document succeeded.
_PythonReading = tuple["Resolver[Any]", bool]


def _python_resolver(uri: str, contents: Any) -> _PythonReading:
    """A `referencing` resolver, as the Python client uses, for `contents` registered under `uri` as
    a resource of its "$schema" draft (draft-07 when it has none or one `referencing` does not know),
    and whether its crawl succeeded."""
    declared = contents.get("$schema") if isinstance(contents, dict) else None
    spec = specification_with(declared.removesuffix("#"), default=DRAFT7) if isinstance(declared, str) else DRAFT7
    registry = Registry().with_resource(uri, spec.create_resource(contents))
    # Crawled once, so that each anchor lookup does not crawl the whole schema again. A malformed
    # subschema makes the crawl fail, and then every anchor lookup would crawl and fail again.
    try:
        registry = registry.crawl()
    except Exception:
        return registry.resolver(base_uri=uri), False
    return registry.resolver(base_uri=uri), True


def _reference_resolver(target: str, referenced: dict[str, str]) -> tuple[_PythonReading, Any] | None:
    """The resolver for the referenced text named `target`, with its parsed contents; None when it
    is not there or is not JSON (stage D4 reports a missing reference)."""
    text = referenced.get(target)
    if text is None:
        return None
    try:
        contents = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return _python_resolver(target, contents), contents


def _python_target(reading: _PythonReading, ref: str) -> object:
    """The node the Python client reaches for `ref`, or `_MISSING` when it cannot follow it."""
    resolver, crawled = reading
    fragment = ref.partition("#")[2]
    if not crawled and fragment and not fragment.startswith("/"):
        # An anchor lookup crawls the document, which fails for the Python client too. Not
        # calling it keeps many anchors in a malformed schema from costing a crawl each.
        return _MISSING
    try:
        return resolver.lookup(ref).contents
    except Exception:
        # referencing raises Unresolvable for a target that is not there, and a TypeError or
        # ValueError on a malformed subschema or a list index that is not a number: the Python
        # client cannot follow the "$ref" either way.
        return _MISSING


def _has_root_fragment(keyword: str, value: str) -> bool:
    """True when the id has a non-empty part before the "#" and a non-empty fragment after it."""
    try:
        split = urldefrag(_join(keyword, "", value))
    except _BadUri:
        return False  # reported as an invalid URI in steps 8 and 9
    return bool(split.url and split.fragment)


def _metaschema_text(message: str, path: Iterable[Any]) -> str:
    """The first line of a meta-schema error in JSON terms, with ` (at /pointer)` unless at the root,
    at most _ERR_LIMIT characters. The text is cut before the location, so a long value cannot cut
    the location off; a location that alone is too long is cut with the rest."""
    where = "".join(f"/{_escape(str(part))}" for part in path)
    text = json_values(message.partition("\n")[0])
    suffix = f" (at {where})" if where else ""
    if len(suffix) >= _ERR_LIMIT:
        return shorten(text + suffix, _ERR_LIMIT)
    return shorten(text, _ERR_LIMIT - len(suffix)) + suffix


def _escape(token: str) -> str:
    """One JSON Pointer token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


_LONG_LIST = 1000


def _quadratic(items: list[Any]) -> bool:
    """A list longer than _LONG_LIST that is not all strings and not all numbers (a bool is not a number)."""
    if len(items) <= _LONG_LIST:
        return False
    if all(isinstance(each, str) for each in items):
        return False
    return not all(isinstance(each, (int, float)) and not isinstance(each, bool) for each in items)


def _walk(
    schema: dict[str, Any], id_keyword: str, long_enum: bool
) -> tuple[list[tuple[str, str]], list[str], list[str], list[str], list[str]]:
    """The string `$ref` values with the JSON Pointer of each `$ref` keyword, and the JSON Pointers of the nested objects that have the id keyword
    and of those that have `$schema`, and the JSON Pointers of the `format` keywords with a string value,
    and the JSON Pointers of the `required`, `type`, `dependencies` (each list value) (and, if `long_enum`, `enum`) lists that jsonschema
    checks for duplicates in quadratic time.

    Visits the objects a schema can hold: name maps (`properties` and the like) are names, not
    keywords; `enum`, `const`, `default`, `examples` and
    `example` are data; lists are walked. It is iterative,
    so a deep schema costs no recursion.
    """
    refs: list[tuple[str, str]] = []
    with_id: list[str] = []
    with_schema: list[str] = []
    formats: list[str] = []
    long_lists: list[str] = []
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
            if (
                (key in ("required", "type") or (key == "enum" and long_enum))
                and isinstance(child, list)
                and _quadratic(child)
            ):
                long_lists.append(f"{pointer}/{key}")
            if key == "dependencies" and isinstance(child, dict):
                long_lists.extend(
                    f"{pointer}/dependencies/{_escape(name)}"
                    for name, each in child.items()
                    if isinstance(each, list) and _quadratic(each)
                )
            if key == "$ref" and isinstance(child, str):
                refs.append((f"{pointer}/$ref", child))
            elif key == "format" and isinstance(child, str):
                formats.append(f"{pointer}/format")
            elif key in _DATA_KEYWORDS:
                continue
            elif key in _NAME_MAPS and isinstance(child, dict):
                pending.extend((each, f"{pointer}/{_escape(key)}/{_escape(name)}") for name, each in child.items())
            else:
                pending.append((child, f"{pointer}/{_escape(key)}"))
    return refs, with_id, with_schema, formats, long_lists


def _paths(pointers: list[str]) -> str:
    """Up to three sorted pointers in single quotes, then how many more."""
    ordered = sorted(pointers)
    shown = ", ".join(f"'{each}'" for each in ordered[:3])
    return shown + (f" and {len(ordered) - 3} more" if len(ordered) > 3 else "")


def _target(base: str, ref: str) -> str:
    """The document a `$ref` points at, without its fragment."""
    return urldefrag(_join("$ref", base, ref)).url
