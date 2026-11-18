# schema-validator

A Python library that checks a Schema deployment (name, format, compatibility level, schema text and references) before the platform registers it in Confluent Schema Registry 7.9. It parses the schema locally for syntax, structure, naming and references, then asks Schema Registry whether the schema is valid and compatible with the versions already registered. It returns one result with a status and a message the user can act on, including the name the deployment must have.

## Install

Python 3.11 or later.

```bash
pip install .
```

The runtime dependencies are pinned in `pyproject.toml` to the versions the validator was verified with: fastavro 1.13.1, jsonschema 4.26.0 (with referencing 0.37.0), grpcio-tools 1.85.1, protobuf 7.36.2, requests 2.34.2 (with urllib3 2.8.0, whose `read1` the client uses). The verdicts depend on these libraries' exact behaviour, so upgrade one only after re-running the integration suite and the differential run. The Protobuf files that Schema Registry resolves without a reference (`google/type/*`, `confluent/*`) are bundled in the package under `schema_validator/_proto/`; `google/protobuf/*` comes with `grpcio-tools`.

## Usage

```python
from schema_validator import (
    HttpSchemaRegistryClient,
    ReferenceInput,
    ValidationRequest,
    ValidatorConfig,
    validate,
)

config = ValidatorConfig(
    sr_url="https://schema-registry.example.com",
    sr_username="schema-validator",
    sr_password="secret",
)
sr = HttpSchemaRegistryClient.from_config(config)

request = ValidationRequest(
    name="com.acme.Customer",
    schema_type="AVRO",
    schema="""{"type": "record", "name": "Customer", "namespace": "com.acme",
               "fields": [{"name": "address", "type": "com.acme.Address"}]}""",
    compatibility="BACKWARD",
    references=[ReferenceInput(schema_name="com.acme.Address", version=3)],
    previous_name=None,          # None on create; the current name on edit
    existing_names=["com.acme.Address", "com.acme.Order"],
)

result = validate(request, sr, config)
result.valid           # False only when validationType is "Error"
result.validationType  # "Ok", "Warning" or "Error"
result.message         # "" for Ok; otherwise the messages, one per line
```

`validate` never raises. An unexpected exception becomes a single `Error` with the message `Internal validator error: {err}`.

`sr` can be any object with the methods of the `SchemaRegistryClient` protocol in `schema_validator/sr_client.py` (`get_latest`, `get_version`, `get_compatibility_level`, `get_mode`, `check_compatibility`, `registered_version`). The unit tests use an in-memory fake (`tests/fake_sr.py`).

## Configuration

`ValidatorConfig` is a pydantic settings class: it is frozen, rejects unknown constructor arguments, and each field can also be set by the environment variable `SCHEMA_VALIDATOR_` plus the field name in capitals (for example `SCHEMA_VALIDATOR_MAX_REFERENCE_DEPTH`). Only constructor arguments are checked for unknown names: a mistyped variable, such as `SCHEMA_VALIDATOR_SR_TIMEOUT=5` for `SCHEMA_VALIDATOR_SR_TIMEOUT_SECONDS`, is silently ignored. The two collection fields are read from the environment as JSON lists, for example `SCHEMA_VALIDATOR_BUILTIN_PROTO_IMPORTS='["google/protobuf/timestamp.proto"]'`; a value that is not JSON raises `pydantic_settings.SettingsError` (a `ValueError`) at construction. Constructor arguments win over variables. A bad value (zero, negative, NaN, a non-number) raises `pydantic.ValidationError` at construction, which is the platform's startup error, not a validation message. Fields:

