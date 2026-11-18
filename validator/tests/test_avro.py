from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from schema_validator import messages
from schema_validator.formats import avro
from schema_validator.formats._avro_defaults import default_errors
from schema_validator.formats.avro import analyse
from schema_validator.models import FormatAnalysis, ValidatorConfig

TOP_LEVEL = ["The top level of an Avro schema must be a single record, enum or fixed."]
NAMESPACE = ['Avro schema needs a namespace, for example "namespace": "com.team.domain".']


def run(d: Any, referenced: dict[str, str] | None = None) -> FormatAnalysis:
    return analyse(json.dumps(d), {} if referenced is None else referenced, ValidatorConfig())


def rec(name: str, fields: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {"type": "record", "name": name, "fields": fields, **kw}


def field(name: str, type_: Any, **kw: Any) -> dict[str, Any]:
    return {"name": name, "type": type_, **kw}


ADDRESS = rec("Address", [field("street", "string")], namespace="com.acme")
SCHEMAS = Path(__file__).parent / "schemas"


def test_valid() -> None:
    result = run(rec("Order", [field("id", "string")], namespace="com.acme"))
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_not_json() -> None:
    result = analyse("{not json", {}, ValidatorConfig())
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Schema is not valid JSON: ")
    assert result.external_refs is None


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


TOO_DEEP = ["Schema is nested more than 64 levels deep."]


def test_depth_at_limit_passes_the_depth_check() -> None:
    result = analyse(nested(64), {}, ValidatorConfig())
    assert messages.SCHEMA_TOO_DEEP.format(max=64) not in result.errors


def test_depth_above_limit_is_an_error() -> None:
    result = analyse(nested(65), {}, ValidatorConfig())
    assert result.errors == TOO_DEEP
    assert result.external_refs is None


def test_parser_recursion_is_too_deep() -> None:
    # json.loads raises RecursionError, not ValueError, at about 1000 levels.
    result = analyse(nested(5000), {}, ValidatorConfig())
    assert result.errors == TOO_DEEP
    assert result.external_refs is None


def test_configured_limit_is_used() -> None:
    result = analyse(nested(6), {}, ValidatorConfig(max_nesting_depth=5))
    assert result.errors == ["Schema is nested more than 5 levels deep."]


def test_top_level_union() -> None:
    result = run(["null", rec("Order", [field("id", "string")], namespace="com.acme")])
    assert result.errors == TOP_LEVEL
    assert result.external_refs is None


def test_top_level_enum() -> None:
    result = run({"type": "enum", "name": "Color", "namespace": "a.b", "symbols": ["RED"]})
    assert result.expected_name == "a.b.Color"
    assert result.errors == []
    assert result.external_refs == set()


def test_top_level_fixed() -> None:
    result = run({"type": "fixed", "name": "Id", "namespace": "a.b", "size": 16})
    assert result.expected_name == "a.b.Id"
    assert result.errors == []
    assert result.external_refs == set()


def test_top_level_enum_needs_namespace() -> None:
    result = run({"type": "enum", "name": "Color", "symbols": ["RED"]})
    assert result.errors == NAMESPACE


def test_record_using_a_referenced_enum() -> None:
    color = json.dumps({"type": "enum", "name": "Color", "namespace": "a.b", "symbols": ["RED"]})
    result = run(rec("Paint", [field("c", "a.b.Color")], namespace="a.b"), {"a.b.Color": color})
    assert result.errors == []
    assert result.external_refs == {"a.b.Color"}


def test_top_level_type_that_is_not_a_string() -> None:
    assert run({"type": ["record"], "name": "R", "fields": []}).errors == TOP_LEVEL
    assert run({"type": {"type": "record"}}).errors == TOP_LEVEL


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
    # fastavro stops at the forward use, and Schema Registry rejects it too.
    assert result.errors == ["Invalid Avro schema: type 'com.acme.Later' is used before it is defined"]


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


def nested_ns(name: str) -> str:
    return messages.AVRO_NESTED_NAMESPACE.format(name=name)


def test_unqualified_use_has_no_null_namespace_fallback() -> None:
    # fastavro (Python clients) has no null-namespace fallback, so neither has the walk:
    # the use resolves against the enclosing namespace only.
    result = run(
        rec(
            "Order",
            [field("inner", rec("Inner", [field("x", "string")], namespace="")), field("again", "Inner")],
            namespace="com.acme",
        )
    )
    # No external reference either: the namespace message is the whole story (no D4 on top).
    assert result == FormatAnalysis("com.acme.Order", set(), [nested_ns("Inner")])


def test_qualified_use_is_not_the_null_namespace_type() -> None:
    result = run(
        rec(
            "Order",
            [field("inner", rec("Inner", [field("x", "string")], namespace="")), field("again", "other.Inner")],
            namespace="com.acme",
        )
    )
    assert result.external_refs == {"other.Inner"}
    assert result.errors == [nested_ns("Inner")]


@pytest.mark.parametrize(
    "nested",
    [
        pytest.param(rec("Inner", [field("x", "string")], namespace=""), id="record"),
        pytest.param({"type": "enum", "name": "Inner", "namespace": "", "symbols": ["A"]}, id="enum"),
        pytest.param({"type": "fixed", "name": "Inner", "namespace": "", "size": 4}, id="fixed"),
    ],
)
def test_nested_type_with_empty_namespace(nested: dict[str, Any]) -> None:
    result = run(rec("Order", [field("inner", nested), field("id", "string")], namespace="com.acme"))
    assert result == FormatAnalysis("com.acme.Order", set(), [nested_ns("Inner")])


@pytest.mark.parametrize("name", [".Inner", "Inner.", "a..B"])
def test_nested_type_with_empty_name_part(name: str) -> None:
    # The fault is in the name, so the message is about the name, not about "namespace".
    result = run(rec("Order", [field("inner", {"type": "fixed", "name": name, "size": 4})], namespace="com.acme"))
    assert result.expected_name == "com.acme.Order"
    assert result.errors[0] == (
        f"Type name '{name}' has an empty part around a dot. "
        'Use a plain name, for example "Address", or a full name, for example "com.team.domain.Address".'
    )
    assert result.errors[0] == messages.AVRO_NESTED_NAME.format(name=name)


def test_nested_namespace_message_text() -> None:
    assert nested_ns("Inner") == (
        "Type 'Inner' needs a namespace. Remove its \"namespace\" entry so it uses the schema's namespace."
    )


@pytest.mark.parametrize("namespace", [None, 5, ["a"], {}])
@pytest.mark.parametrize("used", [True, False])
def test_nested_type_with_a_namespace_that_is_not_a_string(namespace: Any, used: bool) -> None:
    # Java Avro inherits the enclosing namespace here and fastavro uses none, so the two
    # clients would disagree on the type's full name. One message, also when a bare use
    # follows (fastavro stops there, which the namespace message already explains).
    fields = [field("inner", {"type": "fixed", "name": "Inner", "namespace": namespace, "size": 4})]
    if used:
        fields.append(field("again", "Inner"))
    result = run(rec("Order", fields, namespace="com.acme"))
    assert result == FormatAnalysis("com.acme.Order", set(), [nested_ns("Inner")])


def test_wrapped_use_of_a_local_type_says_how_to_write_it() -> None:
    # The registry accepts {"type": "<name>"}; fastavro (Python clients) does not.
    result = run(
        rec("Order", [field("inner", {"type": "fixed", "name": "Inner", "size": 4}),
                      field("again", {"type": "com.acme.Inner"})], namespace="com.acme")
    )
    assert result.errors == [
        "Invalid Avro schema: write the type 'com.acme.Inner' as a plain string, not as {\"type\": \"com.acme.Inner\"}"
    ]


def test_nested_types_in_one_union_reported_in_document_order() -> None:
    def fixed(name: str) -> dict[str, Any]:
        return {"type": "fixed", "name": name, "namespace": "", "size": 4}

    result = run(rec("Order", [field("u", [fixed("First"), "null", fixed("Second"), fixed("Third")])],
                     namespace="com.acme"))
    assert result.errors == [nested_ns(n) for n in ("First", "Second", "Third")]


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="inherits"),
        pytest.param({"namespace": "other.ns"}, id="own-namespace"),
    ],
)
def test_nested_type_with_a_namespace_is_fine(extra: dict[str, Any]) -> None:
    result = run(rec("Order", [field("inner", rec("Inner", [field("x", "string")], **extra))], namespace="com.acme"))
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_nested_dotted_name_is_fine() -> None:
    result = run(rec("Order", [field("inner", rec("other.ns.Inner", [field("x", "string")], namespace=""))],
                     namespace="com.acme"))
    assert result == FormatAnalysis("com.acme.Order", set(), [])


