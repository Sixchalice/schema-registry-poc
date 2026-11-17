from __future__ import annotations

import json
from typing import Any

import pytest

from schema_validator import messages
from schema_validator.formats import avro
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
    assert result.errors == ["Invalid Avro schema: maximum recursion depth exceeded"]


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