| Field (variable) | Default | Meaning |
|---|---|---|
| `sr_url` (`SCHEMA_VALIDATOR_SR_URL`) | `""` | Base URL of Schema Registry: `http://` or `https://` with a host without spaces, and a port, if given, that is a number from 0 to 65535. Anything else (the empty default, `http://host:abc` and `http://ho st:8081` included) makes building the client raise `SchemaRegistryConfigError` |
| `sr_username` (`SCHEMA_VALIDATOR_SR_USERNAME`) | `None` | Basic auth user; no authentication when `None` |
| `sr_password` (`SCHEMA_VALIDATOR_SR_PASSWORD`) | `None` | Basic auth password; not shown in `repr` |
| `sr_timeout_seconds` (`SCHEMA_VALIDATOR_SR_TIMEOUT_SECONDS`) | `10` | Time limit of each HTTP call as a whole, headers included, counted from sending the request: when it passes, the client shuts the call's socket down, or fails the call as soon as a connect still under way returns, and the call is E3, so a registry that sends its headers or body a byte at a time cannot hold a call longer. Not cut this way: a `session` passed to the constructor; a call through a proxy (`HTTP_PROXY` and the like); and the body of a response that closes the connection (`Connection: close`, HTTP/1.0, or no length; the test registry sends HTTP/1.1 keep-alive responses with a length). There the limit is checked after the headers arrive and after each piece of the body, so a stalled read can take up to one more per-read timeout (about 2 × `sr_timeout_seconds` at worst). A DNS lookup, TCP connect or TLS handshake is not interrupted either: a handshake that drips is bounded only by the per-read timeout of each socket operation (a DNS lookup only by the resolver), and the call is E3 as soon as the connect returns. `total_timeout_seconds` bounds the validation in every case |
| `total_timeout_seconds` (`SCHEMA_VALIDATOR_TOTAL_TIMEOUT_SECONDS`) | `30` | Time limit of the whole validation, all registry calls together. When it passes, the result is the E3 error and the unfinished work is dropped. Keep it above `sr_timeout_seconds` |
| `max_schema_bytes` (`SCHEMA_VALIDATOR_MAX_SCHEMA_BYTES`) | `500_000` | Largest schema text accepted, in UTF-8 bytes |
| `max_nesting_depth` (`SCHEMA_VALIDATOR_MAX_NESTING_DEPTH`) | `64` | Deepest nesting of JSON objects and arrays in an Avro or JSON schema, the root counting as 1 |
| `max_reference_depth` (`SCHEMA_VALIDATOR_MAX_REFERENCE_DEPTH`) | `20` | Deepest chain of references below a referenced row's own schema (level 0), at least 1 |
| `max_response_bytes` (`SCHEMA_VALIDATOR_MAX_RESPONSE_BYTES`) | `5_000_000` | Largest Schema Registry response body accepted, in bytes after decompression, at least 1. A larger body is E3; the client stops reading as soon as it passes the limit, so neither an endless body nor a gzip bomb is held in memory. The default holds a version body with a 900,000-character schema with room to spare |
| `protoc_timeout_seconds` (`SCHEMA_VALIDATOR_PROTOC_TIMEOUT_SECONDS`) | `10` | Time limit of one protoc run |
| `python_executable` (`SCHEMA_VALIDATOR_PYTHON_EXECUTABLE`) | `sys.executable` | The Python interpreter that runs protoc (`python -m grpc_tools.protoc`) |
| `allowed_json_drafts` (`SCHEMA_VALIDATOR_ALLOWED_JSON_DRAFTS`) | draft-04, draft-06, draft-07, 2019-09, 2020-12 | Accepted `$schema` values, stored without a trailing `#` (`DEFAULT_JSON_DRAFTS` in `models.py`). This is platform policy: Schema Registry accepts any `$schema` value |
| `builtin_proto_imports` (`SCHEMA_VALIDATOR_BUILTIN_PROTO_IMPORTS`) | 29 paths (`DEFAULT_BUILTIN_PROTO_IMPORTS` in `models.py`) | Import paths that need no reference row: `google/protobuf/` `any`, `api`, `descriptor`, `duration`, `empty`, `field_mask`, `source_context`, `struct`, `timestamp`, `type`, `wrappers`; `google/type/` `calendar_period`, `color`, `date`, `datetime`, `dayofweek`, `decimal`, `expr`, `fraction`, `interval`, `latlng`, `money`, `month`, `phone_number`, `postal_address`, `quaternion`, `timeofday` (each `.proto`); `confluent/meta.proto`, `confluent/type/decimal.proto` |

`python_executable` must be a real Python interpreter. Under uWSGI, `sys.executable` is the uWSGI binary, not Python, so set `python_executable` (or `SCHEMA_VALIDATOR_PYTHON_EXECUTABLE`) to the interpreter of the virtualenv; otherwise every Protobuf schema fails with `Internal validator error`.

On a registry with ACLs (Confluent Security Plugin), the validator's user needs `SUBJECT_READ`, `SUBJECT_WRITE` and `SUBJECT_COMPATIBILITY_READ` on subject `*`. `SUBJECT_READ` also covers the lookup `POST /subjects/{subject}` that tells which version holds the schema (every edit). `SUBJECT_WRITE` is for the compatibility check: it stores nothing, but the registry asks for Write. `SUBJECT_COMPATIBILITY_READ` is also what the mode lookup (`GET /mode/{subject}`) needs, so there is no fourth operation. The same credentials can therefore register schemas, so protect them accordingly.

```bash
sr-acl-cli --config <sr.properties> --add -s '*' -p schema-validator -o SUBJECT_READ -o SUBJECT_WRITE -o SUBJECT_COMPATIBILITY_READ
```