def test_nested_types_without_namespace_reported_in_document_order() -> None:
    def fixed(name: str) -> dict[str, Any]:
        return {"type": "fixed", "name": name, "namespace": "", "size": 4}

    result = run(
        rec(
            "Order",
            [
                field("a", {"type": "array", "items": fixed("InArray")}),
                field("b", rec("Mid", [field("deep", fixed("Deep"))])),
                field("c", {"type": "map", "values": fixed("InMap")}),
                field("d", ["null", fixed("InUnion")]),
            ],
            namespace="com.acme",
        )
    )
    assert result == FormatAnalysis(
        "com.acme.Order", set(), [nested_ns(n) for n in ("InArray", "Deep", "InMap", "InUnion")]
    )


def test_no_namespace_at_the_top_is_reported_once() -> None:
    # Nested types inherit the missing namespace; only the top-level message is shown.
    result = run(rec("Order", [field("inner", rec("Inner", [field("x", "string")]))]))
    assert result.errors == NAMESPACE


def test_nested_namespace_rule_skips_referenced_texts() -> None:
    address = rec("Address", [field("c", {"type": "enum", "name": "Country", "namespace": "", "symbols": ["NL"]})],
                  namespace="com.acme")
    result = run(
        rec("Customer", [field("addr", "com.acme.Address")], namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(address)},
    )
    assert result == FormatAnalysis("com.acme.Customer", {"com.acme.Address"}, [])


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


