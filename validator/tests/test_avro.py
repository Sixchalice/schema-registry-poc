from __future__ import annotations

import json
from typing import Any

import pytest

from schema_validator.formats import avro
from schema_validator.formats.avro import analyse
from schema_validator.models import FormatAnalysis, ValidatorConfig

TOP_LEVEL = ["The top level of an Avro schema must be a single record."]
NAMESPACE = ['Avro schema needs a namespace, for example "namespace": "com.team.domain".']


def run(d: Any, referenced: dict[str, str] | None = None) -> FormatAnalysis:
    return analyse(json.dumps(d), {} if referenced is None else referenced, ValidatorConfig())


def rec(name: str, fields: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {"type": "record", "name": name, "fields": fields, **kw}


def field(name: str, type_: Any, **kw: Any) -> dict[str, Any]:
    return {"name": name, "type": type_, **kw}


ADDRESS = rec("Address", [field("street", "string")], namespace="com.acme")


def test_valid() -> None:
    result = run(rec("Order", [field("id", "string")], namespace="com.acme"))
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_not_json() -> None:
    result = analyse("{not json", {}, ValidatorConfig())
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Schema is not valid JSON: ")
    assert result.external_refs is None


def test_deeply_nested_json_is_not_json() -> None:
    # json.loads raises RecursionError, not ValueError, at about 1000 levels.
    result = analyse("[" * 100_000, {}, ValidatorConfig())
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Schema is not valid JSON: ")
    assert result.external_refs is None


def test_top_level_union() -> None:
    result = run(["null", rec("Order", [field("id", "string")], namespace="com.acme")])
    assert result.errors == TOP_LEVEL
    assert result.external_refs is None


def test_top_level_enum() -> None:
    result = run({"type": "enum", "name": "E", "namespace": "com.acme", "symbols": ["A"]})
    assert result.errors == TOP_LEVEL
    assert result.external_refs is None


def test_top_level_primitive() -> None:
    result = run("string")
    assert result.errors == TOP_LEVEL
    assert result.external_refs is None


def test_top_level_array() -> None:
    result = run({"type": "array", "items": "string"})
    assert result.errors == TOP_LEVEL
    assert result.external_refs is None


def test_no_namespace() -> None:
    result = run(rec("Order", [field("id", "string")]))
    assert result.errors == NAMESPACE
    assert result.expected_name is None
    assert result.external_refs == set()


def test_no_namespace_still_computes_refs_and_validates() -> None:
    result = run(rec("Order", [field("a", "string", default=5), field("b", "com.other.Address")]))
    assert result.external_refs == {"com.other.Address"}
    assert result.errors[0] == NAMESPACE[0]
    assert len(result.errors) == 2
    assert result.errors[1].startswith("Invalid Avro schema: ")


def test_empty_namespace_is_no_namespace() -> None:
    result = run(rec("Order", [field("id", "string")], namespace=""))
    assert result.errors == NAMESPACE
    assert result.expected_name is None


@pytest.mark.parametrize(
    "name, namespace",
    [(".Order", None), ("com..Order", None), ("com.acme.", None), ("Order", "com.acme."), ("Order", ".com")],
)
def test_empty_name_part_is_no_namespace(name: str, namespace: str | None) -> None:
    extra = {} if namespace is None else {"namespace": namespace}
    result = run(rec(name, [field("id", "string")], **extra))
    assert result.errors == NAMESPACE
    assert result.expected_name is None


def test_dotted_name_wins() -> None:
    result = run(rec("com.real.Order", [field("id", "string")], namespace="ignored.ns"))
    assert result == FormatAnalysis("com.real.Order", set(), [])


def test_dotted_name_sets_namespace_for_nested_types() -> None:
    # The nested Address takes the namespace of the dotted name, not "ignored.ns".
    result = run(
        rec(
            "com.real.Order",
            [field("addr", rec("Address", [field("s", "string")])), field("again", "com.real.Address")],
            namespace="ignored.ns",
        )
    )
    assert result == FormatAnalysis("com.real.Order", set(), [])


def test_nested_record_is_not_external() -> None:
    result = run(
        rec(
            "Order",
            [
                field("addr", rec("Address", [field("street", "string")])),
                field("billing", "Address"),
                field("shipping", "com.acme.Address"),
            ],
            namespace="com.acme",
        )
    )
    assert result.external_refs == set()
    assert result.errors == []


def test_external_qualified() -> None:
    result = run(rec("Order", [field("addr", "com.other.Address")], namespace="com.acme"))
    assert result.external_refs == {"com.other.Address"}


def test_external_unqualified_takes_namespace() -> None:
    result = run(rec("Order", [field("addr", "Address")], namespace="com.acme"))
    assert result.external_refs == {"com.acme.Address"}


def test_self_reference() -> None:
    result = run(rec("Order", [field("parent", ["null", "Order"])], namespace="com.acme"))
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_forward_use() -> None:
    result = run(
        rec(
            "Order",
            [
                field("first", "Later"),
                field("second", {"type": "enum", "name": "Later", "symbols": ["A"]}),
            ],
            namespace="com.acme",
        )
    )
    assert result.external_refs == set()
    # fastavro raises UnknownType for the forward use; that is not reported here.
    assert result.errors == []


def test_refs_in_array_map_union() -> None:
    result = run(
        rec(
            "Order",
            [
                field("xs", {"type": "array", "items": "a.X"}),
                field("ys", {"type": "map", "values": "a.Y"}),
                field("z", ["null", "a.Z"]),
            ],
            namespace="com.acme",
        )
    )
    assert result.external_refs == {"a.X", "a.Y", "a.Z"}


def test_unqualified_use_falls_back_to_null_namespace() -> None:
    # Java Avro looks an unqualified name up in the enclosing namespace, then in the null one.
    result = run(
        rec(
            "Order",
            [field("inner", rec("Inner", [field("x", "string")], namespace="")), field("again", "Inner")],
            namespace="com.acme",
        )
    )
    assert result.external_refs == set()


def test_enclosing_namespace_is_tried_first() -> None:
    # Both com.acme.Inner and the null-namespace Inner exist: the use is local either way.
    result = run(
        rec(
            "Order",
            [
                field("a", rec("Inner", [field("x", "string")], namespace="")),
                field("b", rec("Inner", [field("y", "string")])),
                field("c", "Inner"),
            ],
            namespace="com.acme",
        )
    )
    assert result.external_refs == set()


def test_qualified_use_has_no_null_namespace_fallback() -> None:
    # Only an unqualified name falls back: "other.Inner" is not the null-namespace Inner.
    result = run(
        rec(
            "Order",
            [field("inner", rec("Inner", [field("x", "string")], namespace="")), field("again", "other.Inner")],
            namespace="com.acme",
        )
    )
    assert result.external_refs == {"other.Inner"}


def test_nested_namespace() -> None:
    result = run(
        rec(
            "Order",
            [field("inner", rec("Inner", [field("thing", "Thing")], namespace="other"))],
            namespace="com.acme",
        )
    )
    assert result.external_refs == {"other.Thing"}


def test_type_object_with_name() -> None:
    result = run(rec("Order", [field("addr", {"type": "com.other.Address"})], namespace="com.acme"))
    assert result.external_refs == {"com.other.Address"}


def test_defaults_are_not_type_names() -> None:
    result = run(
        rec(
            "Order",
            [field("kind", {"type": "enum", "name": "Kind", "symbols": ["A", "B"]}, default="A")],
            namespace="com.acme",
        )
    )
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_logical_type_and_inline_primitive_object() -> None:
    result = run(
        rec(
            "Order",
            [
                field("at", {"type": "long", "logicalType": "timestamp-millis"}),
                field("id", {"type": "string"}),
            ],
            namespace="com.acme",
        )
    )
    assert result.external_refs == set()
    assert result.errors == []


def test_bad_default() -> None:
    result = run(rec("Order", [field("a", "string", default=5)], namespace="com.acme"))
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Avro schema: ")


def test_missing_reference_not_reported_here() -> None:
    result = run(rec("Order", [field("addr", "com.acme.Address")], namespace="com.acme"))
    assert result.errors == []
    assert result.external_refs == {"com.acme.Address"}


def test_with_reference() -> None:
    result = run(
        rec("Order", [field("addr", "com.acme.Address")], namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(ADDRESS)},
    )
    assert result.errors == []
    assert result.external_refs == {"com.acme.Address"}


def test_transitive_reference() -> None:
    c = rec("C", [field("v", "string")], namespace="a")
    b = rec("B", [field("c", "a.C")], namespace="a")
    result = run(
        rec("Order", [field("b", "a.B")], namespace="com.acme"),
        referenced={"a.C": json.dumps(c), "a.B": json.dumps(b)},
    )
    assert result.errors == []
    assert result.external_refs == {"a.B"}


def test_references_are_loaded() -> None:
    # Without its references fastavro stops at the unknown type (not reported) and
    # never reaches the bad default; with them it does.
    c = rec("C", [field("v", "string")], namespace="a")
    b = rec("B", [field("c", "a.C")], namespace="a")
    schema = rec("Order", [field("b", "a.B"), field("bad", "string", default=5)], namespace="com.acme")
    assert run(schema).errors == []
    result = run(schema, referenced={"a.C": json.dumps(c), "a.B": json.dumps(b)})
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Avro schema: ")


def test_broken_reference_text_is_ignored() -> None:
    result = run(
        rec("Order", [field("id", "string")], namespace="com.acme"),
        referenced={"com.acme.Broken": "{not json"},
    )
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_field_without_type() -> None:
    result = run(rec("Order", [{"name": "a"}], namespace="com.acme"))
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Avro schema: ")


def test_name_not_a_string() -> None:
    # fastavro raises TypeError here, not SchemaParseException.
    result = run({"type": "record", "name": 5, "namespace": "com.acme", "fields": []})
    assert result.expected_name is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Avro schema: ")


@pytest.mark.parametrize(
    "type_",
    [
        pytest.param({"type": ["null", "q.Z"]}, id="type-is-list"),
        pytest.param({"type": {"type": "array", "items": "q.Z"}}, id="type-is-dict"),
        pytest.param({"type": "array", "items": {"type": ["a"]}}, id="items-type-is-list"),
        pytest.param({"type": "map", "values": {"type": {"type": "string"}}}, id="values-type-is-dict"),
    ],
)
def test_type_object_with_non_string_type(type_: Any) -> None:
    result = run(rec("Order", [field("x", type_)], namespace="com.acme"))
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid Avro schema: ")


@pytest.mark.parametrize("value", [5, True, None, ["a"], {"a": 1}], ids=repr)
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda v: rec("Order", [field("x", {"type": v})], namespace="com.acme"), id="type"),
        pytest.param(lambda v: rec("Order", [field("x", rec(v, []))], namespace="com.acme"), id="nested-name"),
        pytest.param(lambda v: rec("Order", [field("x", rec("In", [], namespace=v))], namespace="com.acme"),
                     id="nested-namespace"),
        pytest.param(lambda v: rec("Order", [field("x", {"type": "array", "items": v})], namespace="com.acme"),
                     id="items"),
        pytest.param(lambda v: rec("Order", [field("x", {"type": "map", "values": v})], namespace="com.acme"),
                     id="values"),
        pytest.param(lambda v: rec("Order", [v], namespace="com.acme"), id="field-entry"),
        pytest.param(lambda v: rec("Order", v, namespace="com.acme"), id="fields"),
        pytest.param(lambda v: rec("Order", [field("x", {"type": "enum", "name": "E", "symbols": [v]})],
                                   namespace="com.acme"), id="symbol"),
        pytest.param(lambda v: {"type": "record", "name": v, "namespace": "com.acme", "fields": []}, id="name"),
        pytest.param(lambda v: rec("Order", [], namespace=v), id="namespace"),
    ],
)
def test_unexpected_json_types_do_not_raise(build: Any, value: Any) -> None:
    result = run(build(value))
    assert all(isinstance(e, str) for e in result.errors)


def test_fastavro_recursion_error_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    def overflow(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(avro.fastavro, "parse_schema", overflow)
    result = run(rec("Order", [field("id", "string")], namespace="com.acme"))
    assert result.errors == ["Invalid Avro schema: maximum recursion depth exceeded"]
