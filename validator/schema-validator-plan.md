# Schema deployment validator: implementation plan

Target: Python 3.11 library that validates a "Schema" deployment in the America platform before it is registered in Confluent Schema Registry (SR).

This document is self-contained. Implement exactly what is written here. The assumptions that were marked VERIFY in the first draft have been checked against a real Schema Registry 7.9.0 and real Python and Java clients; the results are in section 10 and are already folded into the rules below.

---

## 1. Context

- America is an internal provider platform. Users create "deployments": Topics, Services, and now **Schemas**.
- A Schema deployment has: a name, a format (Avro / JSON Schema / Protobuf), a compatibility level, the schema text, and a list of references to other Schema deployments.
- A topic deployment selects a Schema deployment (as key or value) in its advanced config. One schema can be used by many topics.
- Only America registers schemas in SR. Client applications never register; they only read.
- Clients use **RecordNameStrategy**, so the SR subject name is derived from the schema itself, not from the topic.
- America cannot set or rewrite the deployment name. It can only call a validation hook and show the result. So the validator must tell the user what the name has to be.
- The environment is air-gapped. Use only: standard library, `fastavro`, `jsonschema`, `grpcio-tools` (with `protobuf`), and `requests` (if `requests` is unavailable, use `urllib.request`).

Schema Registry is Confluent Platform 7.9. The deployment name **is** the SR subject name.

---

## 2. Settled decisions

| Topic | Decision |
|---|---|
| Language | Python 3.11 |
| Subject naming | RecordNameStrategy; subject = deployment name = name derived from schema |
| Namespace | Required in every schema. No tracking of which team owns which namespace |
| Uniqueness | Name must be unique among Schema deployments, compared lowercased |
| Rename on edit | Not allowed. Name must equal the previous name |
| References | Supported in v1. Explicit list on the deployment: selected schema + text as written in the schema (the text is needed for JSON only; see the next two rows) |
| Protobuf import path | Fixed by convention: a referenced Schema `com.acme.Address` must be imported as `com/acme/Address.proto` (dots to slashes, plus `.proto`). The validator derives it; the user does not type it. Java producers must set `reference.subject.name.strategy` to `QualifiedReferenceSubjectNameStrategy` (section 12) |
| JSON `title` | Dot-separated identifiers only: `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$`. SR accepts any subject name (spaces, `/`, `:`, `*`, a `:.ctx:` context prefix), so the validator has to restrict it |
| Library versions | Developed and verified with fastavro 1.13.1, jsonschema 4.26.0, grpcio-tools 1.85.1, protobuf 7.36.2, requests 2.34.2 |
| Reference versions | Not chosen by the user. Pinned to latest at registration time |
| Who resolves versions for registration | America's registration step asks SR itself. The validator only reports versions in its message |
| Validity and compatibility verdict | Schema Registry, via its compatibility endpoint. Local parsing is for naming, structure and early errors |
| Key vs value | Chosen on the topic side. Irrelevant to the validator (same subject either way) |
| Permissions, delete-blocking, registration | America's responsibility, out of scope here |

---

## 3. Interface

The validator is a library function. The HTTP wiring to America's validationURL is done separately by the platform team.

### 3.1 Input

```python
@dataclass(frozen=True)
class ReferenceInput:
    schema_name: str          # the Schema deployment selected in the dropdown (= SR subject)
    ref_string: str | None    # how it is written in the schema text.
                              # JSON: required (the $ref value without its #fragment).
                              # AVRO: optional; empty or schema_name (anything else is rejected, A5).
                              # PROTOBUF: ignored; derived from schema_name (section 6).

@dataclass(frozen=True)
class ValidationRequest:
    name: str                         # deployment name typed by the user
    schema_type: str                  # "AVRO" | "JSON" | "PROTOBUF"
    schema: str                       # schema text
    compatibility: str                # level chosen on the deployment
    references: list[ReferenceInput]  # may be empty
    previous_name: str | None         # current name on edit, None on create
    existing_names: list[str]         # all OTHER Schema deployment names (excluding this one)
```

Configuration (constructor arguments or environment, not per request):

- `sr_url`, `sr_username`, `sr_password`, `sr_timeout_seconds` (default 10)
- `max_schema_bytes` (default 500_000)
- `max_nesting_depth` (default 100): the deepest nesting of JSON objects and arrays an Avro or JSON schema may have, the root counting as 1. Measured 2026-11-17: the registry takes JSON of depth 601, but `jsonschema`'s `check_schema` (the Python client runs it) overflows the stack between depth 322 and 601; the registry and Python's `json.loads` give up from about 1000 (`422 Invalid schema`); fastavro copes up to 990.
- `allowed_json_drafts` (default: draft-04, draft-06, draft-07, 2019-09, 2020-12). This is platform policy only: SR 7.9 accepts any `$schema` value, including unknown URLs.
- `builtin_proto_imports`: the exact set of import paths SR 7.9.0 resolves without a reference (a prefix list is wrong: SR does not resolve `google/type/localized_text.proto` or `google/rpc/*`). Default:
  - `google/protobuf/`: `any`, `api`, `descriptor`, `duration`, `empty`, `field_mask`, `source_context`, `struct`, `timestamp`, `type`, `wrappers` (each `.proto`)
  - `google/type/`: `calendar_period`, `color`, `date`, `datetime`, `dayofweek`, `decimal`, `expr`, `fraction`, `interval`, `latlng`, `money`, `month`, `phone_number`, `postal_address`, `quaternion`, `timeofday` (each `.proto`)
  - `confluent/meta.proto`, `confluent/type/decimal.proto`

### 3.2 Output

```python
@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    validationType: str   # "Ok" | "Warning" | "Error"
    message: str
```

Rules:

- Any error: `valid=False`, `validationType="Error"`, `message` = all error messages joined with `"\n"`.
- No errors, at least one warning: `valid=True`, `validationType="Warning"`, `message` = warnings joined with `"\n"`.
- Nothing to say: `valid=True`, `validationType="Ok"`, `message=""`.
- `Error` never comes with `valid=True`; `Warning` and `Ok` never come with `valid=False`.
- The message is shown directly to end users. Write messages as instructions: what is wrong and what to change.

### 3.3 Entry point

```python
def validate(request: ValidationRequest, sr: SchemaRegistryClient, config: ValidatorConfig) -> ValidationResult
```

`validate` must never raise. Unexpected exceptions become a single Error: "Internal validator error: ...".

---

## 4. Validation pipeline

Collect all errors; do not stop at the first one. A stage is skipped only when it depends on a stage that failed (noted below).

### Stage A: input sanity