CASES = json.loads((SCHEMAS / "avro_defaults.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["label"])
def test_default_cases(case: dict) -> None:
    result = analyse(json.dumps(case["schema"]), {}, ValidatorConfig())
    assert (result.errors == []) == (case["validator"] == "ok")


def test_union_default_message() -> None:
    schema = {"type": "record", "name": "R", "namespace": "t",
              "fields": [{"name": "u", "type": ["null", "string"], "default": "x"}]}
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_UNION_DEFAULT.format(field="u", record="t.R", first="null")]


def test_invalid_default_message_names_the_type() -> None:
    schema = {"type": "record", "name": "R", "namespace": "t", "fields": [
        {"name": "g", "type": {"type": "record", "name": "G", "fields": [{"name": "x", "type": "int"}]},
         "default": {}}]}
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_DEFAULT_INVALID.format(field="g", record="t.R", type="t.G")]


def test_default_in_a_nested_record_is_checked() -> None:
    # Reported against the nested record's own full name.
    inner = {"type": "record", "name": "Inner", "fields": [{"name": "n", "type": ["null", "int"], "default": 1}]}
    schema = {"type": "record", "name": "R", "namespace": "t", "fields": [{"name": "i", "type": inner}]}
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_UNION_DEFAULT.format(field="n", record="t.Inner", first="null")]


def _with_error_type(error_fields: list[dict[str, Any]], **field: Any) -> dict[str, Any]:
    error = {"type": "error", "name": "Failure", "fields": error_fields}
    return {"type": "record", "name": "R", "namespace": "t", "fields": [{"name": "e", "type": error, **field}]}


