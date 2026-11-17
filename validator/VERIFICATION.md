# Verification record

Checked on 2026-11-16 (the secured registry and the Avro namespace follow-up on 2026-11-17) against `confluentinc/cp-schema-registry:7.9.0` on `apache/kafka:3.8.1`, from `integration/docker-compose.yml`, with confluent-kafka-python 2.16.1 and the Confluent 7.9.0 Java serializers.

```
.venv/bin/python -m pytest -q                                   # 721 passed
.venv/bin/python -m pytest integration/tests -m integration -q  # 142 passed, none skipped
```

Every integration test creates its subjects under a fresh namespace (`it` + 10 hex digits, shown below as `<ns>`), so the suite can be rerun against the same registry.

## Spec §10a items

| # | What was tested | Observed | Pinned by | Code change |
|---|---|---|---|---|
| 1 | `POST /compatibility/subjects/{s}/versions?verbose=true` on a subject that does not exist, all three formats; the lookups the validator relies on | Valid schema: `200 {"is_compatible": true, "messages": []}`. Invalid schema: `200`, `is_compatible: false`, one message starting `Invalid schema ` with a `details: ` part. Reasons: Avro `"strin" is not a defined name. The type of the "id" field must be a defined name or a {"type": ...} expression.`; JSON `Invalid JSON Schema`; Protobuf `Could not parse Protobuf - Syntax error in :3:31: expected ';' but was '}'`. Never 404 or 422. `GET /subjects/<ns>.none/versions/latest`: `404 {"error_code":40401,"message":"Subject '<ns>.none' not found."}`. `GET /subjects/<existing>/versions/99`: `404 {"error_code":40402,"message":"Version 99 not found."}`. So the client's "None only for a 404 with 40401 or 40402" rule matches the registry. A path the registry does not serve (a wrong prefix in `sr_url`, e.g. `GET /no-such-prefix/subjects/<ns>.Order/versions/latest` or `.../versions/1`) also gives 404, but with `{"error_code":404,"message":"HTTP 404 Not Found"}`; the client raises `SchemaRegistryUnavailable` for it on `get_latest` and `get_version`, and `validate` of a create through that client gives `Could not reach Schema Registry to validate. Try again.` `GET /config/<ns>.none?defaultToGlobal=true`: `200 {"compatibilityLevel":"BACKWARD"}` | `test_sr_behaviour.py`: `test_new_subject_valid`, `test_new_subject_invalid`, `test_get_latest_missing`, `test_get_version_missing`, `test_404_from_a_wrong_base_url_is_not_not_found`, `test_level_endpoint_for_missing_subject` | `sr_client.py`: a 404 without error_code 40401/40402 is now `SchemaRegistryUnavailable` on every call (see below) |
| 2 | Real producers look up the subject `validate` accepts. Python: `validate` first, then a serializer with `record_subject_name_strategy`, `auto.register.schemas=False`, `use.latest.version=True`; before registration it fails, after registration it serializes and the bytes are produced to Kafka (delivery acknowledged). Cases: Avro simple; Avro with `name` `<ns>.real.Dotted` and `namespace` `ignored.ns`; JSON simple; Protobuf simple; Protobuf importing `google/protobuf/timestamp.proto` | Before registration every serializer raises `SchemaRegistryError: Subject '<expected name>' not found. (HTTP status code 404, SR code 40401)`; the dotted Avro name looks up `<ns>.real.Dotted`. After registration the same serializer instance returns bytes. Java: see below | `test_python_clients.py`: `test_avro_simple`, `test_avro_dotted_name_wins_over_namespace`, `test_json_simple`, `test_protobuf_simple`, `test_protobuf_builtin_import`; Java `probe.Main` | None |
| 3 | References end to end. Python: register the referenced schema, `validate` the referencing one (a `Warning` naming `<ns>.Address v1`), check the serializer fails naming the referencing subject, register it with `{name: ref_string, subject, version: 1}`, serialize and produce. Avro passes `Schema(text, "AVRO", [SchemaReference(...)])`; JSON uses `$ref` both with and without `#/definitions/Zip` and a row `address.json`; Protobuf imports `<ns>/Address.proto` (the derived path) | All three register with 200 and serialize. The JSON payload is validated through the reference by the client. Java `probe.ProtoRefs`: see below | `test_python_clients.py`: `test_avro_with_reference`, `test_json_with_external_ref`, `test_protobuf_import_of_another_schema`; `test_validate_real_sr.py::test_references_end_to_end`; Java `probe.ProtoRefs` | None |
| 4 | The registry does not enforce the validator's own rules | `200` for: an unused reference row (all three formats); a Protobuf reference named `acme/other.proto` for `import "acme/address.proto"`; a Protobuf cycle (`Address` v2 importing `Customer`, which imports `Address`); a JSON schema with `title` `<ns>.My Order` under the subject `<ns>.My Order` | `test_sr_behaviour.py`: `test_sr_accepts_unused_reference_row`, `test_sr_accepts_protobuf_reference_name_mismatch`, `test_sr_accepts_protobuf_cycle`, `test_sr_accepts_json_title_with_space` | None |
| 5 | Built-in Protobuf imports: each of the 29 paths in `DEFAULT_BUILTIN_PROTO_IMPORTS`, imported with one of its types used and no reference row | All 29 register with `200`, and each also compiles with the validator's own Protobuf analyser (bundled `_proto`) with no error and no external reference. `google/type/localized_text.proto` with `google.type.LocalizedText` and `google/rpc/status.proto` with `google.rpc.Status`: `422`, `error_code` 42201, `details: Could not resolve type: google.type.LocalizedText` / `google.rpc.Status` | `test_sr_behaviour.py`: `test_builtin_imports` (29 cases), `test_non_builtin_imports_rejected` | None |
| 6 | The full `validate` pipeline with `HttpSchemaRegistryClient.from_config` against the registry | Create, all formats: `Ok`, then the same schema registers under `request.name` with 200. Stage A, empty schema: `Schema is empty.` Stage B, broken text: `Schema is not valid JSON: ...` (Avro, JSON) and `Invalid Protobuf schema: ...`, with no registry call made. Stage C: `The name must match the schema. It should be '<ns>.Order'.` Stage D, reference to an unregistered subject: exactly `Referenced schema '<ns>.Address' is not registered in Schema Registry yet.` Stage E1, Avro with field `a` twice (fastavro accepts it): `Schema Registry rejected the schema: Duplicate field a in record <ns>.Order: a type:INT pos:1 and a type:INT pos:0.` Stage E2, all formats: one line starting `Not BACKWARD-compatible with version 1: `, without the old schema text. References, all formats: `Warning` `References will be pinned to: <ns>.Address v1.`, then registering with that row gives 200. Avro, an enum `<ns>.Country` defined inside the registered `<ns>.Address`, with one row for `<ns>.Address`: a Customer using `<ns>.Address` and `<ns>.Country`, and one using only `<ns>.Country`, each give that `Warning`, then register with the reference `{name: <ns>.Address, subject: <ns>.Address, version: 1}` with 200. Edit with the subject level set to `FULL` by `PUT /config/{subject}` and `compatibility="BACKWARD"`: `Warning` `Compatibility was checked using the current level 'FULL', not the new level 'BACKWARD'.` Registry at `http://localhost:1`, 1 s timeout: `Could not reach Schema Registry to validate. Try again.` | `test_validate_real_sr.py` (29 tests) | None |
| 7 | The incompatibility and invalid-schema messages still parse with `sr_messages`: register v1, check an incompatible v2 | Each format gives one description, `old_version == 1`, `level == "BACKWARD"`, no `oldSchema` text. Raw first messages: Avro `{errorType:'READER_FIELD_MISSING_DEFAULT_VALUE', description:'The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema', additionalInfo:'zip'}`; JSON `{errorType:"TYPE_CHANGED", description:"A type at path '#/properties/id' is different between the new schema and the old schema'}` (opens with `"`, closes with `'`; the parser strips either); Protobuf `{errorType:"FIELD_SCALAR_KIND_CHANGED", description:"The kind of a SCALAR field at path '#/Order/1' in the new schema does not match its kind in the old schema"}` | `test_sr_behaviour.py::test_incompatible_messages_parse`, `test_new_subject_invalid` | None |

