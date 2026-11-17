# Verification record

Checked on 2026-11-16 (the secured registry and the Avro namespace follow-up on 2026-11-17) against `confluentinc/cp-schema-registry:7.9.0` on `apache/kafka:3.8.1`, from `integration/docker-compose.yml`, with confluent-kafka-python 2.16.1 and the Confluent 7.9.0 Java serializers.

```
.venv/bin/python -m pytest -q                                   # 549 passed
.venv/bin/python -m pytest integration/tests -m integration -q  # 97 passed, none skipped
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
| 6 | The full `validate` pipeline with `HttpSchemaRegistryClient.from_config` against the registry | Create, all formats: `Ok`, then the same schema registers under `request.name` with 200. Stage A, empty schema: `Schema is empty.` Stage B, broken text: `Schema is not valid JSON: ...` (Avro, JSON) and `Invalid Protobuf schema: ...`, with no registry call made. Stage C: `The name must match the schema. It should be '<ns>.Order'.` Stage D, reference to an unregistered subject: exactly `Referenced schema '<ns>.Address' is not registered in Schema Registry yet.` Stage E1, Avro with field `a` twice (fastavro accepts it): `Schema Registry rejected the schema: Duplicate field a in record <ns>.Order: a type:INT pos:1 and a type:INT pos:0.` Stage E2, all formats: one line starting `Not BACKWARD-compatible with version 1: `, without the old schema text. References, all formats: `Warning` `References will be pinned to: <ns>.Address v1. ...`, then registering with that row gives 200. Avro, an enum `<ns>.Country` defined inside the registered `<ns>.Address`, with one row for `<ns>.Address`: a Customer using `<ns>.Address` and `<ns>.Country`, and one using only `<ns>.Country`, each give that `Warning`, then register with the reference `{name: <ns>.Address, subject: <ns>.Address, version: 1}` with 200. Edit with the subject level set to `FULL` by `PUT /config/{subject}` and `compatibility="BACKWARD"`: `Warning` `Compatibility was checked using the current level 'FULL', not the new level 'BACKWARD'.` Registry at `http://localhost:1`, 1 s timeout: `Could not reach Schema Registry to validate. Try again.` | `test_validate_real_sr.py` (20 tests) | None |
| 7 | The incompatibility and invalid-schema messages still parse with `sr_messages`: register v1, check an incompatible v2 | Each format gives one description, `old_version == 1`, `level == "BACKWARD"`, no `oldSchema` text. Raw first messages: Avro `{errorType:'READER_FIELD_MISSING_DEFAULT_VALUE', description:'The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema', additionalInfo:'zip'}`; JSON `{errorType:"TYPE_CHANGED", description:"A type at path '#/properties/id' is different between the new schema and the old schema'}` (opens with `"`, closes with `'`; the parser strips either); Protobuf `{errorType:"FIELD_SCALAR_KIND_CHANGED", description:"The kind of a SCALAR field at path '#/Order/1' in the new schema does not match its kind in the old schema"}` | `test_sr_behaviour.py::test_incompatible_messages_parse`, `test_new_subject_invalid` | None |

## Code changed because of an observation

One change, in `schema_validator/sr_client.py` (`HttpSchemaRegistryClient._call`), pinned by `tests/test_sr_client.py::test_other_404_on_level_and_compatibility_is_unavailable` and `integration/tests/test_sr_behaviour.py::test_404_from_a_wrong_base_url_is_not_not_found`.

- Observed: with a wrong prefix in `sr_url`, `POST /compatibility/...` returns `404 {"error_code":404,"message":"HTTP 404 Not Found"}`.
- Before: only the two version lookups treated a 404 without error_code 40401/40402 as unavailable. On the compatibility and level calls such a 404 was `SchemaRegistryRejected`, so `validate` of a create returned `Schema Registry rejected the schema: HTTP 404 Not Found` (E1), blaming the schema for a configuration fault.
- After: a 404 without error_code 40401/40402 is `SchemaRegistryUnavailable` on every call, so the result is E3 (`Could not reach Schema Registry to validate. Try again.`). A 404 with 40401/40402 is unchanged: `None` from the two version lookups, `SchemaRegistryRejected` on the level and compatibility calls. This departed from spec §7's "any other 4xx with an `error_code`: E1"; spec §7 now states the rule. It covers every 404 whose `error_code` is not 40401 or 40402: `error_code` 404, the HTTP server's generic code, and also Schema Registry's other 404 codes (40403 to 40409, for example `Schema not found`), which do not mean that the looked-up subject or version is not registered.

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

Checked against `schema-registry-secured` in `integration/docker-compose.yml`: the same 7.9.0 image with HTTP Basic auth (a users file) and the Confluent Security Plugin in ACL mode. Pinned by `integration/tests/test_secured_sr.py` (14 tests). No code change was needed.

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

Code change: an Avro record whose `name` is a list or object gave `Internal validator error`; it now gives `Invalid Avro schema: the record "name" must be a string`.

## Packaging

The wheel ships the bundled `.proto` files. This was verified in the final branch review, not rerun for this record: a wheel built from a copy of the tree with `setuptools.build_meta.build_wheel` holds the 18 `.proto` files under `schema_validator/_proto/`, and `protobuf.analyse` compiled `google/type/date.proto` and `confluent/meta.proto` from the unpacked wheel. (The project's virtual environment has no `pip`, so `pip wheel` was not used.)

## Not verified

- The Kafka-side setup of the production registry and its real login module. The secured test registry uses a users file, not the platform database.