def test_default_inside_a_nested_error_type_is_checked() -> None:
    # Java parses an error type as a record and checks its fields' defaults.
    schema = _with_error_type([{"name": "u", "type": ["null", "int"], "default": 1}])
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_UNION_DEFAULT.format(field="u", record="t.Failure", first="null")]


def test_record_shaped_default_of_an_error_type_is_valid() -> None:
    schema = _with_error_type([{"name": "u", "type": "int"}], default={"u": 1})
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == []


def test_bad_default_of_an_error_type_names_the_type() -> None:
    schema = _with_error_type([{"name": "u", "type": "int"}], default={"u": "x"})
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_DEFAULT_INVALID.format(field="e", record="t.R", type="t.Failure")]


def test_default_of_a_referenced_type() -> None:
    address = '{"type":"record","name":"Address","namespace":"t","fields":[{"name":"zip","type":"string"}]}'
    schema = {"type": "record", "name": "R", "namespace": "t",
              "fields": [{"name": "a", "type": "t.Address", "default": {"street": "x"}}]}
    assert analyse(json.dumps(schema), {"t.Address": address}, ValidatorConfig()).errors == [
        messages.AVRO_DEFAULT_INVALID.format(field="a", record="t.R", type="t.Address")]


def test_two_bad_defaults_give_two_messages_in_order() -> None:
    bad = {"type": ["null", "string"], "default": "x"}
    schema = {"type": "record", "name": "R", "namespace": "t",
              "fields": [{"name": "b", **bad}, {"name": "a", **bad}]}
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_UNION_DEFAULT.format(field=f, record="t.R", first="null") for f in ("b", "a")]


@pytest.mark.parametrize(
    "type_, default, described",
    [
        ("int", True, "int"),  # fastavro takes a bool for an int
        ({"type": "array", "items": "int"}, ["a"], "array"),
        ({"type": "map", "values": "int"}, {"k": "a"}, "map"),
        ({"type": "bytes", "logicalType": "decimal", "precision": 4, "scale": 2}, "\u20ac", "bytes"),
        ({"type": "enum", "name": "E", "symbols": ["A"]}, "Z", "t.E"),
        ({"type": "fixed", "name": "F", "namespace": "o", "size": 2}, "abc", "o.F"),
    ],
)
def test_invalid_default_message_describes_the_type(type_: Any, default: Any, described: str) -> None:
    schema = {"type": "record", "name": "R", "namespace": "t", "fields": [{"name": "f", "type": type_, "default": default}]}
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_DEFAULT_INVALID.format(field="f", record="t.R", type=described)]


def test_second_use_of_a_named_type_is_checked_by_its_definition() -> None:
    s = {"type": "record", "name": "S", "fields": [{"name": "x", "type": "int"}]}
    schema = {"type": "record", "name": "R", "namespace": "t",
              "fields": [{"name": "a", "type": s}, {"name": "b", "type": "S", "default": {"x": "s"}}]}
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == [
        messages.AVRO_DEFAULT_INVALID.format(field="b", record="t.R", type="t.S")]


def test_defaults_inside_a_referenced_text_are_not_walked() -> None:
    # The reference's own bad default is its own Schema's error, not this one's.
    address = {"type": "record", "name": "Address", "namespace": "t",
               "fields": [{"name": "zip", "type": ["null", "string"], "default": "x"}]}
    schema = {"type": "record", "name": "R", "namespace": "t", "fields": [{"name": "a", "type": "t.Address"}]}
    assert analyse(json.dumps(schema), {"t.Address": json.dumps(address)}, ValidatorConfig()).errors == []


def test_defaults_are_not_checked_when_fastavro_stopped_early() -> None:
    # A missing reference: fastavro stops at it, D4 reports it, and nothing is parsed to walk.
    schema = {"type": "record", "name": "R", "namespace": "t",
              "fields": [{"name": "a", "type": "t.Address"}, {"name": "u", "type": ["null", "string"], "default": "x"}]}
    assert analyse(json.dumps(schema), {}, ValidatorConfig()).errors == []