## Code changed because of an observation

One change, in `schema_validator/sr_client.py` (`HttpSchemaRegistryClient._call`), pinned by `tests/test_sr_client.py::test_other_404_on_level_and_compatibility_is_unavailable` and `integration/tests/test_sr_behaviour.py::test_404_from_a_wrong_base_url_is_not_not_found`.

- Observed: with a wrong prefix in `sr_url`, `POST /compatibility/...` returns `404 {"error_code":404,"message":"HTTP 404 Not Found"}`.
- Before: only the two version lookups treated a 404 without error_code 40401/40402 as unavailable. On the compatibility and level calls such a 404 was `SchemaRegistryRejected`, so `validate` of a create returned `Schema Registry rejected the schema: HTTP 404 Not Found` (E1), blaming the schema for a configuration fault.
- After: a 404 without error_code 40401/40402 is `SchemaRegistryUnavailable` on every call (except the schema lookup `POST /subjects/{s}`, added later for audit F4, where 40401 and 40403 are the not-found codes), so the result is E3 (`Could not reach Schema Registry to validate. Try again.`). A 404 with 40401/40402 is unchanged: `None` from the two version lookups, `SchemaRegistryRejected` on the level and compatibility calls. This departed from spec §7's "any other 4xx with an `error_code`: E1"; spec §7 now states the rule. It covers every 404 whose `error_code` is not 40401 or 40402: `error_code` 404, the HTTP server's generic code, and also Schema Registry's other 404 codes (40403 to 40409, for example `Schema not found`), which do not mean that the looked-up subject or version is not registered.

## Java checks

`integration/java`, run in `maven:3.9.9-eclipse-temurin-17` on the compose network (command in the README).

`probe.Main` (the `after registering` lines):

| Case | Result |
|---|---|
| AVRO simple (`p5java.Order`) | OK (7 bytes) |
| AVRO dotted name + conflicting namespace (`p5java.real.Dotted`) | OK (7 bytes) |
| AVRO with reference, Address inlined in the client's schema (`p5java.Customer`) | OK (9 bytes) |
| JSON plain POJO, no annotations | Looks up `Plain Order` (not registered; shows the pitfall in spec §12) |
| JSON POJO with `@JsonSchemaTitle` (`p5java.TitledOrder`) | Fails: `Incompatible schema` (the generated schema is compared with the registered one; see case [E] below) |
| JSON POJO with Confluent `@Schema(value=...)` (`p5java.AnnotatedOrder`) | OK (15 bytes) |
| JSON `JsonNode` + schema envelope (`p5java.EnvOrder`) | OK (15 bytes) |
| JSON with external `$ref` (`p5java.JCustomer`) | OK (28 bytes); an invalid payload is rejected by the client through the reference |
| PROTOBUF simple (`p5java.POrder`) | OK (9 bytes) |
| PROTOBUF with `java_package` / `java_outer_classname` | OK (9 bytes), same subject `p5java.POrder` |
| PROTOBUF importing `google/protobuf/timestamp.proto` (`p5java.PStamped`) | OK (6 bytes) |
| PROTOBUF importing another Schema at `p5java/address.proto` (non-conforming path on purpose) | Fails: `Subject 'p5java/address.proto' not found.` |
| PROTOBUF registered with a reference name different from the import path | Fails: `Subject 'p5java/address.proto' not found.` |
| PROTOBUF file with two top-level messages | Looks up `p5java.Multi1` or `p5java.Multi2` depending on the message sent |

`probe.ProtoRefs`:

