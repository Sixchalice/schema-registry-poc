from __future__ import annotations

import time

import pytest

from schema_validator._text import json_values


def test_json_values_leaves_unparseable_text() -> None:
    assert json_values("Default value <not python> must") == "Default value <not python> must"
    assert json_values("") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Default value <None> must match schema type: string", "Default value null must match schema type: string"),
        ("Record field is missing 'name': {'type': 'int'}", 'Record field is missing \'name\': {"type": "int"}'),
        ("Bad entries: [True, None]", "Bad entries: [true, null]"),
        ("True is not of type 'array'", "true is not of type 'array'"),
        ("None is not of type 'string'", "null is not of type 'string'"),
        ("[] is too short", "[] is too short"),
        ("{'a': 1} is too long", '{"a": 1} is too long'),
        ("'abc' is not valid", "'abc' is not valid"),
        ("Value <1> and <True> end", "Value 1 and true end"),
        ("Trailing: not a literal", "Trailing: not a literal"),
        ("a: {x} b: {y} c: {z} d: {w}", "a: {x} b: {y} c: {z} d: {w}"),
        ("Plain text", "Plain text"),
        ("<> empty", "<> empty"),
        ("a < b > c", "a < b > c"),
    ],
)
def test_json_values_converts_python_literals(text: str, expected: str) -> None:
    assert json_values(text) == expected


def test_json_values_survives_hostile_text() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    for text in (f"Bad: {deep}", f"<{deep}>", f"{deep} is not x", "<" * 50_000, "<a" * 50_000, "x: " * 50_000):
        start = time.monotonic()
        json_values(text)
        assert time.monotonic() - start < 2