def _record(type_: Any, **default: Any) -> dict[str, Any]:
    return {"type": "record", "name": "t.R", "fields": [{"name": "f", "type": type_, **default}]}


def _invalid(type_: str) -> list[str]:
    return [messages.AVRO_DEFAULT_INVALID.format(field="f", record="t.R", type=type_)]


@pytest.mark.parametrize(
    "parsed, named, expected",
    [
        pytest.param(_record("t.X", default=1), {"t.X": {"type": "t.X"}}, _invalid("t.X"), id="name-cycle"),
        pytest.param(_record("t.Missing", default={}), {}, _invalid("t.Missing"), id="unknown-name"),
        pytest.param(_record(5, default=1), {}, _invalid("unknown"), id="type-not-a-schema"),
        pytest.param({"type": "record", "name": "t.R", "fields": {"f": 1}}, {}, [], id="fields-not-a-list"),
        pytest.param(_record({"type": "record", "name": "t.S", "fields": None}, default={}), {}, _invalid("t.S"),
                     id="value-record-fields-not-a-list"),
        pytest.param(_record({"type": "record", "name": "t.S", "fields": [5]}, default={}), {}, _invalid("t.S"),
                     id="value-record-field-not-a-dict"),
        pytest.param(_record({"type": "enum", "name": "t.E"}, default="A"), {}, _invalid("t.E"), id="enum-without-symbols"),
        pytest.param(
            _record({"type": {"type": "record", "name": "t.S", "fields": [{"name": "x", "type": "int", "default": "s"}]}}),
            {}, [messages.AVRO_DEFAULT_INVALID.format(field="x", record="t.S", type="int")], id="wrapped-record-is-walked"),
    ],
)
def test_default_check_on_shapes_fastavro_does_not_produce(parsed: Any, named: Any, expected: list[str]) -> None:
    # The check must not raise on any shape, and must not pass a default it cannot check.
    assert default_errors(parsed, named) == expected


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


COUNTRY = {"type": "enum", "name": "Country", "symbols": ["NL", "US"]}
ADDRESS_WITH_COUNTRY = rec("Address", [field("street", "string"), field("country", COUNTRY)], namespace="com.acme")


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param([field("addr", "com.acme.Address"), field("country", "com.acme.Country")], id="both"),
        pytest.param([field("country", "com.acme.Country")], id="nested-only"),
        pytest.param([field("country", "Country")], id="nested-unqualified"),
    ],
)
def test_name_nested_in_a_reference_cannot_be_used(fields: list[dict[str, Any]]) -> None:
    # The registry resolves it; the Python and Java serializers cannot produce with it.
    # The row still counts as used, so this is the only message.
    result = run(
        rec("Customer", fields, namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(ADDRESS_WITH_COUNTRY)},
    )
    assert result == FormatAnalysis("com.acme.Customer", {"com.acme.Address"}, [
        "Type 'com.acme.Country' is defined inside the Schema 'com.acme.Address'. A type nested in another "
        "Schema cannot be used here. Move it to its own Schema and reference that."
    ])
    assert result.errors == [
        messages.AVRO_NESTED_IN_REFERENCE.format(name="com.acme.Country", schema="com.acme.Address")]


def test_nested_name_used_twice_is_reported_once() -> None:
    result = run(
        rec("Customer", [field("a", "com.acme.Country"), field("b", ["null", "Country"])], namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(ADDRESS_WITH_COUNTRY)},
    )
    assert len(result.errors) == 1


def test_nested_names_are_reported_in_name_order() -> None:
    address = rec("Address", [field("z", {"type": "fixed", "name": "Zip", "size": 4}), field("country", COUNTRY)],
                  namespace="com.acme")
    result = run(
        rec("Customer", [field("z", "com.acme.Zip"), field("c", "com.acme.Country")], namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(address)},
    )
    assert result.errors == [
        messages.AVRO_NESTED_IN_REFERENCE.format(name=n, schema="com.acme.Address")
        for n in ("com.acme.Country", "com.acme.Zip")]