| Case | Result |
|---|---|
| [A] `import "p6/shop/PAddress.proto"`, `QualifiedReferenceSubjectNameStrategy` | OK (13 bytes), produced and deserialized |
| [A] same, default strategy | Fails: `Subject 'p6/shop/PAddress.proto' not found.` |
| [B] `import "p6.shop.PAddress"` (import path = subject name), default strategy | OK (13 bytes) |
| [C] registered with a reference name different from the import path | Serializes, then fails to deserialize: `"PAddress" is not defined.` |
| [D] transitive import (QCustomerDeep, QMid, PAddress), qualified strategy | OK (6 bytes) |
| [E] JSON POJO with `@JsonSchemaTitle` and `latest.compatibility.strict=false` | OK (15 bytes) |

`QualifiedReferenceSubjectNameStrategy` maps `p6/shop/PAddress.proto` to `p6.shop.PAddress`, as spec §6 says.

The stack has two registries: one without authentication, used by everything below except the "Secured registry" section, and one with HTTP Basic auth and ACLs.

## Avro rules that are stricter than the registry (spec §5.1 steps 4 and 7)

Added on 2026-11-17. Pinned by `integration/tests/test_validate_real_sr.py::test_avro_rules_the_registry_does_not_have`.

| Schema | Registry (compatibility check on a new subject) | `validate` |
|---|---|---|
| Nested type with `"namespace": ""`, used by its bare name | `is_compatible: true` | `Type 'Inner' needs a namespace. ...`, no registry call |
| Nested type with `"namespace": null`, used by its bare name | `is_compatible: true` | the same message |
| Type name wrapped as `{"type": "<ns>.Inner"}` | `is_compatible: true` | `Invalid Avro schema: write the type '<ns>.Inner' as a plain string, ...` |
| Type used before it is defined | `is_compatible: false`, `"Later" is not a defined name` | `Invalid Avro schema: type '<ns>.Later' is used before it is defined` |

The first three are accepted by the registry and by Java clients, but fastavro (confluent-kafka-python) cannot parse them, so the validator rejects them.

## Secured registry (spec §7, E4)

Checked against `schema-registry-secured` in `integration/docker-compose.yml`: the same 7.9.0 image with HTTP Basic auth (a users file) and the Confluent Security Plugin in ACL mode. Pinned by `integration/tests/test_secured_sr.py` (18 tests). No code change was needed.

| What was tested | Observed |
|---|---|
| `validate` with no credentials, a wrong password, an unknown user | `401 {"error_code": 401, "message": "Unauthorized"}` on the first call; result E4 |
| `validate` as a user with no ACLs, with and without a reference row | `403 {"error_code": 40301, "message": "User is denied operation Read on Subject: <s>"}` (or `Write`); result E4 |
| Which operation each call needs | Version lookups: `SUBJECT_READ`. `GET /config/{s}?defaultToGlobal=true`: `SUBJECT_COMPATIBILITY_READ` (`denied operation ReadCompatibility`). `POST /compatibility/subjects/{s}/versions`: `SUBJECT_WRITE` (`denied operation Write`), although it stores nothing |
| `validate` as a user with `SUBJECT_READ` and `SUBJECT_COMPATIBILITY_READ` only | E4: the compatibility check is refused |
| `validate` as a user with the three operations on `*` | Create: `Ok`, and the subject is not created. Edit: `Ok` for a compatible change, the incompatibility message for an incompatible one. With a reference, and with a reference of a reference (the `/versions/{v}` lookup): the pinned `Warning`. A subject that does not exist gives 404 (40401), not 403 |

## Differential run against the registry

`integration/differential.py` generates schemas (valid seeds and random mutations of them, all three formats, with and without a reference), runs `validate` as a create and then registers the same text under the same name. On 2026-11-17, seeds 2 and 3 with 700 cases per format (4,200 schemas):

| Outcome | Count |
|---|---|
| `validate` passed and the registry rejected the registration (false pass) | 0 |
| `validate` raised or gave an internal error | 0 (3 before the fix below) |
| Both passed / both rejected | 1,270 / 2,257 |
| Rejected by one of the validator's own rules, registered by the registry | 210 |
| Rejected by a local parser (fastavro, jsonschema, protoc), registered by the registry | 463 |

The last group was read by message. Every case is a defect the registry does not check: Avro defaults of the wrong type and `{"type": "<name>"}` around a type name (fastavro cannot resolve it); JSON keywords the meta-schema rejects (duplicate `required` entries, negative `minLength`); Protobuf that `protoc` does not compile (types used without their import, duplicate or out-of-range field numbers, first enum value not zero, duplicate imports or packages).

A further run after the audit fixes (2026-11-17, seed 4, 700 cases per format, 2,100 schemas): 0 false passes, 0 internal errors; both passed / both rejected 632 / 1,118; rejected by an own rule 103; rejected by a local parser 247 (Avro 19, JSON 28, Protobuf 200); stage E only 0.

After the whole-branch review fixes (2026-11-17, seed 5, 300 cases per format, 900 schemas): 0 false passes, 0 internal errors; both passed / both rejected 274 / 471; rejected by an own rule 54; rejected by a local parser 101 (Avro 6, JSON 17, Protobuf 78); stage E only 0.

After the JSON top-level rules and the nesting limit (2026-11-17, seed 6, 700 cases per format, 2,100 schemas): 0 false passes, 0 internal errors; both passed / both rejected 616 / 1,109; rejected by an own rule 112; rejected by a local parser 263 (Avro 7, JSON 35, Protobuf 221); stage E only 0.

After the reference versions were chosen per row (2026-11-17, seed 9, 400 cases per format, 1,200 schemas): 0 false passes, 0 internal errors; both passed / both rejected 349 / 639; rejected by an own rule 71; rejected by a local parser 141 (Avro 16, JSON 15, Protobuf 110); stage E only 0.