The `HttpSchemaRegistryClient` takes its URL, credentials, timeout and response limit from the config through `from_config`, or directly: `HttpSchemaRegistryClient(url, username=None, password=None, timeout_seconds=10, session=None, max_response_bytes=5_000_000)`. The constructor (and so `from_config`) raises `SchemaRegistryConfigError`, a `ValueError`, for a value no call could work with:

| Value | Text |
|---|---|
| `url` is not `http://` or `https://` with a host, or its host has a space, or its port is not a number from 0 to 65535 | `Schema Registry URL must be http:// or https:// with a host` |
| `timeout_seconds` is not a finite number above 0 | `Schema Registry timeout must be a number of seconds above 0` |
| `max_response_bytes` is not a whole number of at least 1 | `Schema Registry response limit must be a whole number of bytes, at least 1` |
| The username or password is not Latin-1 text (the encoding of HTTP Basic auth in `requests`) | `Schema Registry username and password must be Latin-1 text` |

These are startup errors for the platform, not validation messages, so the validator never sees a call fail for them. Leave `session` out: the client's own session is what cuts a call at `timeout_seconds`, and a session passed in gets no such watchdog.

## The request

`ValidationRequest` fields:

| Field | Meaning |
|---|---|
| `name` | The deployment name the user typed. It is the Schema Registry subject, so it must equal the name derived from the schema (see C3) |
| `schema_type` | `"AVRO"`, `"JSON"` or `"PROTOBUF"` |
| `schema` | The schema text |
| `compatibility` | The level chosen on the deployment: `BACKWARD`, `BACKWARD_TRANSITIVE`, `FORWARD`, `FORWARD_TRANSITIVE`, `FULL`, `FULL_TRANSITIVE` or `NONE` |
| `references` | A list of `ReferenceInput`, one per Schema this schema uses; may be empty |
| `previous_name` | `None` on create; the deployment's current name on edit |
| `existing_names` | The names of all other Schema deployments, excluding this one (compared lowercased) |

`ReferenceInput` fields:

| Field | Meaning |
|---|---|
| `schema_name` | The Schema deployment selected as the reference (its Schema Registry subject) |
| `ref_string` | How the reference is written in the schema text. **JSON: required**, the `$ref` value without its `#fragment` (an empty string counts as missing). Under a top-level `$id` it is the absolute URI the `$ref` resolves to, as the Python serializer resolves it (a nested `$id` is not allowed). **AVRO: required**, and it must be `schema_name` (the referenced record's full name); an empty text or any other text is rejected. A schema that the referenced record itself references is covered by the same row. A type defined inside the referenced record (for example a nested enum) cannot be used from another schema; give it its own Schema. **PROTOBUF: ignored**, the import path is always derived from `schema_name` (see below) |

| `version` | The version of the referenced Schema, picked by the user (a dropdown of the subject's versions). Required, an integer from 1 up; there is no "latest" choice. The validator checks and uses exactly this version, and the platform registers with exactly this version (no lookup of the latest). `None` exists only so that A5 can report a missing version |

The validator uses the version of each row for every check and says in a warning when a newer version exists. A version the subject does not have (or one that was soft-deleted) is an error.

The name each format derives (Confluent's RecordNameStrategy): Avro uses the record's full name (a `name` that contains dots is the full name and `namespace` is ignored), JSON Schema uses `title`, Protobuf uses `package` plus the one top-level message.

## The checks

All errors are collected; a stage is skipped only when it depends on one that failed. Stage A1 or A2 failing stops at once; A5 failing skips stage D. If the text cannot be analysed (Avro and JSON: not JSON, or the wrong top level; Protobuf: protoc fails), stage B reports the error and C3, D4, D5 and E are skipped. Exception: when protoc fails only because files the schema itself imports are missing, stage B reports nothing and D4/D5 run on the import statements found in the text. A local validity error (`Invalid Avro schema`, `Invalid JSON schema`) or a naming error does not skip C3, D4 or D5; it skips E. Stage E runs only when A to D found no error. Placeholders in braces are filled in.

| Code | Checks | Message |
|---|---|---|
| A1 | `schema_type` is AVRO, JSON or PROTOBUF | `Unsupported schema format '{x}'. Use AVRO, JSON or PROTOBUF.` |
| A2 | The schema is valid text, is not empty or whitespace only, and its UTF-8 size is within `max_schema_bytes` | `Schema contains characters that are not valid text.` / `Schema is empty.` / `Schema is too large ({n} bytes, limit {max}).` |
| A3 | `compatibility` is a known level | `Unknown compatibility level '{x}'.` |
| A4 | `name` is not empty, has no whitespace and does not start with `:` | `Schema name is invalid.` |
| A5 | No two rows share a `schema_name` or a `ref_string`; no row references `name`; every row has a `version` (an integer from 1 up); an Avro row's `ref_string` equals its `schema_name` (empty is an error) | `Reference '{x}' is listed twice.` / `A schema cannot reference itself.` / `Select a version for reference '{s}'.` / `For an Avro reference to '{s}', write '{s}' as the reference text.` |
| B, all | Avro and JSON text parses as JSON | `Schema is not valid JSON: {err}` |
| B, Avro and JSON | The parsed text is nested no deeper than `max_nesting_depth` (also when it is so deep that the parser overflows) | `Schema is nested more than {max} levels deep, counting every JSON object and array.` |
| B, Avro | The top level is one record, enum or fixed | `The top level of an Avro schema must be a single record, enum or fixed.` |
| B, Avro | The full name has a namespace, and no dot-separated part of it is empty (`.Order`, `com..Order`) | `Avro schema needs a namespace, for example "namespace": "com.team.domain".` |
| B, Avro | Every named type nested in the schema has a namespace: none sets `"namespace"` to `""`, `null` or anything that is not a dotted name (a nested type without a `namespace` inherits the schema's) | `Type '{name}' needs a namespace. Remove its "namespace" entry so it uses the schema's namespace.` |
| B, Avro | No nested type name has an empty part around a dot (`.Inner`, `Inner.`, `a..B`) | `Type name '{name}' has an empty part around a dot. Use a plain name, for example "Address", or a full name, for example "com.team.domain.Address".` |
| B, Avro | The full name of the schema and of every nested named type (record, enum, fixed, error) is a valid Avro name: each dotted part starts with a letter (A-Z, a-z) or an underscore and has only letters, digits and underscores | `'{name}' is not a valid Avro name. Each part between dots must start with a letter (A-Z, a-z) or an underscore and contain only letters, digits and underscores.` |
| B, Avro | No type is used that is defined inside a referenced schema (the registry accepts it, but neither the Python nor the Java serializer can produce with such a schema) | `Type '{name}' is defined inside the Schema '{schema}'. A type nested in another Schema cannot be used here. Move it to its own Schema and reference that.` |
| B, Avro | fastavro parses the schema, with its references. A type must be defined before its first use, and a type name is written as a plain string, not as `{"type": "<name>"}` | `Invalid Avro schema: {err}` (fastavro's text, with the Python values in three shapes written as JSON: a value in `<…>`, a trailing `: {…}` or `: […]`, and a leading value before ` is not ` or ` is too `, for example `Default value null must match schema type: string`; other Python values stay as Python writes them; at most 1,000 characters, cut with `…`) / `Invalid Avro schema: an entry has the wrong kind of value, for example an object where a type name is expected` (any error fastavro raises that is not its own schema error) |
| B, Avro | When fastavro accepts the schema: every field default in the schema's own records (an `error` type counts as a record) is a value of the field's type, as Java Avro checks it (fastavro checks only primitive types). A union's default is a value of its first type; a record default has every field that has no default of its own; an `int` or `long` is in range; `float` and `double` also take `"NaN"`, `"Infinity"` and `"-Infinity"`. Also, where Java does not check: an enum default is one of its symbols, a fixed default has exactly `size` characters, and a bytes or fixed default has only characters U+0000 to U+00FF. One message per field; `{record}` is the record's full name, `{first}` and `{type}` a primitive's name, a named type's full name, `array` or `map` (a logical type is its underlying type) | `The default of field '{field}' in '{record}' must be a value of the first type in its union ({first}). Put the type of the default first in the union.` / `The default of field '{field}' in '{record}' is not a valid {type} value.` |
| B, Avro | When fastavro accepts the schema: every enum the schema's own text defines has at least one symbol (a field of an enum with no symbols can hold no value, so Java and Python producers both fail) | `The enum '{name}' has no symbols, so a field of it can hold no value and producers cannot send it. Add at least one symbol to its "symbols".` (one per enum, sorted by full name) |
| B, JSON | The top level is an object with `"type": "object"` | `The top level of a JSON schema must have "type": "object".` |
| B, JSON | `title` is present | `JSON schema needs a "title"; it becomes the schema name.` |
| B, JSON | `title` matches `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$` | `The "title" must be a dotted name made of letters, digits and underscores, for example "com.team.domain.Order".` |
| B, JSON | `$schema` is present | `JSON schema needs a "$schema", for example "$schema": "http://json-schema.org/draft-07/schema#".` |
| B, JSON | `$schema` is in `allowed_json_drafts` | `Unsupported JSON Schema draft '{x}'.` |
| B, JSON | The schema passes its draft's meta-schema (draft-07 when `$schema` is absent) | `Invalid JSON schema: {err}` (the first line of the error, with the Python values in the same three shapes written as JSON, for example `true is not of type 'array'`; others stay as Python writes them, for example `['a', 'a'] has non-unique elements` or `"x" is not one of ['a', 'b']`; then ` (at /properties/x/required)`, the JSON Pointer of the keyword; at most 1,000 characters: the text is cut with `…` before the ` (at …)` part, so the location stays, unless the location alone is that long) |
| B, JSON | The schema is not nested so deeply that the meta-schema check overflows the stack | `JSON schema is nested too deeply.` |
| B, JSON | In draft-04, -06 and -07 the top level does not have both `$ref` and `$id` (`id` in draft-04) | `In draft-07 and earlier, a "$ref" at the top level makes the "{kw}" next to it ignored. Move the "$ref" into "allOf".` |
| B, JSON | In draft-04, -06 and -07 a top-level `$id` (`id` in draft-04) does not have both a part before `#` and a fragment after it (`#foo` alone is allowed) | `The top-level "{kw}" cannot have a fragment. Remove the "#..." part from '{value}'.` |
| B, JSON | No nested object has `$id` (`id` in draft-04); `{paths}` are up to 3 JSON Pointers, then `and {n} more` | `Only the top level of a JSON schema may have "{kw}". Remove it from {paths}. A type that needs its own "{kw}" belongs in its own Schema, referenced with "$ref".` |
| B, JSON | No nested object has `$schema` | `Only the top level of a JSON schema may have "$schema". Remove it from {paths}. A JSON schema uses one draft.` |
| B, JSON | In draft-04, -06 and -07, and with no `$schema` (which Schema Registry reads as draft-07), no `$ref` to a place in this schema is written with the schema's own `$id` (`id` in draft-04), as `https://acme.example/r8.json#/definitions/s` or `r8.json#/definitions/s` (Schema Registry rejects it; the bare `https://acme.example/r8.json` with no `#` is accepted). Under 2019-09 and 2020-12 Schema Registry accepts such a `$ref`, so its part after `#` gets the check of the next row instead | `A "$ref" to a place in this schema must start with "#", for example "#/definitions/Item". Schema Registry rejects one written with the schema's own "{kw}". Change it at {paths}.` |
| B, JSON | Every `$ref` of the form `#/...` resolves in the schema, read as the registry reads it: split on `/` first, then each token has `+` as a space, is percent-decoded, and has `~1` as `/` and `~0` as `~`; empty tokens at the end are ignored; a list is entered by an index of ASCII digits, leading zeros allowed (`#/allOf/01` is index 1). A `$ref` inside `enum`, `const`, `default` or `examples` is data and is not checked. An anchor such as `#foo` and a fragment with an odd percent escape or bytes that are not UTF-8 are not read this way (not pinned), only by the next row | `A "$ref" points to nothing in this schema. Check the part after "#" at {paths}.` |
| B, JSON | Every `$ref` to a place in this schema that the previous row did not flag is also followed the way the Python client follows it (`referencing`, with the root as a resource of its draft), and reaches the same place as in the registry's reading. The registry accepts, and Python's `JSONSerializer` fails on or reads differently: `#/definitions/a+b` for a key `"a b"`, a trailing `/` (`#/definitions/A/`), `#/definitions/` with a `""` key (Python reads `definitions[""]`, the registry the whole `definitions`), `%2F` inside a token (Python decodes before it splits), and an anchor that is not defined (`#nope`, which the registry reads as the root). A `$ref` with a fragment into a referenced schema (`address.json#nope`) must be found there by the Python client too, and a pointer must reach the same place there in both readings (`address.json#/definitions/` with a `""` key differs); it is skipped when the referenced text is missing (D4) or not JSON | `The Python client cannot follow a "$ref", or follows it to a different place than Schema Registry. Write it as a plain pointer, for example "#/definitions/Item", with no "+", trailing "/" or anchor that is not defined, at {paths}.` |
| B, JSON | Warning only: a `format` keyword with a string value in a schema position is listed (added only when there is no error) | `This schema uses "format" at {paths}. Java producers can reject values that do not match it, while Python producers do not check it, so the two can accept different data.` |
| B, Protobuf | Every reference's import path (derived, or read from Schema Registry) is not empty, is relative, has no `..` path segment, contains no backslash or NUL character, and can be written (it does not collide with `__main__.proto` or another reference's directory) | `Invalid import path '{x}'.` |
| B, Protobuf | protoc compiles the schema with its references. The main file's name `__main__.proto` is shown as `your schema` | `Invalid Protobuf schema: {err}` / `Invalid Protobuf schema: the Protobuf compiler did not finish in {seconds:g} seconds.` |
| B, Protobuf | protoc reports more than 5 errors | `Invalid Protobuf schema: ` plus the first 5 errors, then `and {n} more` (the part after `Invalid Protobuf schema: ` is at most 1,000 characters) |
| B, Protobuf | `package` is set | `Protobuf schema needs a package, for example "package com.team.domain;".` |
| B, Protobuf | Exactly one top-level message | `A Protobuf schema must define exactly one top-level message (found {n}). Nest the others inside it or move them to their own Schema.` |
| B, Protobuf | No proto2 `group` field, in a message, a nested message or an `extend` (the registry's parser drops group fields, so a Java producer cannot set one and a Java consumer loses its data) | `'{message}' has a group '{group}'. Schema Registry drops group fields, so Java producers cannot set it and Java consumers lose its data. Use a nested message and a field of that type instead.` |
| C1 | On edit, `name` equals `previous_name` | `A schema's name cannot change after creation (was '{previous_name}'). Create a new Schema instead.` |
| C2 | `name` is not in `existing_names`, ignoring case (on an edit, the schema's own stored name is not counted) | `A Schema named '{name}' already exists (names are compared case-insensitively).` |
| C3 | `name` equals the name derived from the schema, case-sensitively | `The name must match the schema. It should be '{expected_name}'.` |
| C1 + C3 | On edit, the name derived from the schema changed (replaces C1 and C3) | `The schema's full name changed from '{previous_name}' to '{expected_name}'. A schema's name cannot change; revert the namespace/name or create a new Schema.` |
| D1 | Each referenced subject is registered | `Referenced schema '{s}' is not registered in Schema Registry yet.` |
| D1 (version) | The row's version is registered | `'{s}' has no version {v} (the latest is {latest}).` |
| D2 | Each referenced subject has the same format | `'{s}' is a schema in {their_type} format. A schema in {my_type} format can only reference schemas in {my_type} format.` |
| D3 | Following references from each referenced subject never reaches `name`, within `max_reference_depth` levels (20 by default) | `Reference cycle: '{s}' already depends on '{name}'.` / `References of '{s}' are nested more than {max} levels deep.` |
| D4 | Every external name the schema uses has a row | `Your schema uses '{x}' but no reference is selected for it.` |
| D5 | Every row is used in the schema | `'{x}' is selected as a reference but is not used in the schema.` (Protobuf: `'{s}' is selected as a reference but the schema does not import it. Write the import as: import "{path}";`) |
| D6 | JSON: every row has a non-empty `ref_string` | `Reference to '{s}' needs the text used in the schema (the $ref value).` |
| D7 | One version per subject and one Schema per reference name, across the whole graph (the rows and everything they reference) | `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Update the Schemas that use an older version to v{latest}: select v{latest} for '{s}' in their references and save them. Then save this schema.` (when one of the versions is this schema's own row and it is the highest: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Select the same version for '{s}' as the Schemas it comes through, or update those Schemas to v{chosen}: select v{chosen} for '{s}' in their references and save them.` / when it is not the highest: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Select v{latest} for '{s}', the version the Schemas it comes through use.`) / `The reference name '{x}' is used for more than one Schema: {subjects}. Each Schema needs its own reference name.` |
| E1 | Schema Registry accepts the schema as valid | `Schema Registry rejected the schema: {reason}` / `Schema Registry rejected the schema without giving a reason.` (an `Invalid schema` message with no reason) / `Schema Registry rejected the schema (error code {code}).` (any registry text that is not an `Invalid schema` reason or a known not-found text) |
| E2 | Schema Registry finds it compatible with the registered versions | `Not {sr_level}-compatible with version {n}: {descriptions}` (when the registry names no version: `Not {sr_level}-compatible with the existing versions: {descriptions}`) |
| E3 | Schema Registry is reachable (timeouts, connection errors, 5xx, unreadable responses, a response that is not finished within `sr_timeout_seconds`, a response larger than `max_response_bytes`, a redirect, a 404 that is not Schema Registry's "subject or version not found", a 4xx without an error_code, a validation still running at the `total_timeout_seconds` deadline) | `Could not reach Schema Registry to validate. Try again.` |
| E4 | Schema Registry accepts the validator's credentials (no 401 or 403) | `The validator is not allowed to query Schema Registry. Contact the platform team.` |
| E5 | The subject accepts new versions (its mode, or the global mode when it has none, is `READWRITE`) | `Schema Registry does not accept new versions of '{name}' right now (mode {mode}). Contact the platform team.` |
| E7 | On an edit, the format equals the latest version's (the platform never sends a format change; this guards against a platform bug) | `'{name}' is registered as {registered}. Its format cannot change: to use {requested}, create a new Schema.` |
| E8 | On an edit, the text (with the rows' references) is not identical to a version other than the latest: the registry would register nothing, and producers would keep using the latest | `This schema is identical to version {n} of '{name}', and the latest is version {latest}. Schema Registry would not register it again, so producers would keep using version {latest}. Change the schema so that it differs from every registered version.` |
| E6 | On an edit whose `compatibility` differs from the subject's current level, the schema is unchanged: the schema, looked up with the rows' references, is the latest version, and the latest version's references are the same reference names, subjects and versions as the rows. A row that selects a newer version of a reference is a schema change, even with the same text. A subject without versions is not checked | `The compatibility level and the schema cannot change in the same save. Schema Registry checks a new version under the level in force when it is registered (now '{sr_level}'). Save the new level '{compatibility}' with the schema and its reference versions unchanged first, then change the schema.`; and when the reference rows hold the same triples as the latest version but in another order: `The references are listed in a different order than in the registered version ({order}). Keep that order, so that only the compatibility level changes.` (no compatibility call) |

E1 shows only the reason Schema Registry gives (the text after `details: `), and E2 only the extracted descriptions, because the registry's raw messages contain the whole schema text. When an `Invalid schema` message has no reason, E1 shows the fixed text above, never the raw message. Apart from that reason, E1 shows a registry text only when it is `Subject '<name>' not found.` with `error_code` 40401 or `Version <n> not found.` with 40402. Any other rejection (no message, free text, a structure, a code that does not go with the text; a gateway or plugin may send any of these) shows `Schema Registry rejected the schema (error code {code}).` and nothing else of the body. E3 and E4 also apply to the reference lookups of stage D.

In draft-04, -06 and -07 (and with no `$schema`), a schema should reference its own definitions with `#/...` (for example `#/definitions/x`). The registry rejects a self-reference written with the document's own URI (for example `https://acme.example/o.json#/definitions/x`); drafts 2019-09 and 2020-12 accept it, and the validator then checks the part after `#` as it checks a `#/...` reference.

Warnings, returned as `Warning` when there is no error:

| When | Message |
|---|---|
| The top level is an Avro enum or fixed (`{kind}` is `enum` or `fixed`) | `This Schema is an Avro {kind}. A producer cannot send it as a message, because under RecordNameStrategy a message must be a record. Use it as a reference from a record Schema.` |
| The schema has references | `References will be pinned to: {list}.` when every reference is at its latest version, otherwise `References will be pinned to: {list}. To update one, select a newer version for it.` (`{list}` is for example `com.acme.Address v3, com.acme.Money v1 (latest is v2)`) |
| The schema is JSON and uses `format` with a string value in a schema position (not inside `enum`, `const`, `default`, `examples` or `example`, and not a property name); a `format` key inside an unknown keyword (`"x-doc": {"format": "email"}`) also warns, as a `$ref` there is a use (F10); `{paths}` are up to 3 JSON Pointers, then `and {n} more` | `This schema uses "format" at {paths}. Java producers can reject values that do not match it, while Python producers do not check it, so the two can accept different data.` |
| On edit, the level is the same and the schema is identical to the latest version | `This is already version {n} of '{name}'. Saving it changes nothing in Schema Registry.` |
| On edit, `compatibility` differs from the subject's current level in Schema Registry, and the schema is unchanged, or the subject has no versions | `Compatibility was checked using the current level '{sr_level}', not the new level '{compatibility}'.` |

## Protobuf import paths

A Protobuf schema must import another Schema at a fixed path: the referenced Schema's name with dots replaced by slashes, plus `.proto`. The user selects the Schema; the path is derived, and `ref_string` is ignored. For a reference to the Schema `com.acme.Address`:

```proto
syntax = "proto3";
package com.acme;
import "com/acme/Address.proto";
message Customer { Address address = 1; }
```

An import at any other path gets the D5 message with the import line to write. The fixed path lets Java producers resolve the import to the right subject with `QualifiedReferenceSubjectNameStrategy`.

## Tests

Unit tests (no network, no Docker; Schema Registry is faked):

```bash
.venv/bin/python -m pytest -q
```

Coverage of the unit tests (line and branch; `coverage` is not a dependency, install it first with `uv pip install --python .venv/bin/python coverage`):

```bash
.venv/bin/python -m coverage run --branch --source=schema_validator -m pytest -q && .venv/bin/python -m coverage report -m
```

Integration tests against a real Schema Registry 7.9.0 and Kafka in Docker. They need `confluent-kafka[schemaregistry,avro,json,protobuf]` (a test dependency only). Start the stack, then run the tests:

```bash
docker compose -p validator-it -f integration/docker-compose.yml up -d   # SR on localhost:18081, secured SR on localhost:18082, Kafka on localhost:19092
.venv/bin/python -m pytest integration/tests -m integration -q
```

`SR_URL` and `KAFKA_BOOTSTRAP` override the addresses. The tests are skipped when Schema Registry is not reachable. Each test uses its own namespace, so they can be rerun against the same registry. The images are variables (`KAFKA_IMAGE`, `SR_IMAGE`) for environments that pull from an internal registry.

`integration/tests/test_secured_sr.py` runs against the second registry of the stack, `schema-registry-secured` (HTTP Basic auth and ACLs; users in `integration/secured/users.properties`, ACLs in its start command). `SECURED_SR_URL` overrides its address, and the file is skipped when it is not reachable.

`integration/differential.py` compares the validator with the registry on generated schemas: it runs `validate`, registers the same text and reports every disagreement. Its exit code is 1 when a schema passed `validate` and the registry refused it, or when the validator failed internally. Use the throwaway stack only; it registers every case.

```bash
.venv/bin/python integration/differential.py --cases 700 --seed 2 --out /tmp/differential.tsv
```

The Java checks run in a Maven container on the same network. `probe.Defaults` parses the cases of `tests/schemas/avro_defaults.json` (also used by the unit tests) with Java Avro and needs neither the registry nor Kafka; the validator directory is mounted at `/repo` for it:

```bash
cd integration/java
docker run --rm --network validator-it_default -v "$PWD:/work" -v "$PWD/../..:/repo" -v "$HOME/.m2:/root/.m2" -w /work \
  maven:3.9.9-eclipse-temurin-17 sh -c 'mvn -q -B package dependency:build-classpath -Dmdep.outputFile=target/cp.txt \
  && java -cp target/classes:$(cat target/cp.txt) probe.Main && java -cp target/classes:$(cat target/cp.txt) probe.ProtoRefs \
  && java -cp target/classes:$(cat target/cp.txt) probe.Nested \
  && java -cp target/classes:$(cat target/cp.txt) probe.Pinned \
  && java -cp target/classes:$(cat target/cp.txt) probe.Enums \
  && java -cp target/classes:$(cat target/cp.txt) probe.Defaults /repo/tests/schemas/avro_defaults.json'
```

The results of the last run are in [VERIFICATION.md](VERIFICATION.md).

## Known behaviours

These were found by the second audit (F10, F11) and its final review. They are documented, not changed. The evidence is in `/Users/noamram/validator-audit-2/REPORT.md` (F10, F11), outside this repository.

- **A `$ref` inside an unknown keyword is a use (F10).** `"x-doc": {"$ref": "elsewhere.json"}` counts like any other `$ref`: an external one needs a reference row, and an internal one must resolve (stage B, JSON rules 10 and 11). The registry and the clients ignore unknown keywords, so this is a false rejection by design. Move the annotation out of the schema.
- **Looser than the owner rules (F11).** The clients work in each case.
  - A root `$id` of `#foo` passes (the fragment rule needs a non-empty part before `#`).
  - An Avro type that comes only through another reference, and a Protobuf type from an `import public`, needs no reference row of its own.
  - A Protobuf import that is never used still counts as a use of its row.
- **Avro names are stricter than the registry and the clients.** The name rule (`AVRO_NAME_CHARS`) allows only A-Z, a-z, digits and underscores in each dotted part. The registry and the clients accept more, such as non-ASCII letters (`Ordér`) and parts like `a-b` (both checked on the registry, which accepts them). The rule follows the Avro specification on purpose.

## Not done by the validator

- The platform's own steps (the deployment form, showing the result, registration with the resolved reference versions, blocking deletes, permissions, the topic side) are listed in [section 11 of the plan](schema-validator-plan.md#11-out-of-scope-americas-side).
- The settings producers and consumers need (RecordNameStrategy, `auto.register.schemas=false`, `use.latest.version=true`, which Protobuf producers must set (without it, a Protobuf schema with a map, a nested type or a well-known type fails with `Schema not found`; details in the plan), and `QualifiedReferenceSubjectNameStrategy` for Java Protobuf producers that import another Schema) are in [section 12 of the plan](schema-validator-plan.md#12-client-guidance-to-publish-alongside-not-code).