def test_nested_name_maps_to_the_reference_key() -> None:
    # The key is the row's ref_string, whatever the referenced text calls itself.
    result = run(
        rec("Customer", [field("country", "com.acme.Country")], namespace="com.acme"),
        referenced={"addr": json.dumps(ADDRESS_WITH_COUNTRY)},
    )
    assert result.external_refs == {"addr"}
    # The message names the referenced schema by its own name, not by the row's ref_string.
    assert result.errors == [
        messages.AVRO_NESTED_IN_REFERENCE.format(name="com.acme.Country", schema="com.acme.Address")]


def test_nested_name_without_its_reference_is_external() -> None:
    result = run(rec("Customer", [field("country", "com.acme.Country")], namespace="com.acme"))
    assert result.external_refs == {"com.acme.Country"}


def test_top_level_name_of_a_reference_is_not_mapped() -> None:
    # Only nested names map to the key; the referenced record itself still needs its own name.
    result = run(
        rec("Customer", [field("addr", "com.acme.Address")], namespace="com.acme"),
        referenced={"addr": json.dumps(ADDRESS_WITH_COUNTRY)},
    )
    assert result.external_refs == {"com.acme.Address"}


def test_local_definition_wins_over_a_reference() -> None:
    local_country = {"type": "enum", "name": "Country", "symbols": ["X"]}
    result = run(
        rec("Customer", [field("c", local_country), field("again", "com.acme.Country")], namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(ADDRESS_WITH_COUNTRY)},
    )
    assert result.external_refs == set()
    assert result.errors == []


@pytest.mark.parametrize("text", [
    json.dumps({"type": "record", "name": ["x"], "fields": []}),
    json.dumps({"type": "record", "name": {"a": 1}, "namespace": [1], "fields": []}),
    json.dumps([1, 2]),
    json.dumps("string"),
    "null",
])
def test_odd_reference_texts_do_not_raise(text: str) -> None:
    result = run(rec("Order", [field("x", "com.acme.X")], namespace="com.acme"), referenced={"k": text})
    assert result.external_refs == {"com.acme.X"}


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
    assert result.errors == [messages.AVRO_INVALID.format(err=messages.AVRO_WRONG_KIND)]


BAD_DEFAULT = field("bad", "string", default=5)


def test_null_namespace_type_nested_in_a_reference_is_not_provided_by_bare_name() -> None:
    # Country sits in the null namespace inside Address; "Country" in com.acme is com.acme.Country.
    address = rec("Address", [field("c", {"type": "enum", "name": "Country", "namespace": "", "symbols": ["NL"]})],
                  namespace="com.acme")
    result = run(
        rec("Customer", [field("addr", "com.acme.Address"), field("country", "Country"), BAD_DEFAULT],
            namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(address)},
    )
    assert result.external_refs == {"com.acme.Address", "com.acme.Country"}


def test_unknown_type_the_walk_resolved_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    # The walk and fastavro disagree: fastavro stopped early, so the schema is not checked.
    def unknown(*args: Any, **kwargs: Any) -> Any:
        raise avro.UnknownType("com.acme.Inner")

    monkeypatch.setattr(avro.fastavro, "parse_schema", unknown)
    result = run(
        rec("Customer", [field("addr", "com.acme.Address")], namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(ADDRESS)},
    )
    assert result.external_refs == {"com.acme.Address"}
    assert result.errors == ["Invalid Avro schema: unknown type 'com.acme.Inner'"]


def test_wrapped_use_of_a_referenced_name_names_the_type() -> None:
    # fastavro does not resolve {"type": "<name>"}; its UnknownType carries the wrapper.
    result = run(
        rec("Customer", [field("addr", {"type": "com.acme.Address"})], namespace="com.acme"),
        referenced={"com.acme.Address": json.dumps(ADDRESS)},
    )
    assert result.errors == [
        "Invalid Avro schema: write the type 'com.acme.Address' as a plain string, "
        "not as {\"type\": \"com.acme.Address\"}"
    ]