With reference versions alternating between 1 and 2 (final review fix, 2026-11-17, seed 10, 300 cases per format, 900 schemas, a second compatible Address version registered, registration with the row's version): 0 false passes, 0 internal errors, exit 0; both passed / both rejected 253 / 483; rejected by an own rule 57; rejected by a local parser 107 (Avro 4, JSON 20, Protobuf 83); stage E only 0.

Code change: an Avro record whose `name` is a list or object gave `Internal validator error`; it now gives `Invalid Avro schema: the record "name" must be a string`.

## Spec §10 rows 5a, 9, 10 and 11

First answered by one-off probe scripts; since 2026-11-17 tests in `integration/tests/test_sr_behaviour.py`.

| Row | Observed | Pinned by |
|---|---|---|
| 5a | The registry registers a JSON schema with any `$schema` value, including draft-03 and an unknown URL | `test_sr_accepts_any_json_schema_draft` |
| 9 | `X.Case` and `X.case` are two subjects. Names with a space, non-ASCII letters, a slash, a leading digit or 300 characters register. A name starting with `:.<ctx>:` lands in that context, not in the default one | `test_sr_subject_names_are_case_sensitive`, `test_sr_accepts_odd_subject_names`, `test_sr_context_prefix_lands_in_another_context` |
| 10 | Deleting a referenced subject: 422 (42206), and `referencedby` lists the referencing schema id. After a soft delete the subject is "not registered", an incompatible schema passes the compatibility check and registers as version 2 | `test_sr_refuses_to_delete_a_referenced_subject`, `test_soft_delete_drops_the_history_for_compatibility` |
| 11 | A reference with version `-1` registers and is stored as the concrete latest version | `test_sr_stores_the_latest_version_for_reference_version_minus_one` |

## Avro names from a reference of a reference

Added on 2026-11-17. Address references Country; a schema selects only the row for Address and uses `<ns>.Country` directly.

- The registry registers it with the one reference, and the Python `AvroSerializer` serializes and produces with it (`integration/tests/test_python_clients.py::test_avro_name_from_a_transitive_reference`).
- Code change: such a use counts as a use of the row it was loaded for. Before, `validate` answered `Your schema uses '<ns>.Country' but no reference is selected for it.`

**A type nested inside a referenced schema cannot be used from another schema.** With the enum `Country` defined inside the referenced `Address`, a schema that uses `<ns>.Country` registers, but the Python `AvroSerializer` (confluent-kafka-python 2.16.1) cannot load it: `redefined named type: <ns>.Country` when Address is used too, a `TypeError` when it is not (`test_python_client_cannot_use_a_type_nested_in_a_reference`). The Java `KafkaAvroSerializer` fails as well (`Invalid schema ... with refs`), whether the client's schema is built from the two registered texts or has Address inlined (`probe.Nested`). Code change (owner decision, 2026-11-17, replacing OD3): `validate` rejects the use with `Type '<ns>.Country' is defined inside the Schema '<ns>.Address'. ...` (`test_validate_real_sr.py::test_avro_name_nested_in_a_reference`).

**Protobuf and JSON Schema do not have this problem.** Checked on 2026-11-17 with the real Python serializers: a message and an enum nested inside an imported Protobuf schema (`Address.Geo`, `<ns>.Address.Kind`), and a JSON definition inside a referenced schema reached with `address.json#/definitions/Geo` (which itself points at another definition), are accepted by `validate`, registered and produced; the nested JSON definition is applied when the payload is validated. Pinned by `test_protobuf_types_nested_in_an_imported_schema` and `test_json_definitions_of_a_referenced_schema_two_levels_deep`. The Java serializers behave the same for both shapes (`probe.Nested` in `integration/java`, which exits 1 if either stops working).

## One version per subject (audit F1)

Added on 2026-11-17. Money v1 is registered, Line references Money v1, then Money v2 is registered. Order uses Line and Money directly (latest, v2).

- The registry registers Order with both references. The Python `AvroSerializer` writes Money v2 in both places; a reader that resolves Money v1 everywhere (what the Java serializer does) misreads `total` (`integration/tests/test_python_clients.py::test_two_versions_of_one_type_corrupt_data`).
- Code change: D7 (spec §4, §6). `validate` answers `'<ns>.Money' is used at more than one version: v1 through '<ns>.Line', v2 directly. ...` and the same for a reference name used for two Schemas (`integration/tests/test_validate_real_sr.py::test_version_conflict_real_sr`). Before, it answered `Warning`.

## Reference versions chosen per row

Added on 2026-11-17. Money v1 and v2 are registered; a row picks v1.

- `validate` answers `Warning`: `References will be pinned to: <ns>.Money v1 (latest is v2). To update one, select a newer version for it.` The registry takes the schema with exactly Money v1, and the Python serializer (`use.latest.version`) produces with it (`integration/tests/test_python_clients.py::test_older_pinned_version_registers_and_produces`).
- A version the subject lacks, or one that was soft-deleted, gives `'<ns>.Address' has no version N (the latest is M).` (`integration/tests/test_validate_real_sr.py::test_unknown_version_is_not_found`, `test_soft_deleted_version_is_not_found`).
- The F1 conflict with this schema's own row now offers the fix the user can make (`test_version_conflict_real_sr`).

## Final review fixes: reference order and pinned versions in Java, 2026-11-17

- **Reference order.** The registry's lookup (`POST /subjects/{s}`) compares references in order: a schema with two references is found with the same order (200) and not with the reversed order (404, error_code 40403) (`integration/tests/test_sr_behaviour.py::test_lookup_is_reference_order_sensitive`). Registration does the same lookup, so a reordered save would register an identical new version. Code change: on a level change with the same references in another order, `validate` answers `LEVEL_REFERENCE_ORDER` (`tests/test_validate.py::test_level_change_with_reordered_references_says_keep_the_order`); the platform note and spec §11 step 3 say to keep the registered order.
- **Version-less row.** `resolve` raises `ValueError("reference row without a version")` instead of using the latest (`tests/test_references.py::test_resolve_row_without_a_version_fails_closed`); `validate` reports it as an internal error, and stage A makes it unreachable.
- **Older pinned version, produced bytes.** `test_older_pinned_version_registers_and_produces` now asserts that the payload after the 5-byte header is `0x0a` (amount 5 and nothing else), so Money v2's `currency` would fail it.
- **Java, `probe.Pinned`** (Money v1 and v2 registered, Line references Money v1; `KafkaAvroSerializer`, `use.latest.version=true`, `auto.register.schemas=false`, RecordNameStrategy):
  - (a) client schema with Money v1 inlined: serializes (6 bytes) and reads back `{"price": {"amount": 5}}`.
  - (b) client schema with Money v2 inlined (reported, not counted): also serializes, 6 bytes, and reads back `{"price": {"amount": 5}}`. It does not fail: the serializer writes with the registered Line (Money v1), so `currency` is silently dropped. The note and spec §12 say this.
  - Last line: `Pinned: all cases as expected`; `probe.Main`, `probe.ProtoRefs`, `probe.Nested` and `probe.Pinned` all exit 0 (the README Java command).
- **Messages check** (every message in `messages.py` appears in README word for word; prints the ones that do not): `.venv/bin/python -c "import re,schema_validator.messages as m; t=open('README.md').read(); print([k for k,v in vars(m).items() if k.isupper() and isinstance(v,str) and not all(s in t for s in re.split(r'\{[^}]*\}',v) if len(s)>8)])"` gives `[]`.

## Top-level enum and fixed (audit F5)

Tested: `<ns>.Color` (enum) registered as its own Schema, then a record `<ns>.Paint` with a field of type `<ns>.Color` and a reference row for it. `integration/tests/test_python_clients.py::test_avro_shared_enum` validates both (no Warning on the record), registers them, serializes with the Python `AvroSerializer`, produces, and reads the value back with `AvroDeserializer`. `probe.Nested` does the same with Java `KafkaAvroSerializer` (client schema with `AColor` inlined) and reads `{"color": "GREEN"}` back; it prints `Nested: all cases as expected`.

Observed: the registry, Python and Java all accept a record that references a separately registered enum. Before the change `validate` rejected the enum itself with the top-level message, although `AVRO_NESTED_IN_REFERENCE` tells users to move such a type to its own Schema.

Code changed: step 2 of `formats/avro.py` accepts `record`, `enum` and `fixed` (the namespace rule and name derivation are unchanged); `AVRO_TOP_LEVEL` now ends "must be a single record, enum or fixed."; README and spec 5.1 updated.

## Avro reference text (audit L6)

Tested: unit tests (`tests/test_rules.py`, `tests/test_validate.py`), no registry needed.

Observed: an Avro row with its own `ref_string` (for example `address.avsc` for `a.Address`) passed stage A, although the registry resolves Avro types by full name.

Code changed: A5 gives `AVRO_REF_STRING` for each Avro row whose `ref_string` is neither empty nor `schema_name`, one per row in row order, and skips stage D; `normalise_rows` always uses `schema_name` for Avro. README and spec 3.1, A5 updated.

## Schema that is not text (audit L8b)

Tested: `test_lone_surrogate_schema`: schema `"\ud800"`.

Observed: `.encode("utf-8")` in A2 raised `UnicodeEncodeError` out of `check_input`.

Code changed: A2 gives the fatal `SCHEMA_NOT_TEXT`. Only `schema` is checked. README and spec A2 updated.

## Own name on an edit (audit L9)

Tested: `test_edit_ignores_own_name_in_existing_names` and `test_edit_still_catches_other_case_variant`.

Observed: an edit whose `existing_names` listed its own stored name got `NAME_EXISTS`.

Code changed: C2 leaves entries exactly equal to `previous_name` out on an edit; case variants of other names still count. README and spec C2 updated.

## Request deadline (audit L1)

Tested: `test_slow_body_hits_deadline` (unit); the reviewer's fake registry `t13_failures.py` (`slow` and `drip` cases, run against `fake_sr.py` on port 18999).

Observed: a registry sending one byte every 0.5 s with `sr_timeout_seconds=2` held the call for 10.6 s and then passed; `requests`' `timeout` bounds each read, not the whole response.

Code changed: requests are streamed and the body is read with `response.raw.read1(65536, decode_content=True)` until it returns `b""`, with a wall-clock check after each call against start + `sr_timeout_seconds`; an overrun is E3. `iter_content(chunk_size=None)` was tried first and did not help, because urllib3 reads a whole non-chunked body in one call (`drip` still took 10.6 s). Without `raw.read1` (urllib3 1.x) the client falls back to `iter_content(chunk_size=1)`. Measured with urllib3 2.8.0: `slow` and `drip` of `t13_failures.py` both end in 2.0 s with "Could not reach"; a 1 MB body reads in 0.0001 s with `read1` (about 1.2 s with `chunk_size=1`). A body that stalls or breaks mid-read (urllib3 `ReadTimeoutError`, `ProtocolError`, `DecodeError`, or an `OSError`) is E3: `test_error_while_reading_the_body_is_unavailable` (unit) and `test_stalled_body_is_unreachable` (a local socket server sending 5 of 100 bytes, no Docker). Spec 7 and README updated.

## No redirects (audit L3)

Tested: `test_redirects_not_followed` (unit) and the `redirect` case of `t13_failures.py`.

Observed: `requests` followed a 302, so Basic credentials could go to another host and a different endpoint could answer.

Code changed: every request has `allow_redirects=False`; a 3xx is E3 ("not 2xx"). Spec 7 updated.

## No raw registry bodies (audit L2)

Tested: `test_raw_body_never_in_rejected`, `test_sr_rejected_structured_message_not_echoed`, `test_plain_message_still_shown` (unit); the `e400raw`, `e409` and `e422other` cases of `t13_failures.py`. The integration tests are unchanged.

Observed: a 4xx with an `error_code` but no `message` field showed the whole body, including an echoed `Authorization` header, and a message with the request or the other team's schema embedded was shown as it was.

Code changed: `SchemaRegistryRejected` holds the body's `message` or "" plus `.code`. The stage E text is the `details:` reason, else a non-empty message without `{` when the `error_code` is Schema Registry's own (409, or 40000 to 49999; added after the whole-branch review, because a proxy's 4xx can carry an `error_code` such as 400 with a message of its own), else `Schema Registry rejected the schema (error code {code}).` README and spec 7 updated.

## Subject mode (audit F6)

Tested: `test_get_mode`, `test_get_mode_bad_body`, `test_readonly_subject_is_an_error`, `test_mode_inherited_from_global_blocks`, `test_import_mode_is_an_error`, `test_mode_failure_maps_like_other_stage_e_calls` (unit); `test_mode_default_to_global`, `test_mode_readonly_subject`, `test_readonly_subject_rejected` and, on the secured registry, `test_mode_lookup_by_validator_user` (integration; the case C4 of `t12_compat_auth.py`).

Observed: `GET /mode/<new subject>?defaultToGlobal=true` gives `200 {"mode":"READWRITE"}`. After `PUT /mode/<s>` with READONLY, registering gives 422 with error_code 42205, while the compatibility check still says compatible, so `validate` returned `Ok` and the registration failed. On the secured registry the `validator` user gets 200 from the mode lookup with its existing three ACLs; a user without them gets `403 {"error_code":40301,"message":"User is denied operation ReadCompatibility on Subject: x"}`. No ACL was added.

Code changed: `get_mode` on the client; stage E checks it after a compatible verdict and gives E5 for any mode other than `READWRITE`. README and spec 4 and 7 updated.

## Level change and schema change in one save (audit F4)

Tested: `test_level_and_schema_change_together_is_an_error`, `test_level_change_with_unchanged_schema_warns`, `test_level_change_without_versions_is_not_blocked`, `test_same_level_unchanged_behaviour`, `test_lookup_uses_latest_versions_own_references`, `test_level_change_lookup_failure_is_an_error`, the `is_registered` tests in `test_sr_client.py` and `test_fake_sr.py` (unit); `test_level_and_schema_change_together_is_an_error` (the case C3 of `t12_compat_auth.py`: NONE to FULL with an incompatible edit), `test_level_change_with_unchanged_schema_warns`, and on the secured registry `test_schema_lookup_by_validator_user` and `test_level_and_schema_change_by_validator_user` (integration).

Observed: `POST /subjects/{s}` with a registered text gives 200 with the version; with a text that is not registered, 404 `{"error_code":40403,"message":"Schema not found"}`; for a subject that does not exist, 404 error_code 40401. On the secured registry the `validator` user gets 200 and 404 from it with its existing ACLs (it needs `SUBJECT_READ`; a user without it gets `403 User is denied operation Read on Subject: x`). No ACL was added.

Code changed: `is_registered` on the client (40403 is a not-found code for this call only); stage E checks the level before the compatibility call and returns E6 when the level differs and the schema is not registered. After the whole-branch review (F-I2), "unchanged" also needs the latest version's references and the rows to have the same `(name, subject)` pairs: a JSON row that keeps its `ref_string` but points at another Schema was counted as unchanged, because the lookup sends the latest version's own references. The version gap is closed: "unchanged" now compares `(name, subject, version)` triples, and the lookup uses the rows' references, so a newer selected reference version is E6 (`test_level_change_with_a_newer_reference_version_is_an_error`, `test_level_change_with_the_same_reference_version_warns`, `test_lookup_uses_the_selected_versions`; (int) `test_level_change_gap_is_closed`). README and spec 4 and 7 updated.

## Independent audit, 2026-11-17

An outside reviewer re-checked the library against the registry and the Python and Java clients (report: `validator-audit/REPORT.md`). One row per finding. The details of each change are in the sections above and below; tests are unit (`tests/`) unless marked (int) for `integration/tests/`.

| Finding | Our take | Change | Tests |
|---|---|---|---|
| F1 one type at two versions corrupts data | Agree | D7: one version per subject and one Schema per reference name across the graph (`REF_VERSION_CONFLICT`, `REF_NAME_CONFLICT`). See "One version per subject" | `test_version_conflict_direct_and_transitive`, `test_conflict_between_two_transitive_paths`, `test_diamond_same_version_is_not_a_conflict`, `test_name_conflict`, `test_conflict_recorded_on_a_skipped_revisit`, `test_version_conflict_is_an_error`; (int) `test_version_conflict_real_sr`, `test_two_versions_of_one_type_corrupt_data` |
| F2 JSON `$ref` not resolved against `$id` | Agree | `$ref` is resolved against the root `$id`; nested ids are rule errors (superseded by the JSON rules entry); a relative `$ref` under an `$id` needs the absolute URI as `ref_string`; malformed `$id` or `$ref` is `JSON_INVALID` | `test_relative_ref_under_id_resolves_to_absolute`, `test_ref_to_own_id_with_fragment_is_internal`, `test_nested_id_shapes_are_rule_errors`, `test_fragment_only_root_id_keeps_the_empty_base`, `test_non_string_id_is_ignored`, `test_draft04_uses_id`, `test_malformed_id_is_an_error`, `test_malformed_ref_is_an_error`, `test_keyword_names_as_property_names_or_data_are_not_flagged`; (int) `test_json_id_relative_ref_is_rejected`, `test_json_id_absolute_ref_string_works`, `test_json_nested_id_is_rejected` |
| F3 Java Protobuf references need the qualified strategy | Agree, but not detectable per schema | None, because the registry accepts the schema and the failure depends on a client setting; `QualifiedReferenceSubjectNameStrategy` is a documented required Java setting (README, plan section 12) | Java `probe.ProtoRefs`; (int) `test_protobuf_import_of_another_schema`, `test_sr_accepts_protobuf_reference_name_mismatch` |
| F4 compatibility checked at the current level | Agree; the owner chose option 2 | A level change and a schema change need separate saves (`LEVEL_AND_SCHEMA_CHANGED`, E6); `is_registered` on the client. See "Level change and schema change in one save" | `test_level_and_schema_change_together_is_an_error`, `test_level_change_with_unchanged_schema_warns`, `test_level_change_without_versions_is_not_blocked`, `test_same_level_unchanged_behaviour`, `test_level_change_lookup_failure_is_an_error`, `test_level_change_with_repointed_reference_is_an_error`, `test_level_change_with_the_same_reference_subjects_warns`, `test_level_change_with_a_newer_reference_version_is_an_error`, `test_level_change_with_the_same_reference_version_warns`, `test_lookup_uses_the_selected_versions`; (int) `test_level_change_gap_is_closed`, the `is_registered` and level-change tests named in that section |
| F5 top-level Avro enum/fixed rejected | Agree | Stage B accepts `record`, `enum` and `fixed` at the top | `test_top_level_enum`, `test_top_level_fixed`, `test_top_level_enum_needs_namespace`; (int) `test_avro_shared_enum`; Java `probe.Nested` |
| F6 subject mode not checked | Agree | `get_mode`; any mode other than `READWRITE` is E5 (`SR_MODE`) | `test_get_mode`, `test_readonly_subject_is_an_error`, `test_mode_inherited_from_global_blocks`, `test_import_mode_is_an_error`; (int) `test_mode_readonly_subject`, `test_readonly_subject_rejected`, `test_mode_lookup_by_validator_user` |
| F7 transitive-only names need no row | Disagree | None, because a name provided by a referenced Schema's own reference is a valid use (registry and both clients work) and F1/D7 now covers the corruption the reviewer linked to it | `test_avro_name_from_a_transitive_reference_is_provided_by_the_row`, `test_avro_row_used_only_through_another_rows_transitive_name_is_unused`; (int) `test_avro_name_from_a_transitive_reference` |
| L1 total deadline | Agree | Body read with `raw.read1` against start + `sr_timeout_seconds`; overrun is E3 | `test_slow_body_hits_deadline`, `test_error_while_reading_the_body_is_unavailable`; (int) `test_stalled_body_is_unreachable` |
| L2 raw registry bodies shown | Agree | `SchemaRegistryRejected` keeps only a structured message and the code; a message is shown only with error_code 409 or 40000 to 49999; otherwise `SR_REJECTED_CODE` | `test_raw_body_never_in_rejected`, `test_sr_rejected_structured_message_not_echoed`, `test_plain_message_still_shown`, `test_sr_rejected_message_shown_only_with_a_registry_error_code`, `test_sr_rejected_details_reason_shown_whatever_the_code` |
| L3 redirects followed | Agree | `allow_redirects=False`; a 3xx is E3 | `test_redirects_not_followed` |
| L4 JSON top level must be `"type": "object"` | Disagree | None, because it is a deliberate platform rule (`JSON_TOP_LEVEL`) | `tests/test_json_schema.py` top-level tests (unchanged) |
| L5 JSON reference cycle rejected | Disagree | None, because the cycle rule is deliberately the same for all formats (the registry rejects it for Avro) | `test_direct_cycle`, `test_transitive_cycle`, `test_reference_cycle` (unchanged) |
| L6 Avro `ref_string` | Agree | A5 gives `AVRO_REF_STRING` for a row whose `ref_string` is not empty or `schema_name` | `tests/test_rules.py` and `tests/test_validate.py` A5 Avro tests |
| L7 https draft-07 and `-` in titles | Reversed | None, because `https://json-schema.org/draft-07/schema` is read by jsonschema as 2020-12 with a DeprecationWarning, so it stays rejected (the change was made, then reverted). `-` in titles stays rejected: a platform choice | `test_https_draft07_rejected`; (int) `test_python_client_reads_https_draft07_as_2020_12` |
| L8 internal errors on deep JSON and non-text input | Agree | Deep JSON gives `JSON_TOO_DEEP`; a lone surrogate gives `SCHEMA_NOT_TEXT` (L8b) | `test_json_too_deep_still_reported_above_the_configured_limit`, `test_lone_surrogate_schema` |
| L9 own name in `existing_names` on an edit | Agree | C2 leaves `previous_name` out on an edit | `test_edit_ignores_own_name_in_existing_names`, `test_edit_still_catches_other_case_variant` |

Re-run of the reviewer's scripts against this code (the Java parts of the scripts need a container that does not exist here and were not run; the Python results and the printed `validate` results are what counts):

- `t05`: the diamond (C v1 through B, v2 through D2) gives `Error`, "used at more than one version".
- `t17`: Order using Line (Money v1) and Money (latest v2) gives `Error`, "used at more than one version: v1 through ..., v2 directly".
- `t06`: stopped at its first Java call; no `validate` result was produced after that point.
- `t07`: J2 gives `Error`; J3 (absolute `ref_string`) and J5 (internal `$id` ref) pass (`Warning` pinned, `Ok`); J4 gives `Warning`; J6/J6b are the deliberate L4 rule.
- `t12`: C3 (edit incompatible under NONE with the level changed to FULL in the same save) gives `Error`; C4 (subject READONLY) gives `Error`, and the registry's own register call gives 422 42205.
- Java probes `probe.Main`, `probe.ProtoRefs`, `probe.Nested` exit 0 (`Nested: all cases as expected`).

## Whole-branch review fixes, 2026-11-17

- F-I1, JSON `$id` false pass. Observed with the Python serializer (confluent-kafka 2.16.1, jsonschema 4.26.0, referencing 0.37.0): under a root `$id` of `https://acme.example/o.json`, a `$ref` to `https://acme.example/a.json` defined by an `$id` under draft-07 `$defs`, or under an unknown keyword such as `x-defs`, fails with `Unresolvable`, and in draft-07 an `$id` next to a `$ref` is ignored (the `$ref` resolves against the parent's base). `validate` had passed all three. Also observed: without `$schema` the serializer uses a 2020-12 validator, where an `$id` next to a `$ref` does move the base; below an unknown keyword the base depends on whether a pointer reaches the `$ref` directly or descends to it; and a subtree with its own `$schema` is registered by that draft. Code changed: the external refs follow `referencing` (spec 5.2 step 8). Its tests were replaced by the rules described in the JSON rules entry below.
- F-I2, E6 ignored the subjects of the references. Code changed: see "Level change and schema change in one save".
- F-L2, a rejection's message is shown only with Schema Registry's own error codes. See "No raw registry bodies".

## JSON `$id` and `$schema` rules, 2026-11-17

The resolver that mirrored `referencing` gave way to rules, because it took 90 s and 438 MB on a 277 KB chain of nested `$id`s and still passed a nested `$schema`. Only the top level may have `$id` (`id` in draft-04) or `$schema`; `$schema` is required; and in draft-07 and earlier the top level may not have an id beside a `$ref`. Observed with confluent-kafka 2.16.1, jsonschema 4.26.0 and referencing 0.37.0:

1. A schema without `$schema` and `"items": [{"type": "string"}]`: the registry takes it as draft-07; `JSONSerializer` fails with `SchemaError`, because it uses 2020-12. Pinned by `test_missing_schema_keyword_is_an_error`; (int) `test_json_without_schema_keyword_is_rejected`.
2. A 2020-12 root with `$id` `https://acme.example/o.json`, a subschema with its own draft-07 `$schema`, and below it `{"$id": "https://acme.example/sub/", "$ref": "a.json"}`: the nested `$schema` moves the client to draft-07 for that subtree, the `$id` beside the `$ref` is ignored, and the serializer fails with `Unresolvable` even with a row named `https://acme.example/sub/a.json`. Pinned by `test_nested_schema_keyword_is_an_error`; (int) `test_json_nested_schema_keyword_is_rejected`.
3. A draft-07 `$ref` to an `$id` under `$defs` or `x-defs`: the serializer fails with `Unresolvable`, the registry accepts it. Pinned by `test_nested_id_under_unknown_keyword_is_an_error`, `test_nested_id_shapes_are_rule_errors`; (int) `test_json_id_under_a_keyword_the_draft_does_not_know_is_rejected`, `test_json_nested_id_is_rejected`.

The rejection of long chains is pinned by `test_long_nested_id_chain_is_one_error_and_fast` (2,000 sibling ids and a 100-deep chain of nested ids, one message, under 1 s). Supersedes the F-I1 entry above.

### Maximum nesting depth (2026-11-17, owner decision: 64, first 100)

Measured against the test registry, jsonschema 4.26.0, fastavro and protoc (grpcio-tools). Depth counts nested JSON objects and arrays, the root being 1.

- JSON Schema: the registry accepts depth 601; `jsonschema`'s `check_schema` (also run by the Python client) raises `RecursionError` at the depths below (the first depth that raises, default 1000-frame limit, shallow stack). The pinned integration test uses depth 603, deeper than every cell.

| Draft | properties | allOf | not | items |
|---|---|---|---|---|
| 04 / 07 | 331 | 249 | 248 | 166 |
| 2019-09 | 199 | 165 | 124 | 100 |
| 2020-12 | 199 | 165 | 124 | 124 |

`check_schema` needs about 10 frames per level. The default `max_nesting_depth` is 64 (owner decision, 2026-11-17, lowered from 100): the tightest shape, a 2019-09 `items` chain, then uses about 640 of the 1000 frames, which leaves about 360 for a producer's own stack (a web framework, asyncio, Celery). At 100 a 2019-09 or 2020-12 schema at depth about 90 to 100 could pass `validate` and still raise `RecursionError` in a producer called from a deep stack. Pinned by `test_schema_shaped_at_limit_items` (2019-09 and 2020-12 `items` chains at depth 64 give no error) and `test_items_chain_at_the_old_limit_is_too_deep` (depth 100 is refused).
- The registry rejects JSON and Avro from about depth 1000 (`422 Invalid schema`), and Python's `json.loads` raises `RecursionError` there too.
- Avro: fastavro is fine up to depth 990.
- Protobuf: protoc stops at 31 nested message definitions (`Reached maximum recursion limit for nested messages.`), the registry accepts 500, and a chain of 5,000 separate messages compiles. Protobuf gets no limit; `test_protobuf_nesting_limit_is_protocs` pins protoc's.

The limit is `max_nesting_depth`, 64, checked before every other step of Avro and JSON analysis. Pinned by `test_json_depth`, and for each of Avro and JSON `test_depth_at_limit_passes_the_depth_check`, `test_depth_above_limit_is_an_error`, `test_parser_recursion_is_too_deep`, `test_configured_limit_is_used`; `test_json_too_deep_still_reported_above_the_configured_limit`; (int) `test_json_deeper_than_the_python_client_can_check_is_rejected` (depth 603: the registry accepts it, the Python serializer raises `RecursionError`).

Rule D (a root `$id` beside a root `$ref` in draft-04, -06 and -07) is pinned against the client by (int) `test_python_client_ignores_root_id_beside_root_ref_in_draft07`: with root `$id` `https://acme.example/o.json` and a relative `$ref` `a.json` below it, the serializer finds a reference named `a.json` and fails with `Unresolvable` for one named `https://acme.example/a.json`, so the `$id` is not the base.

## Packaging

`tests/test_packaging.py` builds the wheel from a copy of the tree with `setuptools.build_meta.build_wheel` on every unit-test run and checks that it holds every `.py` file and the 18 `.proto` files under `schema_validator/_proto/`, and no tests.

## Coverage

`coverage run --branch --source=schema_validator -m pytest -q` on 2026-11-17: 1,129 statements and 402 branches, all covered by the unit tests alone.

## Not verified

- The deadline covers the body, not the headers: a server dripping its status line and headers a byte at a time inside the per-read timeout can hold a call longer than `sr_timeout_seconds` (`requests` has no total-time limit for headers). The clock is checked once when the headers are in.
- The Kafka-side setup of the production registry and its real login module. The secured test registry uses a users file, not the platform database.