| Code | Check | Message template |
|---|---|---|
| A1 | `schema_type` is one of AVRO, JSON, PROTOBUF | `Unsupported schema format '{x}'. Use AVRO, JSON or PROTOBUF.` |
| A2 | `schema` is text that encodes as UTF-8, is not empty or whitespace only, and its UTF-8 size is within `max_schema_bytes` | `Schema contains characters that are not valid text.` / `Schema is empty.` / `Schema is too large ({n} bytes, limit {max}).` |
| A3 | `compatibility` is one of BACKWARD, BACKWARD_TRANSITIVE, FORWARD, FORWARD_TRANSITIVE, FULL, FULL_TRANSITIVE, NONE | `Unknown compatibility level '{x}'.` |
| A4 | `name` is non-empty, has no whitespace, does not start with `:` | `Schema name is invalid.` |
| A5 | No two reference rows share a `ref_string` or a `schema_name`; no row references `name` itself; an AVRO row's `ref_string` is empty or equals its `schema_name` (one message per offending row, in row order, after the duplicate messages) | `Reference '{x}' is listed twice.` / `A schema cannot reference itself.` / `For an Avro reference to '{s}', leave the reference text empty or write '{s}'.` |

If A1 or A2 fails, stop and return. If A5 fails, stage D is skipped (no reference lookups, no D4/D5); stages B and C still run.

### Stage B: parse and structural rules (per format, section 5)

Produces: `expected_name` and `external_refs` (the set of external names the schema text uses).

If the text cannot be analysed (Avro and JSON: not JSON, or the wrong top level; Protobuf: protoc fails), report the error and skip C3, D4, D5 and E. Exception: when protoc fails only because files the schema itself imports are missing, stage B reports nothing and D4/D5 run on the import statements found in the text (section 5.3). A local validity error (`Invalid Avro schema`, `Invalid JSON schema`) or a naming error does not skip C3, D4 or D5; it skips E.

### Stage C: name rules