@pytest.mark.parametrize("name", [[["Customer"]], ["x"], {"type": "array", "items": "Customer"}])
@pytest.mark.parametrize("with_reference", [True, False])
def test_name_that_fastavro_lets_through_is_invalid(name: Any, with_reference: bool) -> None:
    # fastavro accepts a list or dict as the record name, or stops earlier at the missing
    # reference; either way no name can be derived, and that must be an error of its own.
    result = run(
        {"type": "record", "name": name, "namespace": "com.acme", "fields": [field("a", "com.acme.Address")]},
        referenced={"com.acme.Address": json.dumps(ADDRESS)} if with_reference else {},
    )
    assert result == FormatAnalysis(None, {"com.acme.Address"}, ['Invalid Avro schema: the record "name" must be a string'])


@pytest.mark.parametrize("type_, entry", [({"type": "array"}, "items"), ({"type": "map"}, "values")])
def test_array_without_items_and_map_without_values(type_: dict[str, Any], entry: str) -> None:
    # Nothing to walk into; fastavro reports the missing key.
    result = run(rec("Order", [field("x", type_)], namespace="com.acme"))
    assert result.expected_name == "com.acme.Order"
    assert result.external_refs == set()
    assert result.errors == [f"Invalid Avro schema: a type is missing its '{entry}' entry"]



def test_enum_and_fixed_warn() -> None:
    enum = run({"type": "enum", "name": "Color", "namespace": "t", "symbols": ["RED"]})
    assert enum.errors == []
    assert enum.warnings == [messages.AVRO_NOT_A_MESSAGE.format(kind="enum")]
    fixed = run({"type": "fixed", "name": "Md5", "namespace": "t", "size": 2})
    assert fixed.errors == []
    assert fixed.warnings == [messages.AVRO_NOT_A_MESSAGE.format(kind="fixed")]


def test_record_has_no_kind_warning() -> None:
    result = run(rec("Order", [field("id", "string")], namespace="t"))
    assert result.errors == []
    assert result.warnings == []


def test_no_kind_warning_with_errors() -> None:
    result = run({"type": "enum", "name": "Color", "symbols": ["RED"]})
    assert result.errors == NAMESPACE
    assert result.warnings == []


def test_type_object_as_type_is_wrong_kind() -> None:
    result = run(rec("t.R", [field("a", {"type": {"type": "Nope"}})]))
    assert result.errors == [messages.AVRO_INVALID.format(err=messages.AVRO_WRONG_KIND)]


def test_long_default_is_shortened() -> None:
    # fastavro rejects the default first, and its text carries the whole default.
    (error,) = run(rec("t.R", [field("a", "int", default="x" * 400000)])).errors
    assert error.startswith("Invalid Avro schema: ")
    assert len(error) <= len("Invalid Avro schema: ") + 1000
    assert error.endswith("…")


def test_long_parse_error_is_shortened(monkeypatch: pytest.MonkeyPatch) -> None:
    def parse(*args: Any, **kwargs: Any) -> Any:
        raise avro.SchemaParseException("x" * 400000)

    monkeypatch.setattr(avro.fastavro, "parse_schema", parse)
    (error,) = run(rec("t.R", [field("a", "int")])).errors
    assert error.startswith("Invalid Avro schema: ")
    assert len(error) <= len("Invalid Avro schema: ") + 1000
    assert error.endswith("…")


def _error_type(**kw: Any) -> dict[str, Any]:
    return {"type": "error", "name": "Oops", "fields": [field("why", "string")], **kw}


def test_nested_error_type_passes() -> None:
    result = run(rec("Reply", [field("e", _error_type())], namespace="t"))
    assert result.errors == []
    assert result.expected_name == "t.Reply"
    assert result.external_refs == set()


def test_nested_error_name_can_be_used_again() -> None:
    result = run(rec("Reply", [field("e", _error_type()), field("again", "Oops")], namespace="t"))
    assert result.errors == []
    assert result.external_refs == set()


def test_nested_error_fields_are_walked() -> None:
    def twin(kind: str) -> FormatAnalysis:
        nested = {"type": kind, "name": "Oops", "fields": [field("why", "t.Missing")]}
        return run(rec("Reply", [field("e", nested)], namespace="t"))

    result = twin("error")
    assert result.external_refs == {"t.Missing"}
    assert result == twin("record")


def test_nested_error_namespace_rules_apply() -> None:
    result = run(rec("Reply", [field("e", _error_type(name="a..b"))], namespace="t"))
    assert messages.AVRO_NESTED_NAME.format(name="a..b") in result.errors
    twin = run(rec("Reply", [field("e", rec("a..b", [field("why", "string")]))], namespace="t"))
    assert result.errors[0] == twin.errors[0]


def test_top_level_error_unchanged() -> None:
    result = run({"type": "error", "name": "Oops", "namespace": "t", "fields": []})
    assert result == FormatAnalysis(None, None, [messages.AVRO_TOP_LEVEL])


def _chars(name: str) -> str:
    return messages.AVRO_NAME_CHARS.format(name=name)


def test_namespace_with_a_hyphen_is_an_error() -> None:
    assert run(rec("R", [], namespace="a-b")).errors == [_chars("a-b.R")]


def test_namespace_part_starting_with_a_digit_is_an_error() -> None:
    assert run(rec("R", [], namespace="x.9x")).errors == [_chars("x.9x.R")]


def test_non_ascii_name_is_an_error() -> None:
    assert run(rec("Ordér", [], namespace="t")).errors == [_chars("t.Ordér")]


@pytest.mark.parametrize("kind", ["record", "error"])
def test_nested_bad_name_is_an_error(kind: str) -> None:
    nested_type = {"type": kind, "name": "Bad-Name", "fields": []}
    result = run(rec("R", [field("n", nested_type)], namespace="t"))
    assert result.errors == [_chars("t.Bad-Name")]


def test_valid_names_with_digits_and_underscores_pass() -> None:
    assert run(rec("Z_2", [], namespace="_x.y1")).errors == []
    nested_type = rec("Line_1", [])
    assert run(rec("Order_V2", [field("l", nested_type)], namespace="com.acme_2.v1")).errors == []


def test_empty_part_keeps_its_own_message() -> None:
    assert run(rec("Order", [], namespace="com.")).errors == [messages.AVRO_NAMESPACE]
    assert run(rec("com..Order", [])).errors == [messages.AVRO_NAMESPACE]


def test_bad_names_are_reported_in_document_order_one_per_type() -> None:
    inner = rec("B-b", [])
    outer = rec("Mid", [field("i", inner)], namespace="t")
    result = run(rec("A-a", [field("m", outer)], namespace="t"))
    assert result.errors == [_chars("t.A-a"), _chars("t.B-b")]


def test_bad_chars_in_a_type_with_another_message_get_no_second_one() -> None:
    nested_type = rec("a-b.", [])
    result = run(rec("R", [field("n", nested_type)], namespace="t"))
    assert result.errors == [messages.AVRO_NESTED_NAME.format(name="a-b.")]


def test_bad_chars_in_a_referenced_text_are_not_checked() -> None:
    ref = rec("Bad-Name", [], namespace="t")
    result = run(rec("R", [field("n", "t.Bad-Name")], namespace="t"), {"r": json.dumps(ref)})
    assert _chars("t.Bad-Name") not in result.errors


def test_nested_dotted_bad_name_is_an_error() -> None:
    nested_type = rec("x.9y.Inner", [])
    result = run(rec("R", [field("n", nested_type)], namespace="t"))
    assert result.errors == [_chars("x.9y.Inner")]