| Code | Check | Message template |
|---|---|---|
| C1 | Edit only: `name == previous_name` | `A schema's name cannot change after creation (was '{previous_name}'). Create a new Schema instead.` |
| C2 | `name.lower()` not in lowercased `existing_names`; on an edit, entries exactly equal to `previous_name` are left out (the schema's own stored name), other case variants still count | `A Schema named '{name}' already exists (names are compared case-insensitively).` |
| C3 | `name == expected_name`, exact and case-sensitive | `The name must match the schema. It should be '{expected_name}'.` |

On edit, if the user changed the namespace or record name inside the schema, C3 fires with the new expected name while C1 holds the name fixed. In that case replace both with one message: `The schema's full name changed from '{previous_name}' to '{expected_name}'. A schema's name cannot change; revert the namespace/name or create a new Schema.`

### Stage D: references (section 6)

| Code | Check | Message template |
|---|---|---|
| D1 | Each referenced `schema_name` exists in SR | `Referenced schema '{s}' is not registered in Schema Registry yet.` |
| D2 | Referenced schema has the same format | `'{s}' is a {their_type} schema; a {my_type} schema can only reference {my_type} schemas.` |
| D3 | No cycle: following references transitively from each referenced schema never reaches `name` | `Reference cycle: '{s}' already depends on '{name}'.` / `References of '{s}' are nested more than 20 levels deep.` |
| D4 | Every external name used in the schema text has a reference row | `Your schema uses '{x}' but no reference is selected for it.` |
| D5 | Every reference row is actually used in the schema text | `'{x}' is selected as a reference but is not used in the schema.` PROTOBUF: `'{s}' is selected as a reference but the schema does not import it. Write the import as: import "{path}";` |
| D6 | JSON: `ref_string` is present and not empty on every row | `Reference to '{s}' needs the text used in the schema (the $ref value).` |
| D7 | One version per subject and one Schema per reference name, across the whole graph (the rows and everything they reference) | `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Save the Schemas that use an older version again so that they use v{latest}, then save this schema.` / `The reference name '{x}' is used for more than one Schema: {subjects}. Each Schema needs its own reference name.` |

A nested reference that is not registered gives D1 with the nested subject.

For AVRO, a missing `ref_string` defaults to `schema_name`; any other value is rejected by A5, so stage D always uses `schema_name`. For PROTOBUF, `ref_string` is always derived: `schema_name.replace(".", "/") + ".proto"`.

D3, D4 and D5 exist only in the validator. Verified: SR accepts unused reference rows in every format, accepts a Protobuf reference whose name differs from the import path (a Java consumer then fails with `"X" is not defined`), and does not detect cycles for Protobuf or JSON (Avro fails only by accident, with `Can't redefine`).

If there are references and no errors, add a Warning listing the versions that would be pinned right now:

`References will be pinned to: com.acme.Address v3, com.acme.Money v1. They stay on these versions until this schema is saved again.`

### Stage E: Schema Registry verdict (section 7)

Run only if stages A to D produced no errors.

| Code | Check | Message template |
|---|---|---|
| E1 | SR accepts the schema as valid | `Schema Registry rejected the schema: {reason}` / `Schema Registry rejected the schema without giving a reason.` / `Schema Registry rejected the schema (error code {code}).` |
| E2 | SR reports it compatible with existing versions | `Not {sr_level}-compatible with version {n}: {descriptions}` |
| E3 | SR reachable | `Could not reach Schema Registry to validate. Try again.` (Error; fail closed) |
| E4 | SR accepts the validator's credentials (no 401/403) | `The validator is not allowed to query Schema Registry. Contact the platform team.` (Error; fail closed) |
| E5 | The subject accepts new versions: its mode (or the global mode, when the subject has none) is `READWRITE`. Checked after a compatible verdict, on creates and edits alike | `Schema Registry does not accept new versions of '{name}' right now (mode {mode}). Contact the platform team.` (Error). A registry failure on the mode lookup is E3 or E4 |
| E6 | On an edit whose `compatibility` differs from the subject's current level, the schema is unchanged (see "Level change on edit" below). Checked before the compatibility call | `The compatibility level and the schema cannot change in the same save. Schema Registry checks a new version under the level in force when it is registered (now '{sr_level}'). Save the new level '{compatibility}' with the schema unchanged first, then change the schema.` (Error). A registry failure on the lookups is E3 or E4 |

E1 and E2 arrive in the same response shape and are told apart by the message text (section 7). SR's raw messages are not shown as they are: they embed the whole schema text. An `Invalid schema` message without a `details:` part, or with an empty one, is shown as `Schema Registry rejected the schema without giving a reason.`

Level change on edit (E6): SR checks a new version under the level in force when it is registered and has no call that checks under another level. So when the deployment's `compatibility` differs from the level SR has for the subject, the schema must be unchanged, whatever order the platform uses for `PUT /config` and the registration. "Unchanged" means that the latest version's references use the same reference names for the same subjects as the rows (the pairs `(name, subject)` of both are equal as sets), and that the schema, looked up with the **latest version's own references**, is already registered under the subject (`POST /subjects/{s}`). So a row pointing a reference name at another Schema is a schema change, even with the same text. Versions are not compared: references re-pinned to newer versions of the same subjects count as unchanged (an accepted gap, see below). If it is not unchanged, the result is the Error `The compatibility level and the schema cannot change in the same save. Schema Registry checks a new version under the level in force when it is registered (now '{sr_level}'). Save the new level '{compatibility}' with the schema unchanged first, then change the schema.` and no compatibility call is made. If it is unchanged, the check runs and the warning `Compatibility was checked using the current level '{sr_level}', not the new level '{compatibility}'.` is added. A subject without versions is not checked (the warning is added as before). The stage E order is: level (this rule), compatibility, mode, warnings. Known gap: if the latest versions of the references changed in the meantime, the re-save of the unchanged schema repins them under the old level.

A subject that was soft-deleted has no history: SR checks nothing and the validator treats it as a create.

---

## 5. Parsing, structure and name derivation per format

How Confluent derives the RecordNameStrategy subject (confirmed in Confluent docs): Avro uses the record full name (namespace + record name), Protobuf uses the message name, JSON Schema uses the `title`.

### 5.1 Avro

1. `json.loads` the text. Failure: `Schema is not valid JSON: {err}`. Then the depth check, before everything else: nested deeper than `max_nesting_depth`, or so deep that `json.loads` raises `RecursionError`, gives `Schema is nested more than {max} levels deep.` and nothing more. The check is on the submitted text only, not on referenced texts.
2. Top level must be a JSON object with `"type"` `record`, `enum` or `fixed`. A shared enum or fixed is a Schema of its own, which records reference. Reject top-level unions (JSON arrays), primitives, arrays and maps: `The top level of an Avro schema must be a single record, enum or fixed.` Steps 3 to 7 apply to an enum or fixed as to a record (it has no fields, so no external references of its own).
3. Full name:
   - if `name` contains a dot, `name` is the full name and `namespace` is ignored;
   - otherwise full name = `namespace + "." + name`.
4. Namespace required: the full name must contain at least one dot. `Avro schema needs a namespace, for example "namespace": "com.team.domain".` Every dot-separated part of the full name must be non-empty (`.Order`, `com..Order` and namespace `com.acme.` get the same message). The same holds for every named type nested in the schema text (record, enum, fixed): one whose `namespace` is `""`, `null`, not a string or not a well-formed dotted name gets `Type '{name}' needs a namespace. Remove its "namespace" entry so it uses the schema's namespace.` (for `null` Java Avro inherits the enclosing namespace and fastavro uses none, so the clients would disagree on the type's full name), and one whose `name` has an empty dotted part (`.Inner`, `Inner.`, `a..B`) gets `Type name '{name}' has an empty part around a dot. Use a plain name, for example "Address", or a full name, for example "com.team.domain.Address".` One message per type in document order, with `{name}` as written. A nested type without a `namespace` key inherits the enclosing one. The rule is not applied to the texts of referenced schemas.
5. `expected_name` = full name.
6. External references: walk the whole schema and collect
   - every named type **defined** (record, enum, fixed), resolving namespaces with Avro's inheritance rule (an unqualified name takes the nearest enclosing namespace);
   - every type name **used** as a string that is not a primitive (`null, boolean, int, long, float, double, bytes, string`) and not a complex-type keyword.
   - `external_refs` = used full names minus defined full names. Unqualified used names resolve against the enclosing namespace only. Java Avro would also try the null namespace, but fastavro does not, so a schema that relies on that fallback cannot be parsed by Python clients; its use is reported as an unselected reference (D4).
   - A named type defined inside the text of a row's own referenced schema (an enum `com.acme.Country` nested in the referenced record `com.acme.Address`) cannot be used: the registry resolves it through the reference, but neither the Python serializer (confluent-kafka-python 2.16.1) nor the Java one (Confluent 7.9.0) can produce with such a schema. The error is `Type '{name}' is defined inside the Schema '{schema}'. A type nested in another Schema cannot be used here. Move it to its own Schema and reference that.`, one per name, sorted by name, with `{schema}` the referenced schema's own full name. The use still counts as a use of that reference's `ref_string` (`com.acme.Address`), so D4 and D5 add nothing. A type nested in a schema may be used inside that schema. The same holds for a name that only a reference of a reference provides (the referenced `com.acme.Address` itself references `com.acme.Country`, and the schema uses `com.acme.Country`): the registry loads it together with the row, so its use counts as a use of the row it was loaded for. A name that has a row of its own counts for that row only.
7. Local validity: fetch each referenced schema's text from SR (recursively including its own references), parse them in dependency order into one dict with `fastavro.parse_schema(ref_schema, named_schemas=named)` (the dict is filled with full names as keys), then `fastavro.parse_schema(schema, named_schemas=named)`. A fastavro error becomes `Invalid Avro schema: {err}`. fastavro stops at the first unknown type and leaves the rest of the schema unchecked, so its `UnknownType` is left unreported only when the name is one step 6 found no definition for and no reference row carries it (a missing reference; D4 explains it). When a row's `ref_string` is that name but its text does not define the type, D4 is silent, so it is an `Invalid Avro schema: ` error: `type '{name}' is not defined by the reference selected for it`. For any other name it is an `Invalid Avro schema: ` error: `type '{name}' is used before it is defined` when the text defines the name further down (SR rejects that too), otherwise `unknown type '{name}'`. A name wrapped as `{"type": "<name>"}` (the registry resolves it, fastavro does not) gives `write the type '{name}' as a plain string, not as {"type": "{name}"}`. When step 4 already reported a nested type, fastavro not finding a name the text defines is not reported again. A record `name` that is not a string is `Invalid Avro schema: the record "name" must be a string` when fastavro reports nothing itself.

fastavro and SR (Java Avro 1.11.4) do not agree on everything, which is fine because both run:

- fastavro rejects a default that does not match the field type; SR accepts it. Keep the fastavro error: such a schema breaks Java code generation.
- SR rejects illegal name characters (`T-6`) and duplicate field names; fastavro accepts both. Stage E reports them.
- SR accepts top-level unions, and records, enums and fixed without a namespace. Rules 2 (unions) and 4 are the validator's own.

### 5.2 JSON Schema

1. `json.loads`, then the same depth check as Avro step 1 (`Schema is nested more than {max} levels deep.`). Must be a JSON object. `JSON schema is nested too deeply.` (step 6) remains for a `max_nesting_depth` set above what `check_schema` can follow.
2. `title` must be a non-empty string: `JSON schema needs a "title"; it becomes the schema name.`
3. Namespace required and characters restricted: `title` must match `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$`: `The "title" must be a dotted name made of letters, digits and underscores, for example "com.team.domain.Order".`
4. Top level must have `"type": "object"`: `The top level of a JSON schema must have "type": "object".`
5. `$schema` is required: `JSON schema needs a "$schema", for example "$schema": "http://json-schema.org/draft-07/schema#".` Without it Confluent reads the schema as draft-07 and the Python serializer as 2020-12 (verified: `"items": [...]` fails with `SchemaError` in the serializer). It must be in `allowed_json_drafts` (compare after removing a trailing `#`): `Unsupported JSON Schema draft '{x}'.` `https://json-schema.org/draft-07/schema` is rejected: jsonschema (the Python client) does not recognise it and validates with Draft 2020-12, with a DeprecationWarning that it will become an error.
6. Meta-schema check: `jsonschema.validators.validator_for(schema, default=Draft7Validator).check_schema(schema)`. Failure: `Invalid JSON schema: {first line of err}`. A `RecursionError` from the check (a schema nested a few hundred levels deep) gives `JSON schema is nested too deeply.` The explicit default is kept for a schema without `$schema` (reported in step 5). This local check is the only source of a useful message: SR answers every invalid JSON schema with just `Invalid JSON Schema`.
   - The id keyword below is `id` when `$schema` (without a trailing `#`) is draft-04, otherwise `$id`, also when `$schema` is missing or not allowed.
   - Rule D: in draft-04, -06 or -07, a root with both `$ref` and the id keyword: `In draft-07 and earlier, a "$ref" at the top level makes the "{kw}" next to it ignored. Move the "$ref" into "allOf".` (Verified: the Python client ignores that id.)
   - Rule A: every nested object (reached through the walk of step 8) that has the id keyword, whatever its value: `Only the top level of a JSON schema may have "{kw}". Remove it from {paths}. A type that needs its own "{kw}" belongs in its own Schema, referenced with "$ref".` `{paths}` are up to three sorted JSON Pointers (RFC 6901) in single quotes, then `and {n} more`.
   - Rule B: the same for `$schema`: `Only the top level of a JSON schema may have "$schema". Remove it from {paths}. A JSON schema uses one draft.`
   - The errors come after the meta-schema error, in the order missing `$schema`, D, A, B. After a D, A or B error `external_refs` is None (stages D4 and D5 are skipped).
7. `expected_name` = `title`.
8. External references, derived the way the Python serializer resolves them. A wrong guess here is a false pass: the registry accepts the schema and the serializer fails with `Unresolvable`. With steps 5 and 6 there is one base, the root's.
   - `$ref`s: every string `$ref` found by walking the schema keywords iteratively (map keys under `properties`, `patternProperties`, `definitions`, `$defs`, `dependentSchemas` and `dependencies` are names, not keywords; the values of `enum`, `const`, `default` and `examples` are data and are not walked; lists are walked).
   - Base: `urldefrag(urljoin("", root_id)).url` for a string root id (the id keyword), otherwise `""`. A malformed root id or `$ref` gives `Invalid JSON schema: the {kw} '{v}' is not a valid URI`, with `external_refs` None.
   - Target: `urldefrag(urljoin(base, ref)).url`. It is internal when it is `""` or equals the base, otherwise external, and its row's `ref_string` must equal it exactly. `external_refs` = the set of external targets. Without a root id this is the `$ref` without its fragment. Internal refs (`#/definitions/...`, `#/$defs/...`) are allowed and ignored.
   - Verified: SR resolves an external `$ref` when the reference `name` equals the resolved value (`address.json#/definitions/Zip` works with name `address.json`; under a root `$id` of `https://acme.example/schemas/order.json` the name must be `https://acme.example/schemas/address.json`, because the Python serializer resolves `address.json` against the `$id` and fails with `Unresolvable`), and rejects the schema when no reference has that name. `jsonschema` does not check that `$ref` targets exist; SR does, in stage E.
9. A top-level `javaType` property is allowed (section 12). Verified: SR accepts it.

### 5.3 Protobuf

Parsing `.proto` text requires the protoc compiler shipped in `grpcio-tools`.

1. Create a temporary directory (always cleaned up).
2. Write the schema to `<tmp>/__main__.proto`.
3. For each reference, fetch the schema text from SR and write it to `<tmp>/<derived path>` (`com.acme.Address` goes to `<tmp>/com/acme/Address.proto`). Do this recursively for the referenced schemas' own references, using the `name` field of their SR references as the path.
   - **Security:** reject any path (derived, or read from an SR reference `name`) that is empty, absolute, has a `..` path segment, contains a backslash or a NUL character, or cannot be written (it collides with `__main__.proto` or another reference's directory), before writing: `Invalid import path '{x}'.`
4. Run protoc with a timeout (10 s) as a subprocess: `python -m grpc_tools.protoc -I <tmp> -I <grpc_tools/_proto> -I <schema_validator/_proto> --include_imports --descriptor_set_out=<tmp>/out.pb __main__.proto`.
   - `importlib.resources.files("grpc_tools") / "_proto"` provides `google/protobuf/*` (verified in grpcio-tools 1.85.1).
   - grpcio-tools ships nothing else. The `google/type/*` and `confluent/*` files in `builtin_proto_imports` must be bundled in the package under `schema_validator/_proto/`, copied from the SR 7.9.0 image (`proto-google-common-protos-2.22.1.jar` and `kafka-protobuf-types-7.9.0.jar` in `/usr/share/java/schema-registry`). Bundle only the listed files.
5. Decide by exit code only. protoc also writes warnings to stderr on success (unused import; `No edition or syntax specified`). Non-zero exit: `Invalid Protobuf schema: {stderr, with temp paths stripped and "__main__.proto:" removed}`. If every failure is a missing file that the schema itself imports and none of them is the path of a reference row, report nothing here (D4 names them). A missing file imported only by a referenced schema is reported as `Invalid Protobuf schema`. A protoc timeout gives `Invalid Protobuf schema: the Protobuf compiler did not finish in 10 seconds.` A non-zero exit with no error line (a crash or a signal) gives `Internal validator error: protoc exited with code {n} and no message`. Error lines look like `__main__.proto:3:30: Expected ";".` and `nope/x.proto: File not found.`

   protoc also limits nesting itself: 32 nested message definitions fail with `Reached maximum recursion limit for nested messages.` (31 compile), while the registry accepts 500. A chain of 5,000 separate messages compiles. So Protobuf gets no depth limit of its own; `test_protobuf_nesting_limit_is_protocs` pins protoc's.

   protoc is the real validity check for Protobuf. SR's own parser accepted duplicate field numbers and a proto3 enum that does not start at 0. The other way round, SR rejects `edition = "2023"`, which protoc accepts; stage E reports that.
6. Parse `out.pb` as `google.protobuf.descriptor_pb2.FileDescriptorSet`; take the entry for `__main__.proto`.
7. `package` must be non-empty: `Protobuf schema needs a package, for example "package com.team.domain;".`
8. Exactly one top-level message (`len(file.message_type) == 1`). Nested messages and top-level enums are fine. `A Protobuf schema must define exactly one top-level message (found {n}). Nest the others inside it or move them to their own Schema.`
   - Reason: the client derives the subject from whichever message it serializes, so several top-level messages make the name ambiguous. Verified with the Java serializer: a file with `Multi1` and `Multi2` looks up `pkg.Multi1` or `pkg.Multi2` depending on the message sent. SR itself accepts such files, and files with no package or no message.
9. `expected_name` = `package + "." + message_type[0].name`. The `java_package` and `java_outer_classname` options do not change it (verified).
10. External references: `file.dependency` minus the paths in `builtin_proto_imports`. Each remaining path must equal the derived path of a reference row (D4/D5).

When protoc fails, no name is derived (C3 is skipped). protoc's "File not found" for the schema's own import is not reported on top of the D4 error.

---

## 6. Reference handling

Reference row = `(schema_name, ref_string)`:

- `ref_string` is the SR reference `name`: the Avro full type name, the Protobuf import path, or the JSON `$ref` value without its fragment.
- `schema_name` is the SR reference `subject`.
- `version` is the latest version of that subject, looked up in SR.

Why the Protobuf import path is fixed. A Java Protobuf producer resolves every import of its message to a subject before it sends, even with `use.latest.version=true`, through `reference.subject.name.strategy`. The default strategy uses the import path itself as the subject, so `import "acme/address.proto"` fails with `Subject 'acme/address.proto' not found`. Confluent's `QualifiedReferenceSubjectNameStrategy` drops `.proto` and turns `/` into `.`, so `com/acme/Address.proto` resolves to `com.acme.Address`, which is the Schema's subject. Verified for a direct and a transitive import; the consumer needs no extra setting. Python producers do not do this lookup.

For AVRO, SR ignores the reference `name` entirely (any text works); the exact-match rule in D4/D5 is the validator's own, kept so the rows stay meaningful.

Algorithm:

1. Normalise rows (AVRO: a missing or empty `ref_string` defaults to `schema_name`; PROTOBUF: set `ref_string` to `schema_name.replace(".", "/") + ".proto"`).
2. For each row, `GET /subjects/{schema_name}/versions/latest`. 404 gives D1. Otherwise keep `version`, `schemaType` (absent means AVRO), `schema` text and its `references`.
3. D2 format check.
4. D3 cycle check: depth-first walk over the referenced schemas' own `references` (fetch each `subject`/`version` from SR, memoised per request, max depth 20). If `name` is reached, report the cycle.
5. D7, during the same walk: record every reference edge (also edges to a schema already walked) and every row. For each subject, the versions used must be one; for each reference name, the subjects must be one. Otherwise report `REF_VERSION_CONFLICT` / `REF_NAME_CONFLICT`, after the per-row errors, sorted by subject / name. Why: the registry accepts a schema that uses Money v2 directly and Money v1 through another reference, but Python and Java then resolve the one name differently and decode each other's data wrongly (audit F1).
6. D4/D5 cross-check: compare `external_refs` from stage B with the set of `ref_string` values. Both directions, exact string match.
7. Build `resolved = [{name: ref_string, subject: schema_name, version: n}]` for stage E and for the warning text.

The validator does not return `resolved` as data. America's registration step re-resolves the latest versions itself (accepted trade-off: a referenced schema could gain a version between validation and registration).

---

## 7. Schema Registry client

Thin wrapper; all calls use header `Content-Type: application/vnd.schemaregistry.v1+json`, basic auth, and the configured timeout. URL-encode subject names in paths.

Every request is sent with `allow_redirects=False` and `stream=True`. `sr_timeout_seconds` is a deadline for the whole response, counted from sending the request: the body is read with urllib3 2.x `response.raw.read1(65536, decode_content=True)` until it returns `b""`, which hands over whatever one underlying read gives, and the clock is checked after each call, so a registry that sends a byte every second cannot hold the call open (`iter_content(chunk_size=None)` waits for the whole non-chunked body, and any larger chunk size waits for a full chunk). Without `raw.read1` (urllib3 1.x) it falls back to `iter_content(chunk_size=1)`. The `timeout` passed to `requests` still bounds connect and each read. An overrun is E3, as is any 3xx (a redirect is never followed, so credentials are never sent to another host). A urllib3 or OS error while reading the body (a stall past the per-read timeout, a reset, a bad encoding) is E3 too. The deadline is also checked once when the headers are in, which covers an empty body. It does not interrupt a slow header read: a server that drips its status line and headers can hold a call for up to the per-read timeout per byte, because `requests` has no total-time limit for headers. The response is always closed. A rejection (`SchemaRegistryRejected`) carries only the body's `message` (or "") and its `error_code`, never the raw body, which may echo request headers.

| Purpose | Call |
|---|---|
| Latest version of a subject | `GET /subjects/{subject}/versions/latest` |
| Specific version | `GET /subjects/{subject}/versions/{version}` |
| Subject compatibility level | `GET /config/{subject}?defaultToGlobal=true` |
| Subject mode | `GET /mode/{subject}?defaultToGlobal=true` |
| Schema already registered under the subject (level change on edit) | `POST /subjects/{subject}`, same body as the compatibility request. 200 means yes; 404 with error_code 40401 (subject) or 40403 (schema) means no |
| Validity + compatibility | `POST /compatibility/subjects/{subject}/versions?verbose=true` |

Compatibility request body:

```json
{
  "schema": "<schema text>",
  "schemaType": "AVRO | JSON | PROTOBUF",
  "references": [{"name": "...", "subject": "...", "version": 3}]
}
```

Response handling (observed on SR 7.9.0; the endpoint behaves the same for a subject that does not exist yet, so it is a full validity check on create):

- `200` with `is_compatible: true`: pass.
- `200` with `is_compatible: false` and a message starting with `Invalid schema `: E1. The message has the form `Invalid schema {subject=...,schema=<whole text>,...} with refs [...] of type AVRO, details: <reason>`. Show only the text after the last `details: `. If there is no `details: ` part, or it is empty, show `Schema Registry rejected the schema without giving a reason.` instead, never the raw message. Examples of `<reason>`: `"strin" is not a defined name...`, `Could not parse Protobuf - Syntax error in :3:31: expected ';' but was '}'`, `No schema reference found for subject "x" and version 1`, `Invalid JSON Schema`.
- `200` with `is_compatible: false` otherwise: E2. `messages` is a list of strings, not JSON:
  ```
  {errorType:'READER_FIELD_MISSING_DEFAULT_VALUE', description:'The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema', additionalInfo:'zip'}
  {oldSchemaVersion: 1}
  {oldSchema: '<whole text>'}
  {validateFields: 'false', compatibility: 'BACKWARD'}
  ```
  Extract every `description` (take the text after the first `, description:` up to the last `}`, cut it at the last `, additionalInfo:`, then remove one leading and one trailing quote, `'` or `"`; they do not always match: JSON opens with `"` and closes with `'`), the `oldSchemaVersion` and the `compatibility` level. Drop `oldSchema`. A message without `errorType` (for example `Incompatible because of different schema type`) is shown as it is. If nothing can be extracted, fall back to the raw messages with any `{oldSchema: ...}` entry removed.
- `422` with `error_code` 42201, or any other 4xx with an `error_code`: E1, cleaned the same way. The text shown is, in order: the `details:` reason if the message has one; else the message itself if it is non-empty, has no `{`, and the `error_code` is one of Schema Registry's own (409, or five digits from 40000 to 49999); else `Schema Registry rejected the schema (error code {code}).` Any other code (for example a proxy's `400` with `error_code` 400) may carry a body that is not Schema Registry's, so its message is not shown. Not observed on this endpoint, but registration and lookup return 42201, so handle it. A 404 whose `error_code` is not 40401 or 40402 (a wrong URL prefix or a proxy) is E3 on every call, except the schema lookup `POST /subjects/{s}`, where 40401 and 40403 are the not-found codes (40402 is E3 there). A 4xx without an integer `error_code` is E3.
- `401` / `403`: E4.
- Timeouts, connection errors, 5xx, and a `200` whose body is not the expected JSON (an HTML page, a missing `is_compatible`): E3.

`GET /config/{subject}?defaultToGlobal=true` returns `200 {"compatibilityLevel": "..."}` for any subject, existing or not. Without the parameter it returns 404 (40408) when the subject has no level of its own.

`GET /mode/{subject}?defaultToGlobal=true` returns `200 {"mode": "..."}` for any subject, existing or not: the subject's own mode, else the global one. A body without a string `mode` is E3. A registration into a subject in `READONLY` mode is refused with 422 (error_code 42205), after the compatibility check has passed, which is why E5 exists.

`GET /subjects/{subject}/versions/latest` returns 404 (40401) for a subject that does not exist or is soft-deleted. The client returns "not registered" only for a 404 with `error_code` 40401 or 40402.

Verified against a 7.9.0 registry with HTTP Basic auth and the Confluent Security Plugin in ACL mode (`integration/tests/test_secured_sr.py`): missing or wrong credentials give 401 and a user without the ACL gives 403 (`error_code` 40301), on the first call the request makes; both are E4. The validator's user needs three operations on subject `*`:

| Operation | Needed by |
|---|---|
| `SUBJECT_READ` | `GET /subjects/{s}/versions/latest` and `/versions/{v}` (the reference lookups, and the latest version on a level change), and `POST /subjects/{s}` (is the schema registered). No fourth operation is needed |
| `SUBJECT_WRITE` | `POST /compatibility/subjects/{s}/versions`. The call stores nothing, but the registry asks for Write on the subject |
| `SUBJECT_COMPATIBILITY_READ` | `GET /config/{s}?defaultToGlobal=true` (on edit) and `GET /mode/{s}?defaultToGlobal=true` (every request that reaches stage E; the registry answers a user without it with 403 `denied operation ReadCompatibility`). No fourth operation is needed |

`SUBJECT_WRITE` also lets the same credentials register schemas, so they must be kept like the platform's own registration credentials. With these ACLs a subject that does not exist still gives 404 (40401), not 403.

Provide a `SchemaRegistryClient` protocol so tests can inject a fake.

---

## 8. Code layout

```
schema_validator/
  __init__.py          # exports validate, ValidationRequest, ReferenceInput, ValidationResult, ValidatorConfig, HttpSchemaRegistryClient, FormatAnalysis
  models.py            # dataclasses
  validator.py         # pipeline orchestration, message assembly
  rules.py             # stage A and stage C
  result.py            # errors and warnings to ValidationResult
  formats/
    avro.py            # parse, structure, expected name, external refs
    json_schema.py
    protobuf.py
  references.py        # stage D
  sr_client.py         # HTTP client + protocol
  sr_messages.py       # parsing SR verdict messages
  messages.py          # all user-facing message templates in one place
  _proto/              # bundled google/type/*.proto and confluent/*.proto (section 5.3), shipped as package data
integration/
  docker-compose.yml   # exists: Kafka + Schema Registry 7.9.0, no auth (SR on localhost:18081, Kafka on localhost:19092)
  tests/               # the integration tests (section 10a)
  java/                # the Java checks (probe.Main, probe.ProtoRefs)
tests/
  fake_sr.py           # in-memory SR fake
  schemas/             # good and bad sample schemas per format
  test_rules.py
  test_json_schema.py
  test_avro.py
  test_protobuf.py
  test_references.py
  test_sr_messages.py
  test_sr_client.py
  test_validate.py
  test_result.py
  test_fake_sr.py
README.md
```

Each format module exposes the same function:

```python
def analyse(schema_text: str, referenced: dict[str, str], config) -> FormatAnalysis
# FormatAnalysis: expected_name | None, external_refs: set[str] | None (None when the text could not be analysed; D4/D5 are then skipped), errors: list[str]
```

Type hints throughout, no global state, no network access outside `sr_client.py`.

---

## 9. Tests (pytest, SR faked)

Minimum cases per format:

- valid simple schema gives `Ok`
- invalid syntax
- missing namespace / package / title
- title without a dot, with a space, with `/`, `:` or `*`, starting with `:.ctx:` (JSON)
- JSON without `$schema` is checked as draft-07; `$schema` with and without the trailing `#`; unsupported draft
- top-level union (Avro), non-object top level (JSON), two top-level messages (Protobuf), zero messages (Protobuf)
- nested types allowed (nested record, nested message, `$defs`)
- Protobuf importing `google/protobuf/timestamp.proto`, `google/type/date.proto` and `confluent/meta.proto` with no reference row passes; `google/type/localized_text.proto` and `google/rpc/status.proto` need a row (D4)
- protoc warnings on stderr with exit code 0 (no `syntax` line, unused built-in import) do not produce an error
- Avro name with dots and a conflicting `namespace` (name wins)

Names:

- name differs from expected: message contains the expected name
- differs only by case: still an error
- duplicate by case-insensitive comparison
- rename on edit; fullname changed inside schema on edit (single combined message)

References:

- happy path per format gives `Warning` with pinned versions
- missing row (D4), unused row (D5), missing `ref_string` for JSON (D6)
- Protobuf: the import path is derived from the selected schema; a schema that imports the right type under another path (`acme/address.proto`) gets the message with the required import line; a `ref_string` sent for Protobuf is ignored
- JSON: `$ref` with a `#fragment` matches the row without it
- referenced schema not in SR (D1), different format (D2)
- direct and transitive cycle (D3)
- transitive reference (A uses B uses C) compiles for Protobuf and parses for Avro
- path traversal in a Protobuf `ref_string`

SR verdict:

- incompatible gives Error with the extracted descriptions, old version and level, and without the old schema text (one fixture per format, copied from section 7)
- `is_compatible: false` with an `Invalid schema ... details: X` message gives the E1 Error showing only `X`
- 42201 gives Error
- new subject: `is_compatible: true` passes
- 401 and 403 give the E4 Error
- timeout gives Error (fail closed)
- compatibility level changed on edit gives the extra warning

Result shape:

- several errors joined by newline in one message
- `Error` only with `valid=False`; `Warning`/`Ok` only with `valid=True`
- an exception inside a format module becomes an Error, never a raise

---

## 9a. Implementation order

Everything in this document ships as one release: all three formats, with references. Build it in this order, and have the tests for each step passing before starting the next:

1. Models, result assembly, message templates, the pipeline skeleton, stage A, and the fake SR for tests.
2. Stage C name rules (using a stub format analyser).
3. JSON Schema format module.
4. Avro format module.
5. Protobuf format module (without references first).
6. Stage D references: shared checks (D1, D2, D3, D5, D6), then the per-format external-reference detection and D4, then reference-aware parsing for Avro and Protobuf.
7. Stage E: SR client and verdict handling.
8. README: how to call `validate`, configuration, and the meaning of each error code.

9. Integration tests against a real Schema Registry in Docker (section 10a), including the full `validate` pipeline.

Unit tests always run against the fake SR. The Docker-based tests are a separate, required step.

---

## 10. What has been verified

Verified on 2026-11-16 against `confluentinc/cp-schema-registry:7.9.0` (no auth) on `apache/kafka:3.8.1`, with confluent-kafka-python 2.16.1 and the Confluent 7.9.0 Java serializers. The checks are in `integration/tests/` (Python) and `integration/java/` (Java). Rows 5a, 9, 10 and 11 were first checked by probe scripts and are now tests in `integration/tests/test_sr_behaviour.py`.

| # | Question | Result |
|---|---|---|
| 1 | Compatibility endpoint on a subject that does not exist | It parses the schema. Valid: `200 is_compatible: true`. Invalid: `200 is_compatible: false` with an `Invalid schema ... details:` message. Never 404 or 42201. Same for all three formats and for `/versions/latest`. So stage E is a full validity check on create |
| 2 | Subject looked up by real producers equals `expected_name` | Yes, Python and Java, all three formats, including an Avro `name` with dots and a conflicting `namespace`, and Protobuf with `java_package` |
| 3 | Confluent's "additional configuration" for RecordNameStrategy | Concerns only how Java deserializers pick the class (section 12). No effect on the validator |
| 4 | JSON `$ref` matching | Reference `name` = `$ref` value without the fragment. Verified end to end with Python and Java, including client-side payload validation through the reference |
| 5a | JSON Schema drafts | SR accepts any `$schema` value. The allowed list is policy |
| 5b | Built-in Protobuf imports | Exact list in section 3.1. SR silently ignores an unresolvable import that is not used, and fails with `Could not resolve type` when it is used |
| 6 | Library APIs | `fastavro.parse_schema(schema, named_schemas=dict)` and `importlib.resources.files("grpc_tools") / "_proto"` work as written. grpcio-tools bundles only `google/protobuf/*` |
| 7 | References end to end | Work for all three formats. SR does not enforce names for Avro or Protobuf, unused rows, or cycles (section 4, stage D) |
| 8 | Java Protobuf producers and imports | Need the import-path convention and `QualifiedReferenceSubjectNameStrategy` (section 6) |
| 9 | Subject names | SR accepts anything, case-sensitively (`X.Case` and `X.case` are two subjects). A name starting with `:.ctx:` lands in another context |
| 10 | Deleting | SR refuses to soft-delete a subject that others reference (422, error 42206). After a soft delete the subject's history is gone for compatibility: an incompatible schema registers as the next version |
| 11 | Reference version `-1` | SR accepts it at registration and stores the concrete latest version |
| 12 | 401/403 and the validator's ACLs | Checked against a registry with Basic auth and ACLs. E4 in every case; the user needs `SUBJECT_READ`, `SUBJECT_WRITE` and `SUBJECT_COMPATIBILITY_READ` on `*` (section 7) |

Confirmed from Confluent documentation:

- RecordNameStrategy subject: Avro record full name, Protobuf message name, JSON Schema `title`.
  https://docs.confluent.io/platform/current/schema-registry/fundamentals/serdes-develop/index.html
- A schema reference is a name, a subject and a version.
  https://docs.confluent.io/platform/current/schema-registry/develop/api.html

---

## 10a. Integration tests (Docker)

The questions below were first answered by probe scripts; the integration tests below repeat them, so a new Schema Registry version can be checked the same way.

Setup:

- `integration/docker-compose.yml` exists: one Kafka broker (`apache/kafka:3.8.1`) and one Schema Registry (`confluentinc/cp-schema-registry:7.9.0`), without authentication. Image names are variables (`KAFKA_IMAGE`, `SR_IMAGE`): the target environment is air-gapped, so images may have to come from an internal registry. If an image cannot be pulled, stop and report exactly which image is missing; do not substitute a different version silently.
- The client tests need `confluent-kafka[schemaregistry,avro,json,protobuf]` (test dependency only; without the extras the Protobuf serializer fails to import `google.type`).
- The Java checks live in `integration/java` and run in a `maven:3.9.9-eclipse-temurin-17` container on the compose network.
- Integration tests under `integration/tests/`, marked with a pytest marker (`@pytest.mark.integration`) so the unit suite stays runnable without Docker.

What each test asserts (the expected results are the ones recorded in section 10):

1. **Compatibility endpoint on a new subject.** For each format, a valid schema gives `200 is_compatible: true`; an invalid one gives `200 is_compatible: false` with a message starting `Invalid schema ` and containing `details: `.
2. **Name match with real clients.** For each format, register a schema under the validator's `expected_name`, then serialize and produce one message with a real Python producer (`record_subject_name_strategy`, `auto.register.schemas=False`, `use.latest.version=True`). Include an Avro schema whose `name` contains dots and a conflicting `namespace`. The Java equivalent is `probe.Main` in `integration/java`.
3. **References end to end.** For each format: register a referenced schema, register a schema that references it with the payload the validator implies, and produce with a real client. JSON: `$ref` with and without `#fragment`. Protobuf: import path `com/acme/Address.proto` for subject `com.acme.Address`; the Java producer with `QualifiedReferenceSubjectNameStrategy` succeeds and with the default strategy fails (`probe.ProtoRefs`).
4. **SR does not enforce the validator's own rules.** SR accepts: an unused reference row, a Protobuf reference name that differs from the import path, a Protobuf cycle, a JSON `title` with a space. If a future SR version starts rejecting these, the test fails and the plan is revisited.
5. **Built-in Protobuf imports.** Every path in `builtin_proto_imports` registers with no references when its type is used, and compiles locally with the bundled `_proto` directory; `google/type/localized_text.proto` and `google/rpc/status.proto` do not register.
6. **Validator against real SR.** Run the full `validate` pipeline against the container for the happy path and one failure per stage, using the real `sr_client` (no auth in the container; auth stays configurable).
7. **Message formats.** The incompatibility and invalid-schema messages SR returns still parse with the extraction rules in section 7, for each format.

Deliver `VERIFICATION.md` with one row per item: what was tested, the observed behaviour (status codes, messages), and what was changed in the code as a result. Where an observation contradicts this plan, the observation wins: fix the code and note the deviation.

---

## 11. Out of scope (America's side)

1. Schema deployment form: name, format, compatibility, schema text, reference rows (dropdown + text).
2. Calling the validator and displaying `valid`, `validationType`, `message`.
3. Registration: for each reference row, get the latest version from SR and register with `{name: ref_string, subject: schema_name, version}`. For PROTOBUF the `name` is not user input: it is `schema_name` with dots replaced by slashes, plus `.proto`. Surface SR errors to the user: an invalid schema gives 422 (42201) and an incompatible one gives 409, both with the long message format described in section 7. SR also accepts `version: -1` and stores the latest version itself, which would remove the lookup.
4. Blocking deletion of a Schema that other Schemas reference. SR enforces this too: the delete fails with 422 (42206), and `GET /subjects/{s}/versions/{v}/referencedby` lists the schema ids that reference it. Note that deleting a Schema and creating it again under the same name skips the compatibility check against the old versions.
5. Permissions: Services on a topic need read access to the topic's schema and everything it references, transitively. Re-evaluate when a reference is added to an existing schema.
6. Topic side: selecting the schema and whether it is the key or the value.

---

## 12. Client guidance to publish alongside (not code)

Because only the platform registers schemas, clients must not try to register and should fetch the registered schema:

- Java: `value.subject.name.strategy=io.confluent.kafka.serializers.subject.RecordNameStrategy`, `auto.register.schemas=false`, `use.latest.version=true`
- Java, Protobuf producers whose schema imports another Schema: also `reference.subject.name.strategy=io.confluent.kafka.serializers.subject.QualifiedReferenceSubjectNameStrategy`, and keep the imported file at the path used in the import (`com/acme/Address.proto`). Without the setting the first send fails with `Subject 'com/acme/Address.proto' not found`.
- Python: `'subject.name.strategy': record_subject_name_strategy`, `'auto.register.schemas': False`, `'use.latest.version': True`

Verified pitfalls on the producer side:

- **Java JSON Schema with a plain POJO.** The serializer generates its own schema from the class, and takes the subject from that schema's title: class `PlainOrder` looks up the subject `Plain Order`. Three setups work:
  - annotate the class with Confluent's `@Schema(value = "<the registered schema text>")`;
  - send a `JsonNode` wrapped with `JsonSchemaUtils.envelope(schema, payload)`;
  - annotate with `@JsonSchemaTitle("com.team.domain.Order")` and set `latest.compatibility.strict=false` (otherwise the generated schema is compared with the registered one and the send fails with `Incompatible schema`).
- **Python Avro with references.** Pass `Schema(schema_str, "AVRO", [SchemaReference(name, subject, version)])` to `AvroSerializer`. Plain text fails with `TypeError: argument of type 'NoneType' is not iterable`. Python JSON and Protobuf producers work with references without extra steps.
- **Java Avro with references.** Works when the client's own schema has the referenced record inlined.
- **Protobuf nested messages.** Only the top-level message can be produced. Sending a nested message on its own looks up `package.Outer.Inner`, which does not exist.
- **JSON Schema without a `title`.** The Python serializer fails with an unrelated-looking `AttributeError`.

Java consumers only, choosing the class to deserialize into (does not affect the validator):

- Protobuf: `specific.protobuf.value.type` names the class. Confluent documents that it does not work with RecordNameStrategy when the `.proto` has more than one message; the "exactly one top-level message" rule avoids that case. Without it the consumer gets a `DynamicMessage`.
- JSON Schema: Confluent documents that `json.value.type` does not work with RecordNameStrategy. The alternative is `type.property=javaType` on the deserializer plus a top-level `javaType` property in the schema holding the Java class name, restricted with `json.type.allowed.packages`. Otherwise the consumer gets a generic map or `JsonNode`. Consequence for the validator: a top-level `javaType` property in a JSON schema must be accepted, not flagged.
- Python consumers are unaffected: they pass the message type or a `from_dict` function in code.

Keep `use.latest.version` on: it makes the client use the registered schema instead of looking its own text up. In the one case tested without it (Python, Avro, same schema with different whitespace and key order) the lookup still succeeded, so the "schema not found" risk was not reproduced, but the setting is also what makes references work without extra lookups in Python.
