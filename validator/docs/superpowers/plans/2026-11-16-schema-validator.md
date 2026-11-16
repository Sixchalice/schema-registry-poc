# Schema Deployment Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python 3.11 library, `schema_validator`, whose `validate()` tells an America user whether a Schema deployment (Avro, JSON Schema or Protobuf) can be registered in Schema Registry 7.9, and what to change if not.

**Architecture:** One pure pipeline (`validator.py`) runs five stages and collects every error: input checks, per-format analysis, name rules, reference rules, Schema Registry verdict. Each format has one module with the same `analyse()` function. All network access goes through one `SchemaRegistryClient` protocol, so unit tests use an in-memory fake and never need Docker.

**Tech Stack:** Python 3.11, fastavro 1.13.1, jsonschema 4.26.0, grpcio-tools 1.85.1 (protoc), protobuf 7.36.2, requests 2.34.2, pytest. Integration tests: Docker (`apache/kafka:3.8.1`, `confluentinc/cp-schema-registry:7.9.0`), confluent-kafka 2.16.1.

**Spec:** `validator/schema-validator-plan.md`. Read it before starting any task. Section numbers below ("spec §5.1") refer to it.

## Global Constraints

- The project root is `validator/` inside the `acl-schema-registry` repository. Every path in this plan is relative to `validator/`, and every command runs from `validator/`.
- Python is `.venv/bin/python` (3.11, already created, all libraries installed). Run tests with `.venv/bin/python -m pytest`.
- Runtime dependencies are only: standard library, `fastavro`, `jsonschema`, `grpcio-tools`, `protobuf`, `requests`. `confluent-kafka` is allowed in `integration/` only.
- No network access outside `schema_validator/sr_client.py`. No global mutable state. Type hints on every function.
- Unit tests (`tests/`) never touch the network or Docker. They may run protoc through `grpc_tools` (local subprocess).
- Every user-facing message is a constant in `schema_validator/messages.py`, copied verbatim from the spec. No message text is written inline in other modules.
- `validate` never raises.
- Git: the repository has unrelated uncommitted work. Stage only the files a task names (`git add <paths>`); never `git add -A` or `git add .`. Do not push.
- Do not stop, remove or reconfigure the Docker containers `validator-it-kafka-1` and `validator-it-schema-registry-1`, or any `sr-acl-poc-*` container.

## Review Focus

Inputs the spec implies but does not spell out. Each has a test in the task named.

1. An Avro record that refers to itself or to a type defined later in the same text (`["null", "Order"]` inside `Order`) is not an external reference. Task 4.
2. A JSON schema where `$ref` is a property *name* (`"properties": {"$ref": {"type": "string"}}`) or whose `$ref` value is not a string must not be read as a reference. Task 3.
3. A Protobuf schema with an `import "x.proto";` line inside a comment, while a real import is missing, must report only the real one. Task 5.
4. Schema Registry answering `200` with a body that is not the expected JSON (an HTML page from a proxy, or JSON without `is_compatible`) is "unreachable" (E3), not a crash. Task 7.
5. A diamond of references (the schema uses B and C, both of which use D) is not a cycle, and D is fetched once. Task 8.

## Task order

- Task 1 first.
- Tasks 2 to 8 are independent of each other (each owns its own files) and can run in parallel after Task 1.
- Task 9 needs Tasks 1 to 8. Task 10 needs Task 9.

## File map

| File | Responsibility | Task |
|---|---|---|
| `pyproject.toml`, `.gitignore` | Packaging, pytest config | 1 |
| `schema_validator/models.py` | All dataclasses, `proto_import_path` | 1 |
| `schema_validator/messages.py` | Every message template | 1 |
| `schema_validator/result.py` | Errors and warnings to `ValidationResult` | 1 |
| `schema_validator/sr_client.py` | SR types, exceptions, protocol (Task 1); HTTP client (Task 7) | 1, 7 |
| `tests/fake_sr.py` | In-memory `SchemaRegistryClient` | 1 |
| `schema_validator/rules.py` | Stage A and stage C | 2 |
| `schema_validator/formats/json_schema.py` | JSON Schema analysis | 3 |
| `schema_validator/formats/avro.py` | Avro analysis | 4 |
| `schema_validator/formats/protobuf.py`, `schema_validator/_proto/` | Protobuf analysis, bundled `.proto` files | 5 |
| `schema_validator/sr_messages.py` | Parsing SR's verdict messages | 6 |
| `schema_validator/references.py` | Stage D | 8 |
| `schema_validator/validator.py`, `schema_validator/formats/__init__.py`, `schema_validator/__init__.py` | Pipeline, format dispatch, public exports | 9 |
| `integration/tests/`, `README.md`, `VERIFICATION.md` | Tests against the real registry, docs | 10 |

---

### Task 1: Foundation (models, messages, result, SR protocol, fake SR)

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `schema_validator/__init__.py`, `schema_validator/models.py`, `schema_validator/messages.py`, `schema_validator/result.py`, `schema_validator/sr_client.py`, `schema_validator/formats/__init__.py` (empty), `tests/__init__.py` (empty), `tests/fake_sr.py`
- Test: `tests/test_result.py`, `tests/test_fake_sr.py`

**Interfaces:**
- Consumes: nothing.
- Produces (every later task relies on these exact names):

```python
# models.py
@dataclass(frozen=True)
class ReferenceInput:      schema_name: str; ref_string: str | None = None
@dataclass(frozen=True)
class ValidationRequest:   name: str; schema_type: str; schema: str; compatibility: str
                           references: list[ReferenceInput]; previous_name: str | None; existing_names: list[str]
@dataclass(frozen=True)
class ValidationResult:    valid: bool; validationType: str; message: str
@dataclass(frozen=True)
class ValidatorConfig:     sr_url: str = ""; sr_username: str | None = None; sr_password: str | None = None
                           sr_timeout_seconds: float = 10; max_schema_bytes: int = 500_000
                           allowed_json_drafts: tuple[str, ...] = DEFAULT_JSON_DRAFTS
                           builtin_proto_imports: frozenset[str] = DEFAULT_BUILTIN_PROTO_IMPORTS
@dataclass(frozen=True)
class FormatAnalysis:      expected_name: str | None; external_refs: set[str] | None; errors: list[str]
                           # external_refs is None when the text could not be analysed (stage D4/D5 are skipped)

SCHEMA_TYPES = ("AVRO", "JSON", "PROTOBUF")
COMPATIBILITY_LEVELS = ("BACKWARD", "BACKWARD_TRANSITIVE", "FORWARD", "FORWARD_TRANSITIVE", "FULL", "FULL_TRANSITIVE", "NONE")
def proto_import_path(schema_name: str) -> str   # "com.acme.Address" -> "com/acme/Address.proto"

# result.py
def build_result(errors: list[str], warnings: list[str]) -> ValidationResult

# sr_client.py
@dataclass(frozen=True)
class SrReference:           name: str; subject: str; version: int
@dataclass(frozen=True)
class SchemaVersion:         subject: str; version: int; schema_type: str; schema: str; references: tuple[SrReference, ...] = ()
@dataclass(frozen=True)
class CompatibilityVerdict:  is_compatible: bool; messages: tuple[str, ...] = ()
class SchemaRegistryError(Exception)
class SchemaRegistryUnavailable(SchemaRegistryError)     # timeout, connection error, 5xx, unreadable body -> E3
class SchemaRegistryUnauthorized(SchemaRegistryError)    # 401, 403 -> E4
class SchemaRegistryRejected(SchemaRegistryError)        # other 4xx with error_code; str(e) is SR's message -> E1
class SchemaRegistryClient(Protocol):
    def get_latest(self, subject: str) -> SchemaVersion | None                    # None on 404
    def get_version(self, subject: str, version: int) -> SchemaVersion | None     # None on 404
    def get_compatibility_level(self, subject: str) -> str
    def check_compatibility(self, subject: str, schema: str, schema_type: str,
                            references: Sequence[SrReference]) -> CompatibilityVerdict

# tests/fake_sr.py
class FakeSchemaRegistry:   # implements SchemaRegistryClient
    def add(self, subject: str, schema: str, schema_type: str = "AVRO",
            references: Sequence[SrReference] = ()) -> int      # returns the new version, starting at 1
    levels: dict[str, str]            # per-subject level; get_compatibility_level falls back to "BACKWARD"
    verdict: CompatibilityVerdict     # returned by check_compatibility; default CompatibilityVerdict(True)
    fail_with: Exception | None       # when set, every method raises it
    calls: list[tuple]                # ("get_latest", subject), ("get_version", subject, version), ("check_compatibility", subject, schema, schema_type, tuple(references)), ("get_compatibility_level", subject)
```

`DEFAULT_JSON_DRAFTS` holds these five URIs, stored without a trailing `#`: `http://json-schema.org/draft-04/schema`, `http://json-schema.org/draft-06/schema`, `http://json-schema.org/draft-07/schema`, `https://json-schema.org/draft/2019-09/schema`, `https://json-schema.org/draft/2020-12/schema`.

`DEFAULT_BUILTIN_PROTO_IMPORTS` holds the 29 paths listed in spec §3.1 (`google/protobuf/any.proto`, ..., `confluent/type/decimal.proto`).

`messages.py` constants. Copy each text verbatim from the spec location given; placeholders stay as `{named}` fields for `str.format`.

| Constant | Spec | Constant | Spec |
|---|---|---|---|
| `UNSUPPORTED_FORMAT` | §4 A1 | `REF_NOT_REGISTERED` | §4 D1 |
| `SCHEMA_EMPTY`, `SCHEMA_TOO_LARGE` | §4 A2 | `REF_WRONG_FORMAT` | §4 D2 |
| `UNKNOWN_COMPATIBILITY` | §4 A3 | `REF_CYCLE` | §4 D3 |
| `NAME_INVALID` | §4 A4 | `REF_MISSING_ROW` | §4 D4 |
| `REFERENCE_DUPLICATE`, `SELF_REFERENCE` | §4 A5 | `REF_UNUSED`, `REF_UNUSED_PROTOBUF` | §4 D5 |
| `NAME_CHANGED` | §4 C1 | `REF_STRING_REQUIRED` | §4 D6 |
| `NAME_EXISTS` | §4 C2 | `PINNED_WARNING` | §4 stage D, last paragraph; field `{list}` |
| `NAME_MISMATCH` | §4 C3 | `SR_REJECTED`, `SR_UNREACHABLE`, `SR_UNAUTHORIZED` | §4 E1, E3, E4 |
| `FULLNAME_CHANGED` | §4 stage C, last paragraph | `SR_INCOMPATIBLE` | §4 E2 |
| `AVRO_TOP_LEVEL`, `AVRO_NAMESPACE`, `AVRO_INVALID` | §5.1 steps 2, 4, 7 | `LEVEL_WARNING` | §4 stage E |
| `JSON_TITLE_MISSING`, `JSON_TITLE_FORMAT`, `JSON_TOP_LEVEL`, `JSON_DRAFT`, `JSON_INVALID` | §5.2 steps 2 to 6 | `PROTO_IMPORT_PATH`, `PROTO_INVALID`, `PROTO_PACKAGE`, `PROTO_MESSAGE_COUNT` | §5.3 steps 3, 5, 7, 8 |

Texts the spec does not give:

```python
NOT_JSON = "Schema is not valid JSON: {err}"                      # Avro (spec §5.1 step 1) and JSON Schema
SR_INCOMPATIBLE_NO_VERSION = "Not {sr_level}-compatible with the existing versions: {descriptions}"
REF_TOO_DEEP = "References of '{s}' are nested more than 20 levels deep."
PROTO_TIMEOUT = "Invalid Protobuf schema: the Protobuf compiler did not finish in 10 seconds."
INTERNAL_ERROR = "Internal validator error: {err}"
```

- [ ] **Step 1: Write `pyproject.toml` and `.gitignore`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "schema-validator"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fastavro", "jsonschema", "grpcio-tools", "protobuf", "requests"]

[tool.setuptools.packages.find]
include = ["schema_validator*"]

[tool.setuptools.package-data]
schema_validator = ["_proto/**/*.proto"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: needs the Docker stack in integration/"]
```

`.gitignore`: `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `integration/probes/java/target/`.

- [ ] **Step 2: Write the failing tests**

`tests/test_result.py`:

```python
def test_no_messages_is_ok():
    assert build_result([], []) == ValidationResult(True, "Ok", "")
def test_warnings_only():
    assert build_result([], ["w1", "w2"]) == ValidationResult(True, "Warning", "w1\nw2")
def test_errors_win_and_hide_warnings():
    assert build_result(["e1", "e2"], ["w1"]) == ValidationResult(False, "Error", "e1\ne2")
def test_duplicate_errors_are_reported_once():
    assert build_result(["e1", "e1"], []).message == "e1"
def test_proto_import_path():
    assert proto_import_path("com.acme.Address") == "com/acme/Address.proto"
```

`tests/test_fake_sr.py`: `add` twice on one subject returns 1 then 2; `get_latest` returns version 2 with the given `schema_type` and `references`; `get_latest("missing")` and `get_version("s", 9)` return `None`; `get_compatibility_level` returns `"BACKWARD"` unless `levels[subject]` is set; `check_compatibility` returns `verdict` and records the call in `calls`; with `fail_with = SchemaRegistryUnavailable("x")` every method raises it.

- [ ] **Step 3: Run the tests and see them fail**

Run: `.venv/bin/python -m pytest tests/test_result.py tests/test_fake_sr.py -q`
Expected: collection errors (`ModuleNotFoundError: schema_validator`).

- [ ] **Step 4: Implement the files listed under Interfaces**

`schema_validator/__init__.py` exports the five `models` dataclasses for now (Task 9 adds `validate`).

- [ ] **Step 5: Install the package and run the tests**

Run: `uv pip install --python .venv/bin/python -e . && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore schema_validator tests schema-validator-plan.md docs integration/docker-compose.yml integration/probes
git commit -m "validator: models, messages, result assembly, SR protocol and fake"
```

---

### Task 2: Stage A and stage C rules

**Files:**
- Create: `schema_validator/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `ValidationRequest`, `ValidatorConfig`, `SCHEMA_TYPES`, `COMPATIBILITY_LEVELS`, `messages`.
- Produces:

```python
@dataclass(frozen=True)
class InputCheck:
    errors: list[str]
    fatal: bool            # A1 or A2 failed: the pipeline returns immediately
    references_ok: bool    # False when A5 failed: the pipeline skips stage D

def check_input(request: ValidationRequest, config: ValidatorConfig) -> InputCheck      # spec §4 stage A
def check_name(request: ValidationRequest, expected_name: str | None) -> list[str]      # spec §4 stage C
```

Rules the spec leaves open:
- A2: "empty" means empty or whitespace only. Size is `len(schema.encode("utf-8"))`.
- A4: invalid when empty, contains any whitespace character, or starts with `:`.
- A5: one `REFERENCE_DUPLICATE` per repeated value (`x` is the repeated `schema_name` or `ref_string`); rows with `ref_string=None` are not compared on `ref_string`.
- Stage C with `expected_name=None`: C3 and the combined message are skipped; C1 and C2 still run.
- Stage C on edit when `expected_name != previous_name`: return only `FULLNAME_CHANGED` (plus C2 if it fires). Otherwise C1 and C3 are reported independently.

- [ ] **Step 1: Write the failing tests** (`tests/test_rules.py`; build requests with a local `req(**overrides)` helper whose defaults are a valid AVRO create named `com.acme.Order`)

| Test | Input | Expected |
|---|---|---|
| `test_valid_input` | defaults | `InputCheck([], False, True)` |
| `test_unsupported_format` | `schema_type="XML"` | errors `["Unsupported schema format 'XML'. Use AVRO, JSON or PROTOBUF."]`, `fatal=True` |
| `test_empty_schema` | `schema="  \n"` | `["Schema is empty."]`, `fatal=True` |
| `test_schema_too_large` | `schema="é"*6`, `max_schema_bytes=10` | `["Schema is too large (12 bytes, limit 10)."]`, `fatal=True` |
| `test_unknown_compatibility` | `compatibility="STRICT"` | `["Unknown compatibility level 'STRICT'."]`, `fatal=False` |
| `test_invalid_names` | `name` in `""`, `"a b"`, `"a\tb"`, `":.ctx:a.B"` | `["Schema name is invalid."]` each |
| `test_duplicate_reference_subject` | two rows `schema_name="a.B"` | `["Reference 'a.B' is listed twice."]`, `references_ok=False` |
| `test_duplicate_ref_string` | rows `("a.B","x.json")`, `("a.C","x.json")` | `["Reference 'x.json' is listed twice."]` |
| `test_self_reference` | row `schema_name == name` | `["A schema cannot reference itself."]`, `references_ok=False` |
| `test_collects_all_non_fatal` | bad compatibility + bad name | both messages, in A3, A4 order |
| `test_name_matches` | `check_name(req(), "com.acme.Order")` | `[]` |
| `test_name_mismatch` | name `com.acme.Orders` | `["The name must match the schema. It should be 'com.acme.Order'."]` |
| `test_name_differs_only_by_case` | name `com.acme.order` | same C3 message |
| `test_duplicate_case_insensitive` | `existing_names=["COM.ACME.ORDER"]` | `["A Schema named 'com.acme.Order' already exists (names are compared case-insensitively)."]` |
| `test_rename_on_edit` | `previous_name="com.acme.Old"`, name `com.acme.New`, expected `com.acme.Old` | two messages: C1 `A schema's name cannot change after creation (was 'com.acme.Old'). Create a new Schema instead.` then C3 `The name must match the schema. It should be 'com.acme.Old'.` |
| `test_fullname_changed_on_edit` | `previous_name=name="com.acme.Order"`, expected `com.acme.Order2` | exactly `["The schema's full name changed from 'com.acme.Order' to 'com.acme.Order2'. A schema's name cannot change; revert the namespace/name or create a new Schema."]` |
| `test_no_expected_name` | `expected_name=None`, create | `[]` |

- [ ] **Step 2: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_rules.py -q`. Expected: `ModuleNotFoundError: schema_validator.rules`.
- [ ] **Step 3: Implement `check_input` and `check_name` in `schema_validator/rules.py`.**
- [ ] **Step 4: Run and see them pass.** Same command. Expected: all pass.
- [ ] **Step 5: Commit**

```bash
git add schema_validator/rules.py tests/test_rules.py
git commit -m "validator: input sanity and name rules"
```

---

### Task 3: JSON Schema format module

**Files:**
- Create: `schema_validator/formats/json_schema.py`
- Test: `tests/test_json_schema.py`

**Interfaces:**
- Consumes: `FormatAnalysis`, `ValidatorConfig`, `messages`.
- Produces: `def analyse(schema_text: str, referenced: dict[str, str], config: ValidatorConfig) -> FormatAnalysis` (`referenced` is unused for JSON).

Implements spec §5.2 steps 1 to 9. Decisions:
- Not JSON: `NOT_JSON`, `external_refs=None`. JSON but not an object: `JSON_TOP_LEVEL`, `external_refs=None`.
- Steps 2 to 6 all run and all report (they do not stop at the first failure). `expected_name` is the title only when steps 2 and 3 pass.
- Step 5 compares `$schema` with a trailing `#` removed against `config.allowed_json_drafts`. When it fails, step 6 is skipped.
- Step 6 uses `validator_for(schema, default=Draft7Validator)`; `{err}` is the first line of `SchemaError.message`.
- Step 8: a `$ref` counts only when it is a key of a schema object whose value is a string. Walk dict values and list items, but when walking the value of `properties`, `patternProperties`, `definitions`, `$defs` or `dependentSchemas`, treat its keys as names (not keywords) and descend into the values. Do not descend into `enum`, `const`, `default` or `examples`.

- [ ] **Step 1: Write the failing tests** (`cfg = ValidatorConfig()`; `run(d)` = `analyse(json.dumps(d), {}, cfg)`)

| Test | Schema | Expected |
|---|---|---|
| `test_valid` | `{"title":"com.acme.Order","type":"object","properties":{"id":{"type":"string"}}}` | `expected_name="com.acme.Order"`, `external_refs=set()`, `errors=[]` |
| `test_not_json` | text `{not json` | one error starting `Schema is not valid JSON: `, `external_refs is None` |
| `test_top_level_array` | `[1]` | `['The top level of a JSON schema must have "type": "object".']`, `external_refs is None` |
| `test_missing_title` | no `title` | `['JSON schema needs a "title"; it becomes the schema name.']`, `expected_name is None` |
| `test_bad_titles` | title in `"Order"`, `"com.acme.My Order"`, `"com/acme.Order"`, `"com.acme:Order"`, `"com.*.Order"`, `":.ctx:com.acme.Order"`, `"com..Order"`, `"1com.acme.Order"` | each: `['The "title" must be a dotted name made of letters, digits and underscores, for example "com.team.domain.Order".']`, `expected_name is None` |
| `test_type_not_object` | `"type":"array"` | `JSON_TOP_LEVEL` text |
| `test_unsupported_draft` | `"$schema":"http://json-schema.org/draft-03/schema#"` | `["Unsupported JSON Schema draft 'http://json-schema.org/draft-03/schema#'."]` |
| `test_draft_with_and_without_hash` | draft-07 with `#` and without | no errors both |
| `test_all_allowed_drafts` | each of the five defaults | no errors |
| `test_invalid_schema` | `"properties":"nope"` | one error starting `Invalid JSON schema: ` and containing `'nope' is not of type 'object'` |
| `test_no_schema_keyword_uses_draft7` | `{"title":"a.B","type":"object","properties":{"x":{"type":"array","items":[{"type":"string"}]}}}` (array-form `items`: valid in draft-07, invalid in 2020-12) | no errors; the same schema with `"$schema":"https://json-schema.org/draft/2020-12/schema"` gives one error starting `Invalid JSON schema: ` |
| `test_internal_refs_ignored` | `$ref` values `#/definitions/Z`, `#/$defs/Z`, `#` | `external_refs=set()` |
| `test_external_refs` | `$ref` values `address.json`, `address.json#/definitions/Zip`, `https://x.io/money.json#` | `{"address.json", "https://x.io/money.json"}` |
| `test_ref_in_nested_positions` | `$ref` under `items`, `allOf[0]`, `definitions.X.properties.y` | all collected |
| `test_property_named_ref` (Review Focus 2) | `"properties":{"$ref":{"type":"string"}}` | `external_refs=set()`, no errors |
| `test_ref_inside_enum_or_default_ignored` | `"default":{"$ref":"x.json"}`, `"enum":[{"$ref":"y.json"}]` | `external_refs=set()` |
| `test_javatype_allowed` | top-level `"javaType":"com.acme.Order"` | no errors |
| `test_several_errors` | no title and `"type":"array"` | both messages |

- [ ] **Step 2: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_json_schema.py -q`.
- [ ] **Step 3: Implement `analyse` in `schema_validator/formats/json_schema.py`.**
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: Commit**

```bash
git add schema_validator/formats/json_schema.py tests/test_json_schema.py
git commit -m "validator: JSON Schema analysis"
```

---

### Task 4: Avro format module

**Files:**
- Create: `schema_validator/formats/avro.py`
- Test: `tests/test_avro.py`

**Interfaces:**
- Consumes: `FormatAnalysis`, `ValidatorConfig`, `messages`.
- Produces: `def analyse(schema_text: str, referenced: dict[str, str], config: ValidatorConfig) -> FormatAnalysis`. `referenced` maps a reference name to the referenced schema's text, already in dependency order (a schema comes after everything it references).

Implements spec §5.1 steps 1 to 7. Decisions:
- Not JSON: `NOT_JSON`, `external_refs=None`. Not a record object: `AVRO_TOP_LEVEL`, `external_refs=None`.
- No namespace: `AVRO_NAMESPACE`, `expected_name=None`; external references are still computed and step 7 still runs.
- Step 6, defined names: a named type's full name is its `name` if that contains a dot, else `namespace + "." + name` using its own `namespace` or the nearest enclosing one. Collect every definition in the whole text first, then resolve uses, so order does not matter.
- Step 6, used names: every string in a type position (field `type`, array `items`, map `values`, union members, the `type` of a `{"type": ...}` object) that is not one of `null boolean int long float double bytes string record enum fixed array map`. A used name without a dot resolves to `enclosing namespace + "." + name` when there is an enclosing namespace.
- Step 7: build one `named: dict = {}`; for each text in `referenced` (in order) call `fastavro.parse_schema(json.loads(text), named_schemas=named)`, ignoring failures; then parse the schema itself with the same dict. `fastavro.schema.UnknownType` is not reported (stage D explains a missing reference); note it is not a subclass of `SchemaParseException` in fastavro 1.13.1, so catch it separately and first. `fastavro.schema.SchemaParseException` and any other exception fastavro raises become `AVRO_INVALID` with `{err}` = `str(exception)`.

- [ ] **Step 1: Write the failing tests** (`run(d, referenced={})` = `analyse(json.dumps(d), referenced, ValidatorConfig())`; `rec(name, fields, **kw)` builds a record dict)

| Test | Schema | Expected |
|---|---|---|
| `test_valid` | record `Order`, namespace `com.acme`, field `id: string` | `("com.acme.Order", set(), [])` |
| `test_not_json` | text `{not json` | error starts `Schema is not valid JSON: `, `external_refs is None` |
| `test_top_level_union`, `_enum`, `_primitive`, `_array` | `["null", {...record}]`; an enum object; `"string"`; `{"type":"array","items":"string"}` | each `["The top level of an Avro schema must be a single record."]`, `external_refs is None` |
| `test_no_namespace` | record `Order`, no namespace | `['Avro schema needs a namespace, for example "namespace": "com.team.domain".']`, `expected_name is None` |
| `test_dotted_name_wins` | name `com.real.Order`, namespace `ignored.ns` | `expected_name="com.real.Order"` |
| `test_nested_record_is_not_external` | field `addr` of inline record `Address` (no namespace), second field of type `Address`, third of type `com.acme.Address` | `external_refs=set()` |
| `test_external_qualified` | field type `com.other.Address` | `{"com.other.Address"}` |
| `test_external_unqualified_takes_namespace` | namespace `com.acme`, field type `Address` | `{"com.acme.Address"}` |
| `test_self_reference` (Review Focus 1) | `Order` with field `parent: ["null", "Order"]` | `external_refs=set()`, no errors |
| `test_forward_use` | field 1 of type `Later`, field 2 defines inline enum `Later` | `external_refs=set()` |
| `test_refs_in_array_map_union` | `{"type":"array","items":"a.X"}`, `{"type":"map","values":"a.Y"}`, `["null","a.Z"]` | `{"a.X","a.Y","a.Z"}` |
| `test_nested_namespace` | inline record `Inner` with `"namespace":"other"` whose field uses `Thing` | `{"other.Thing"}` |
| `test_logical_type_and_inline_primitive_object` | `{"type":"long","logicalType":"timestamp-millis"}`, `{"type":"string"}` | `external_refs=set()`, no errors |
| `test_bad_default` | field `a: string` with `"default": 5` | one error starting `Invalid Avro schema: ` |
| `test_missing_reference_not_reported_here` | field type `com.acme.Address`, `referenced={}` | `errors=[]`, `external_refs={"com.acme.Address"}` |
| `test_with_reference` | same schema, `referenced={"com.acme.Address": <Address text>}` | `errors=[]` |
| `test_transitive_reference` | schema uses `a.B`; `referenced` = `{"a.C": C, "a.B": B}` where B uses `a.C` | `errors=[]` |
| `test_field_without_type` | field `{"name":"a"}` | one error starting `Invalid Avro schema: ` |

- [ ] **Step 2: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_avro.py -q`.
- [ ] **Step 3: Implement `analyse` in `schema_validator/formats/avro.py`.**
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: Commit**

```bash
git add schema_validator/formats/avro.py tests/test_avro.py
git commit -m "validator: Avro analysis"
```

---

### Task 5: Protobuf format module and bundled .proto files

**Files:**
- Create: `schema_validator/formats/protobuf.py`, `schema_validator/_proto/google/type/*.proto` (16 files), `schema_validator/_proto/confluent/meta.proto`, `schema_validator/_proto/confluent/type/decimal.proto`
- Test: `tests/test_protobuf.py`

**Interfaces:**
- Consumes: `FormatAnalysis`, `ValidatorConfig` (`builtin_proto_imports`), `messages`.
- Produces: `def analyse(schema_text: str, referenced: dict[str, str], config: ValidatorConfig) -> FormatAnalysis`. `referenced` maps an import path to the file's text (direct and transitive references).

Implements spec §5.3 steps 1 to 10. Decisions:
- Run protoc as `[sys.executable, "-m", "grpc_tools.protoc", "-I", tmp, "-I", grpc_proto_dir, "-I", bundled_proto_dir, "--include_imports", f"--descriptor_set_out={tmp}/out.pb", "__main__.proto"]` with `timeout=10`, in a `tempfile.TemporaryDirectory`. `grpc_proto_dir = importlib.resources.files("grpc_tools") / "_proto"`; `bundled_proto_dir = importlib.resources.files("schema_validator") / "_proto"`. Timeout: `PROTO_TIMEOUT`, `external_refs=None`.
- Before writing, every key of `referenced` must be a safe path: not absolute, no `..` segment, no backslash, not empty. Otherwise `PROTO_IMPORT_PATH`, nothing is written, `external_refs=None`.
- Exit code 0 decides success; stderr is ignored on success.
- On failure, `expected_name=None` and:
  - `imports` = the paths in `import "x";` / `import public "x";` / `import weak "x";` statements, found by regex after removing `//` and `/* */` comments;
  - `external_refs = imports - config.builtin_proto_imports`;
  - `missing` = the paths in stderr lines of the form `<path>: File not found.`;
  - if `missing` is not empty and none of them is a key of `referenced`: `errors=[]` (stage D4 names the missing import);
  - otherwise `errors=[PROTO_INVALID]`, with `{stderr}` = the stderr lines joined by `"; "`, after dropping lines that contain `warning:` or start with `WARNING:` / `W0000`, and removing the prefix `__main__.proto:` and the temp directory path.
- On success: step 7 (`PROTO_PACKAGE`) and step 8 (`PROTO_MESSAGE_COUNT`, `{n}` = the count) are both checked; `expected_name` is set only when both pass; `external_refs = set(file.dependency) - config.builtin_proto_imports`.

- [ ] **Step 1: Copy the bundled .proto files from the Schema Registry image**

```bash
cid=$(docker create confluentinc/cp-schema-registry:7.9.0)
tmp=$(mktemp -d)
docker cp "$cid:/usr/share/java/schema-registry/proto-google-common-protos-2.22.1.jar" "$tmp/"
docker cp "$cid:/usr/share/java/schema-registry/kafka-protobuf-types-7.9.0.jar" "$tmp/"
docker rm "$cid"
mkdir -p schema_validator/_proto
for n in calendar_period color date datetime dayofweek decimal expr fraction interval latlng money month phone_number postal_address quaternion timeofday; do
  unzip -o -q "$tmp/proto-google-common-protos-2.22.1.jar" "google/type/$n.proto" -d schema_validator/_proto
done
unzip -o -q "$tmp/kafka-protobuf-types-7.9.0.jar" "confluent/meta.proto" "confluent/type/decimal.proto" -d schema_validator/_proto
find schema_validator/_proto -name '*.proto' | wc -l
```

Expected: `18`. The image is already on this machine; no download.

- [ ] **Step 2: Write the failing tests** (`P = 'syntax = "proto3";\npackage com.acme;\n'`; `run(text, referenced={})` = `analyse(text, referenced, ValidatorConfig())`)

| Test | Schema text | Expected |
|---|---|---|
| `test_valid` | `P + 'message Order { string id = 1; }'` | `("com.acme.Order", set(), [])` |
| `test_syntax_error` | `P + 'message Order { strin id = 1 }'` | `expected_name is None`; one error starting `Invalid Protobuf schema: ` that contains `Expected ";"` and contains neither `__main__` nor the temp directory |
| `test_no_package` | no `package` line | `['Protobuf schema needs a package, for example "package com.team.domain;".']`, `expected_name is None` |
| `test_two_messages` | messages `A` and `B` | `["A Protobuf schema must define exactly one top-level message (found 2). Nest the others inside it or move them to their own Schema."]` |
| `test_zero_messages` | only `enum E { A = 0; }` | same message with `(found 0)` |
| `test_nested_message_and_top_level_enum_allowed` | `Order` with nested `Inner`, plus top-level enum | `expected_name="com.acme.Order"`, no errors |
| `test_builtin_imports_need_no_reference` | imports and uses `google/protobuf/timestamp.proto`, `google/type/date.proto`, `confluent/meta.proto` | no errors, `external_refs=set()` |
| `test_every_builtin_compiles` | for each path in `DEFAULT_BUILTIN_PROTO_IMPORTS`: `P + f'import "{path}";\nmessage M {{ string a = 1; }}'` | exit success for all 29 (`expected_name="com.acme.M"`) |
| `test_reference` | imports `com/acme/Address.proto`, uses `Address`; `referenced={"com/acme/Address.proto": <Address text>}` | no errors, `external_refs={"com/acme/Address.proto"}`, `expected_name="com.acme.Customer"` |
| `test_transitive_reference` | main imports `a/B.proto`; `referenced` has `a/B.proto` (which imports `a/C.proto`) and `a/C.proto` | no errors, `external_refs={"a/B.proto"}` |
| `test_missing_import_left_to_stage_d` | imports `com/acme/Address.proto`, `referenced={}` | `errors=[]`, `expected_name is None`, `external_refs={"com/acme/Address.proto"}` |
| `test_commented_import_ignored` (Review Focus 3) | `// import "old/x.proto";` and `/* import "old/y.proto"; */` plus a real missing `import "com/acme/Address.proto";` | `external_refs={"com/acme/Address.proto"}`, `errors=[]` |
| `test_non_builtin_google_import_is_external` | `import "google/rpc/status.proto";` | `external_refs={"google/rpc/status.proto"}`, `errors=[]` |
| `test_unsafe_reference_paths` | `referenced` key in `"/etc/x.proto"`, `"../x.proto"`, `"a/../../x.proto"`, `"a\\b.proto"` | `["Invalid import path '<key>'."]`, `external_refs is None`; assert no file was created outside the temp directory |
| `test_warnings_do_not_fail` | no `syntax` line (`package com.acme;\nmessage M { optional string a = 1; }`); and an unused built-in import | no errors |
| `test_temp_directory_removed` | patch `tempfile.TemporaryDirectory` to record the path | the path no longer exists after `analyse` returns, on success and on failure |

- [ ] **Step 3: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_protobuf.py -q`.
- [ ] **Step 4: Implement `analyse` in `schema_validator/formats/protobuf.py`.** Parse `out.pb` with `google.protobuf.descriptor_pb2.FileDescriptorSet` and take the entry whose `name == "__main__.proto"`.
- [ ] **Step 5: Reinstall and run.** Run: `uv pip install --python .venv/bin/python -e . && .venv/bin/python -m pytest tests/test_protobuf.py -q`. Expected: all pass.
- [ ] **Step 6: Commit**

```bash
git add schema_validator/formats/protobuf.py schema_validator/_proto tests/test_protobuf.py
git commit -m "validator: Protobuf analysis with bundled well-known types"
```

---

### Task 6: Parsing Schema Registry verdict messages

**Files:**
- Create: `schema_validator/sr_messages.py`
- Test: `tests/test_sr_messages.py`

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
def invalid_schema_reason(message: str) -> str | None
    # None unless message starts with "Invalid schema ". Otherwise the text after the LAST "details: "
    # (the whole message when "details: " is absent).

@dataclass(frozen=True)
class Incompatibility:
    descriptions: tuple[str, ...]   # extracted descriptions, then messages that had no recognised shape
    old_version: int | None
    level: str | None

def parse_incompatibility(messages: Sequence[str]) -> Incompatibility
```

`parse_incompatibility`, per message:
- matches `^\{oldSchemaVersion: (\d+)\}$`: sets `old_version`;
- starts with `{oldSchema:`: dropped;
- matches `compatibility: '([A-Z_]+)'\}$` and starts with `{validateFields`: sets `level`;
- starts with `{errorType:` and contains `, description:`: take the text after the first `, description:` up to the final `}`; cut a trailing `, additionalInfo:...` (last occurrence); then remove one leading and one trailing quote character (`'` or `"`, they do not always match);
- anything else: kept unchanged as a description.

- [ ] **Step 1: Write the failing tests.** Fixtures are real SR 7.9.0 output:

```python
AVRO = ["{errorType:'READER_FIELD_MISSING_DEFAULT_VALUE', description:'The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema', additionalInfo:'zip'}",
        "{oldSchemaVersion: 1}",
        "{oldSchema: '{\"type\":\"record\",\"name\":\"Address\",\"namespace\":\"p\",\"fields\":[{\"name\":\"city\",\"type\":\"string\"}]}'}",
        "{validateFields: 'false', compatibility: 'BACKWARD'}"]
JSON_ = ["{errorType:\"TYPE_CHANGED\", description:\"A type at path '#/properties/city' is different between the new schema and the old schema'}",
         "{oldSchemaVersion: 3}", "{oldSchema: '{\"title\":\"p.JAddress\"}'}",
         "{validateFields: 'false', compatibility: 'FULL_TRANSITIVE'}"]
PROTO = ["{errorType:\"FIELD_SCALAR_KIND_CHANGED\", description:\"The kind of a SCALAR field at path '#/PAddress/1' in the new schema does not match its kind in the old schema\"}",
         "{oldSchemaVersion: 1}", "{oldSchema: 'syntax = \"proto3\";\npackage p;\n\nmessage PAddress {\n  string city = 1;\n}\n'}",
         "{validateFields: 'false', compatibility: 'BACKWARD'}"]
TYPE_CHANGE = ["Incompatible because of different schema type", "{validateFields: 'false', compatibility: 'BACKWARD'}"]
INVALID = "Invalid schema {subject=p.Order,version=0,id=-1,schemaType=AVRO,references=[],metadata=null,ruleSet=null,schema={\"type\": \"record\", \"doc\": \"details: not this\", \"name\": \"Order\"},schemaTags=null} with refs [] of type AVRO, details: \"strin\" is not a defined name."

def test_avro():
    assert parse_incompatibility(AVRO) == Incompatibility(("The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema",), 1, "BACKWARD")
def test_json_mismatched_quotes():
    assert parse_incompatibility(JSON_) == Incompatibility(("A type at path '#/properties/city' is different between the new schema and the old schema",), 3, "FULL_TRANSITIVE")
def test_protobuf():
    assert parse_incompatibility(PROTO) == Incompatibility(("The kind of a SCALAR field at path '#/PAddress/1' in the new schema does not match its kind in the old schema",), 1, "BACKWARD")
def test_plain_message_kept():
    assert parse_incompatibility(TYPE_CHANGE) == Incompatibility(("Incompatible because of different schema type",), None, "BACKWARD")
def test_several_errors_keep_order():   # two errorType entries -> two descriptions in order
def test_empty():
    assert parse_incompatibility([]) == Incompatibility((), None, None)
def test_old_schema_never_leaks():
    assert all("oldSchema" not in d and "record" not in d for d in parse_incompatibility(AVRO).descriptions)
def test_invalid_reason_takes_last_details():
    assert invalid_schema_reason(INVALID) == '"strin" is not a defined name.'
def test_invalid_reason_json():
    assert invalid_schema_reason("Invalid schema {subject=x,schema={},schemaTags=null}, details: Invalid JSON Schema") == "Invalid JSON Schema"
def test_invalid_reason_without_details():
    assert invalid_schema_reason("Invalid schema something") == "Invalid schema something"
def test_not_an_invalid_schema_message():
    assert invalid_schema_reason(AVRO[0]) is None
```

- [ ] **Step 2: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_sr_messages.py -q`.
- [ ] **Step 3: Implement both functions in `schema_validator/sr_messages.py`.**
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: Commit**

```bash
git add schema_validator/sr_messages.py tests/test_sr_messages.py
git commit -m "validator: parse Schema Registry verdict messages"
```

---

### Task 7: HTTP Schema Registry client

**Files:**
- Modify: `schema_validator/sr_client.py` (add the class below; leave the Task 1 definitions unchanged)
- Test: `tests/test_sr_client.py`

**Interfaces:**
- Consumes: the Task 1 types and exceptions in `sr_client.py`; `ValidatorConfig`.
- Produces:

```python
class HttpSchemaRegistryClient:     # implements SchemaRegistryClient
    def __init__(self, url: str, username: str | None = None, password: str | None = None,
                 timeout_seconds: float = 10, session: requests.Session | None = None) -> None
    @classmethod
    def from_config(cls, config: ValidatorConfig) -> "HttpSchemaRegistryClient"
```

Behaviour (spec §7):
- Every call: `session.request(method, url, headers={"Content-Type": "application/vnd.schemaregistry.v1+json"}, auth=(username, password) or None, timeout=timeout_seconds, data=<json or None>)`. Subjects are encoded with `urllib.parse.quote(subject, safe="")`. A trailing `/` on `url` is removed.
- `get_latest`: `GET /subjects/{s}/versions/latest`. `get_version`: `GET /subjects/{s}/versions/{n}`. `404` returns `None`. A missing `schemaType` means `"AVRO"`; a missing `references` means `()`.
- `get_compatibility_level`: `GET /config/{s}?defaultToGlobal=true`, returns `compatibilityLevel`.
- `check_compatibility`: `POST /compatibility/subjects/{s}/versions?verbose=true` with body `{"schema", "schemaType", "references": [{"name","subject","version"}]}` (`references` omitted when empty). Returns `CompatibilityVerdict(is_compatible, tuple(messages or ()))`.
- Errors, for every method: `requests.RequestException` (includes timeouts), status `>= 500`, a body that is not JSON, or a `200` body missing the expected field: `SchemaRegistryUnavailable`. `401`/`403`: `SchemaRegistryUnauthorized`. Any other `4xx` (except the two 404 cases above): `SchemaRegistryRejected(message)` with the body's `message` field (or the raw text).

- [ ] **Step 1: Write the failing tests.** Use a stub session: an object with `request(method, url, **kwargs)` that records the call and returns a stub response (`status_code`, `text`, `json()` raising `ValueError` when the text is not JSON), or raises.

| Test | Stub response | Expected |
|---|---|---|
| `test_get_latest` | 200 `{"subject":"a.B","version":3,"id":7,"schemaType":"JSON","schema":"{}","references":[{"name":"x.json","subject":"a.C","version":2}]}` | `SchemaVersion("a.B", 3, "JSON", "{}", (SrReference("x.json","a.C",2),))`; URL ends `/subjects/a.B/versions/latest` |
| `test_avro_has_no_schema_type` | 200 without `schemaType`/`references` | `schema_type=="AVRO"`, `references==()` |
| `test_get_latest_404` | 404 `{"error_code":40401,"message":"Subject 'x' not found."}` | `None` |
| `test_get_version_url_and_404` | 404 (40402) | `None`; URL ends `/subjects/a.B/versions/2` |
| `test_subject_is_url_encoded` | subject `com/acme my.Order` | URL contains `com%2Facme%20my.Order` |
| `test_level` | 200 `{"compatibilityLevel":"FULL"}` | `"FULL"`; URL ends `/config/a.B?defaultToGlobal=true` |
| `test_check_compatibility_request` | 200 `{"is_compatible":true,"messages":[]}` | `CompatibilityVerdict(True, ())`; method `POST`; URL ends `/compatibility/subjects/a.B/versions?verbose=true`; JSON body equals `{"schema":"S","schemaType":"PROTOBUF","references":[{"name":"n","subject":"s","version":1}]}`; header and `timeout=10` passed |
| `test_no_references_key_when_empty` | same, no references | body has no `references` key |
| `test_incompatible` | 200 `{"is_compatible":false,"messages":["m1","m2"]}` | `CompatibilityVerdict(False, ("m1","m2"))` |
| `test_basic_auth` | client with username/password | `auth == ("u","p")`; without them `auth is None` |
| `test_unauthorized` | 401, and 403 | `SchemaRegistryUnauthorized` |
| `test_rejected_42201` | 422 `{"error_code":42201,"message":"Invalid schema X"}` on `check_compatibility` | `SchemaRegistryRejected`, `str(e) == "Invalid schema X"` |
| `test_server_error` | 500, and 503 | `SchemaRegistryUnavailable` |
| `test_timeout_and_connection_error` | `request` raises `requests.Timeout`, `requests.ConnectionError` | `SchemaRegistryUnavailable` |
| `test_html_body` (Review Focus 4) | 200 with text `<html>proxy</html>` | `SchemaRegistryUnavailable` for all four methods |
| `test_json_without_expected_field` (Review Focus 4) | 200 `{}` on `check_compatibility` and on `get_compatibility_level` | `SchemaRegistryUnavailable` |
| `test_from_config` | `ValidatorConfig(sr_url="http://sr:8081/", sr_username="u", sr_password="p", sr_timeout_seconds=3)` | requests go to `http://sr:8081/subjects/...` with that auth and timeout |

- [ ] **Step 2: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_sr_client.py -q`.
- [ ] **Step 3: Implement `HttpSchemaRegistryClient` in `schema_validator/sr_client.py`.**
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: Commit**

```bash
git add schema_validator/sr_client.py tests/test_sr_client.py
git commit -m "validator: HTTP Schema Registry client"
```

---

### Task 8: Reference handling (stage D)

**Files:**
- Create: `schema_validator/references.py`
- Test: `tests/test_references.py`

**Interfaces:**
- Consumes: `ReferenceInput`, `proto_import_path`, `messages`, `SchemaRegistryClient`, `SrReference`, `SchemaVersion`; `FakeSchemaRegistry` in tests.
- Produces:

```python
def normalise_rows(schema_type: str, references: Sequence[ReferenceInput]) -> tuple[list[ReferenceInput], list[str]]
    # Returns (rows with ref_string filled, D6 errors).
    # AVRO: ref_string defaults to schema_name. PROTOBUF: ref_string = proto_import_path(schema_name), input ignored.
    # JSON: a row without ref_string gives REF_STRING_REQUIRED and is left out of the returned rows.

@dataclass(frozen=True)
class Resolution:
    resolved: list[SrReference]     # one per row that passed D1 and D2: (name=ref_string, subject=schema_name, version=latest)
    referenced: dict[str, str]      # reference name -> schema text, direct and transitive, dependencies first
    errors: list[str]               # D1, D2, D3, REF_TOO_DEEP

def resolve(name: str, schema_type: str, rows: Sequence[ReferenceInput], sr: SchemaRegistryClient) -> Resolution
def cross_check(schema_type: str, rows: Sequence[ReferenceInput], external_refs: set[str]) -> list[str]   # D4, D5
def pinned_warning(resolved: Sequence[SrReference]) -> str
    # PINNED_WARNING with {list} = "com.acme.Address v3, com.acme.Money v1" (subject + " v" + version, row order)
```

`resolve` algorithm (spec §6 steps 2 to 4):
- Per row: `sr.get_latest(schema_name)`. `None`: `REF_NOT_REGISTERED`. Different `schema_type`: `REF_WRONG_FORMAT` (`their_type`, `my_type`), and the row is not walked.
- Walk each accepted row's `references` depth-first with `sr.get_version(subject, version)`, memoised per `(subject, version)` for the whole call. Add texts to `referenced` after their own dependencies (post-order), keyed by the SR reference `name`; the row's own text is keyed by its `ref_string` and added after its dependencies. A key already present is not overwritten.
- If any visited subject equals `name` (including the row's own subject): one `REF_CYCLE` for that row (`s` = the row's `schema_name`), and stop walking that row.
- Depth beyond 20: `REF_TOO_DEEP` for that row. A nested `get_version` returning `None`: `REF_NOT_REGISTERED` with the nested subject.
- `SchemaRegistryError` raised by `sr` is not caught here; it propagates to the pipeline.

`cross_check`: `REF_MISSING_ROW` for each external ref with no row whose `ref_string` equals it (sorted); then for each row whose `ref_string` is not in `external_refs`: `REF_UNUSED_PROTOBUF` (`s` = schema_name, `path` = ref_string) for PROTOBUF, else `REF_UNUSED` (`x` = ref_string), in row order.

- [ ] **Step 1: Write the failing tests** (a `sr` fixture returns a fresh `FakeSchemaRegistry`)

| Test | Setup | Expected |
|---|---|---|
| `test_normalise_avro_default` | AVRO row `("a.B", None)` | `([ReferenceInput("a.B","a.B")], [])` |
| `test_normalise_avro_keeps_given` | AVRO row `("a.B","a.B")` | unchanged |
| `test_normalise_protobuf_derives` | PROTOBUF rows `("com.acme.Address", None)`, `("com.acme.Money","whatever.proto")` | ref_strings `com/acme/Address.proto`, `com/acme/Money.proto`; no errors |
| `test_normalise_json_requires_ref_string` | JSON rows `("a.B", None)`, `("a.C","c.json")` | rows `[("a.C","c.json")]`; errors `["Reference to 'a.B' needs the text used in the schema (the $ref value)."]` |
| `test_resolve_happy` | `sr.add("a.B", B)` twice; row `("a.B","a.B")` | `resolved=[SrReference("a.B","a.B",2)]`, `referenced={"a.B": B2}`, no errors |
| `test_not_registered` | row for unknown subject | `["Referenced schema 'a.B' is not registered in Schema Registry yet."]`, `resolved=[]` |
| `test_wrong_format` | `sr.add("a.B", "{}", "JSON")`, my type AVRO | `["'a.B' is a JSON schema; a AVRO schema can only reference AVRO schemas."]`, `resolved=[]` |
| `test_transitive_order` | C; B references C as name `c.ref`; row for B | `list(referenced) == ["c.ref", "a.B"]` |
| `test_direct_cycle` | B (in SR) references subject `a.Main`; validating `a.Main` with a row for B | `["Reference cycle: 'a.B' already depends on 'a.Main'."]` |
| `test_transitive_cycle` | B references C, C references `a.Main` | same message for `a.B` |
| `test_diamond_is_not_a_cycle` (Review Focus 5) | rows B and C; both reference D v1 | no errors; `("get_version","a.D",1)` appears once in `sr.calls`; D's text appears once in `referenced` |
| `test_too_deep` | chain of 22 subjects | `["References of 'a.S0' are nested more than 20 levels deep."]` |
| `test_sr_failure_propagates` | `sr.fail_with = SchemaRegistryUnavailable("x")` | `resolve` raises `SchemaRegistryUnavailable` |
| `test_cross_check_ok` | rows `[("a.B","a.B")]`, external `{"a.B"}` | `[]` |
| `test_missing_row` | no rows, external `{"a.B"}` | `["Your schema uses 'a.B' but no reference is selected for it."]` |
| `test_unused_row` | AVRO row `("a.B","a.B")`, external `set()` | `["'a.B' is selected as a reference but is not used in the schema."]` |
| `test_unused_row_protobuf` | PROTOBUF row `("com.acme.Address","com/acme/Address.proto")`, external `{"acme/address.proto"}` | `["Your schema uses 'acme/address.proto' but no reference is selected for it.", "'com.acme.Address' is selected as a reference but the schema does not import it. Write the import as: import \"com/acme/Address.proto\";"]` |
| `test_json_fragment_already_stripped` | JSON row `("a.B","address.json")`, external `{"address.json"}` | `[]` |
| `test_pinned_warning` | `[SrReference("x","com.acme.Address",3), SrReference("y","com.acme.Money",1)]` | `"References will be pinned to: com.acme.Address v3, com.acme.Money v1. They stay on these versions until this schema is saved again."` |

- [ ] **Step 2: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_references.py -q`.
- [ ] **Step 3: Implement the four functions in `schema_validator/references.py`.**
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: Commit**

```bash
git add schema_validator/references.py tests/test_references.py
git commit -m "validator: reference resolution, cycle check and cross-check"
```

---

### Task 9: Pipeline (`validate`)

**Files:**
- Create: `schema_validator/validator.py`
- Modify: `schema_validator/formats/__init__.py`, `schema_validator/__init__.py`
- Test: `tests/test_validate.py`, `tests/schemas/` (sample schema files the tests load)

**Interfaces:**
- Consumes: everything produced by Tasks 1 to 8, with the exact signatures in their Interfaces blocks.
- Produces:

```python
# formats/__init__.py
ANALYSERS: dict[str, Callable[[str, dict[str, str], ValidatorConfig], FormatAnalysis]]   # keys "AVRO", "JSON", "PROTOBUF"

# validator.py
def validate(request: ValidationRequest, sr: SchemaRegistryClient, config: ValidatorConfig) -> ValidationResult

# __init__.py exports
validate, ValidationRequest, ReferenceInput, ValidationResult, ValidatorConfig, HttpSchemaRegistryClient
```

Pipeline, in this order. Errors are kept in five lists (A, B, C, D, E) and joined in that order at the end.

1. `check_input`. If `fatal`: return `build_result(A, [])`.
2. If `references_ok`: `rows, d6 = normalise_rows(...)`; `resolution = resolve(name, schema_type, rows, sr)`. D gets `resolution.errors + d6`. If `references_ok` is false: no rows, empty `referenced`, stage D is skipped entirely.
3. `analysis = ANALYSERS[schema_type](schema, resolution.referenced, config)`. B gets `analysis.errors`.
4. C gets `check_name(request, analysis.expected_name)`.
5. If `references_ok` and `analysis.external_refs is not None`: D gets `cross_check(...)`.
6. Stage E, only when A to D are all empty and SR has not failed: `sr.check_compatibility(name, schema, schema_type, resolution.resolved)`.
   - Compatible: pass.
   - Not compatible, and some message has `invalid_schema_reason(m)` not `None`: E gets `SR_REJECTED` with that reason (first such message).
   - Not compatible otherwise: `inc = parse_incompatibility(messages)`; `sr_level = inc.level or request.compatibility`; `descriptions = "; ".join(inc.descriptions)`; E gets `SR_INCOMPATIBLE` (`n = inc.old_version`) or `SR_INCOMPATIBLE_NO_VERSION` when `old_version is None`.
   - `SchemaRegistryRejected`: E gets `SR_REJECTED` with `invalid_schema_reason(str(e)) or str(e)`.
7. SR failures, in step 2 or step 6: `SchemaRegistryUnavailable` adds `SR_UNREACHABLE` to E, `SchemaRegistryUnauthorized` adds `SR_UNAUTHORIZED` to E. After a failure in step 2, steps 3 to 5 still run with empty `referenced` and the given rows; step 6 does not.
8. Warnings, only when there are no errors:
   - if `resolution.resolved` is not empty: `pinned_warning(resolution.resolved)`;
   - if `previous_name is not None`: `level = sr.get_compatibility_level(name)`; if `level != request.compatibility`: `LEVEL_WARNING`. An SR failure here follows rule 7 (it becomes an error).
9. `build_result(A + B + C + D + E, warnings)`.

`validate` wraps all of this in `try/except Exception as e` and returns `build_result([INTERNAL_ERROR.format(err=e)], [])`.

- [ ] **Step 1: Write sample schemas** under `tests/schemas/`: `avro/order.avsc` (`com.acme.Order`), `avro/address.avsc` (`com.acme.Address`), `avro/customer.avsc` (`com.acme.Customer`, field of type `com.acme.Address`), `json/order.json` (title `com.acme.Order`), `json/address.json` (title `com.acme.Address`), `json/customer.json` (title `com.acme.Customer`, `$ref` `address.json#/properties/city`), `proto/order.proto` (`com.acme.Order`), `proto/address.proto` (`com.acme.Address`), `proto/customer.proto` (`com.acme.Customer`, `import "com/acme/Address.proto";`).

- [ ] **Step 2: Write the failing tests** (`tests/test_validate.py`; helper `req(schema_type, file, name, **overrides)`; fixture `sr` = `FakeSchemaRegistry()`; `cfg = ValidatorConfig()`)

| Test | Setup | Expected `ValidationResult` |
|---|---|---|
| `test_ok` (parametrised over the three formats) | `order` file, name `com.acme.Order` | `(True, "Ok", "")`; `check_compatibility` was called with subject `com.acme.Order`, the schema text, the type, `()` |
| `test_fatal_stops_early` | `schema_type="XML"` | Error with only the A1 message; `sr.calls == []` |
| `test_name_mismatch_reports_expected_name` (three formats) | name `com.acme.Wrong` | Error, message `The name must match the schema. It should be 'com.acme.Order'.`; `check_compatibility` not called |
| `test_duplicate_name` | `existing_names=["com.acme.order"]` | Error with the C2 message |
| `test_fullname_changed_on_edit` | `previous_name="com.acme.Old"`, name `com.acme.Old`, schema `order.avsc` | Error, message is exactly the `FULLNAME_CHANGED` text for `com.acme.Old` to `com.acme.Order` |
| `test_references_happy` (three formats) | `sr.add("com.acme.Address", <address>, type)`; `customer` file; row `ReferenceInput("com.acme.Address", <"address.json" for JSON, None otherwise>)` | `(True, "Warning", "References will be pinned to: com.acme.Address v1. They stay on these versions until this schema is saved again.")`; `check_compatibility` received `(SrReference(<ref_string>, "com.acme.Address", 1),)` with ref_string `com.acme.Address` / `address.json` / `com/acme/Address.proto` |
| `test_missing_row` (three formats) | `customer` file, no rows | Error containing `but no reference is selected for it.` and naming `com.acme.Address` / `address.json` / `com/acme/Address.proto`; no `Invalid ... schema` text |
| `test_unused_row` | `order.avsc` + a row for a registered Address | Error with the D5 message |
| `test_reference_not_registered` | `customer.avsc` + row, SR empty | Error: only the D1 message |
| `test_reference_wrong_format` | Address registered as JSON, AVRO customer | Error contains the D2 message |
| `test_reference_cycle` | Address in SR references `com.acme.Customer` | Error contains `Reference cycle: 'com.acme.Address' already depends on 'com.acme.Customer'.` |
| `test_json_row_without_ref_string` | JSON customer, row `ref_string=None` | Error contains the D6 message |
| `test_protobuf_wrong_import_path` | proto customer text with `import "acme/address.proto";`, row for registered `com.acme.Address` | Error contains `Write the import as: import "com/acme/Address.proto";` |
| `test_protobuf_transitive` | Address registered; `com.acme.Mid` registered importing it with reference name `com/acme/Address.proto`; schema imports `com/acme/Mid.proto`; one row for Mid | `validationType == "Warning"` |
| `test_avro_transitive` | same shape for Avro | `validationType == "Warning"` |
| `test_duplicate_rows_skip_stage_d` | two rows for the same subject | Error with the A5 message only; no `get_latest` call |
| `test_incompatible` | `sr.verdict = CompatibilityVerdict(False, AVRO fixture from Task 6)` | `(False, "Error", "Not BACKWARD-compatible with version 1: The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema")` |
| `test_incompatible_without_version` | verdict messages `TYPE_CHANGE` fixture | message `Not BACKWARD-compatible with the existing versions: Incompatible because of different schema type` |
| `test_sr_says_invalid` | verdict `(False, (INVALID fixture,))` | message `Schema Registry rejected the schema: "strin" is not a defined name.` |
| `test_sr_rejected_exception` | `check_compatibility` raises `SchemaRegistryRejected("Invalid schema {..}, details: boom")` | message `Schema Registry rejected the schema: boom` |
| `test_sr_unreachable` | `sr.fail_with = SchemaRegistryUnavailable("x")`, no references | `(False, "Error", "Could not reach Schema Registry to validate. Try again.")` |
| `test_sr_unreachable_during_references` | same, with a reference row | Error; message contains the E3 text once and no D1 message |
| `test_sr_unauthorized` | `sr.fail_with = SchemaRegistryUnauthorized("x")` | message `The validator is not allowed to query Schema Registry. Contact the platform team.` |
| `test_level_changed_on_edit` | edit (`previous_name == name`), `compatibility="FULL"`, `sr.levels[name]="BACKWARD"` | `(True, "Warning", "Compatibility was checked using the current level 'BACKWARD', not the new level 'FULL'.")` |
| `test_no_level_call_on_create` | create | no `get_compatibility_level` in `sr.calls` |
| `test_pinned_and_level_warnings_together` | edit with a reference and a changed level | Warning; message is the pinned line, `\n`, the level line |
| `test_errors_joined_in_stage_order` | bad compatibility (A3) + name mismatch (C3) + unused row (D5) | message lines in A, C, D order, joined by `\n` |
| `test_local_errors_skip_sr_verdict` | invalid Avro default | Error starts `Invalid Avro schema: `; `check_compatibility` not called |
| `test_never_raises` | monkeypatch `ANALYSERS["AVRO"]` to raise `RuntimeError("boom")` | `(False, "Error", "Internal validator error: boom")` |
| `test_wrong_input_types_do_not_raise` | `schema=None`; `references=None` | `valid is False`, `validationType == "Error"` |
| `test_result_invariant` | every result produced in this file (collect via a wrapper fixture) | `valid == (validationType != "Error")` |

- [ ] **Step 3: Run and see them fail.** Run: `.venv/bin/python -m pytest tests/test_validate.py -q`.
- [ ] **Step 4: Implement `ANALYSERS`, `validate` and the exports.**
- [ ] **Step 5: Run the whole unit suite.** Run: `.venv/bin/python -m pytest -q`. Expected: all pass, no test skipped.
- [ ] **Step 6: Commit**

```bash
git add schema_validator/validator.py schema_validator/formats/__init__.py schema_validator/__init__.py tests/test_validate.py tests/schemas
git commit -m "validator: validation pipeline"
```

---

### Task 10: Integration tests against the real registry, README, VERIFICATION.md

**Files:**
- Create: `integration/tests/conftest.py`, `integration/tests/test_sr_behaviour.py`, `integration/tests/test_validate_real_sr.py`, `integration/tests/test_python_clients.py`, `README.md`, `VERIFICATION.md`
- Move: `integration/probes/java/` to `integration/java/`
- Delete: `integration/probes/` (the Python probe scripts, once their checks live in the tests)

**Interfaces:**
- Consumes: `validate`, `HttpSchemaRegistryClient`, `ValidationRequest`, `ReferenceInput`, `ValidatorConfig`, `DEFAULT_BUILTIN_PROTO_IMPORTS`.
- Produces: nothing for other tasks.

Setup facts:
- The stack is defined in `integration/docker-compose.yml` and is normally already running: Schema Registry on `http://localhost:18081` (no auth), Kafka on `localhost:19092`. Start it with `docker compose -p validator-it -f integration/docker-compose.yml up -d` if `curl -fs localhost:18081/subjects` fails. Both images are local; nothing is downloaded.
- `conftest.py`: reads `SR_URL` (default `http://localhost:18081`) and `KAFKA_BOOTSTRAP` (default `localhost:19092`); marks every test in the directory `integration`; skips the session with a clear reason when SR is not reachable; provides a `ns` fixture returning a unique namespace per test (`"it" + uuid4().hex[:10]`) so reruns never collide with earlier subjects; provides `sr` = `HttpSchemaRegistryClient(SR_URL)` and a raw `register(subject, schema, schema_type, references)` helper using `requests`.
- The existing scripts `integration/probes/p1_compat_new_subject.py`, `p2_refs_drafts_imports.py`, `p3_details.py`, `p4_python_clients.py` contain working requests and schemas for every check below. Reuse them, then delete them.

- [ ] **Step 1: Write `integration/tests/test_sr_behaviour.py`** (spec §10a items 1, 4, 5, 7). One test per row, each parametrised over the formats where it says so:

| Test | Assertion |
|---|---|
| `test_new_subject_valid` (3 formats) | `sr.check_compatibility(new_subject, valid)` is `CompatibilityVerdict(True, ())` |
| `test_new_subject_invalid` (3 formats) | `is_compatible is False` and `invalid_schema_reason(messages[0])` is not `None` |
| `test_incompatible_messages_parse` (3 formats) | register v1, check an incompatible v2: `parse_incompatibility` gives at least one description, `old_version == 1`, `level == "BACKWARD"`, and no description contains `oldSchema` |
| `test_sr_accepts_unused_reference_row` (3 formats) | raw register returns 200 |
| `test_sr_accepts_protobuf_reference_name_mismatch` | raw register returns 200 |
| `test_sr_accepts_protobuf_cycle` | register Address, Customer referencing it, then Address v2 referencing Customer: 200 |
| `test_sr_accepts_json_title_with_space` | raw register under subject `"<ns>.My Order"` returns 200 |
| `test_builtin_imports` | for each path in `DEFAULT_BUILTIN_PROTO_IMPORTS`, a schema that imports it and uses one of its types registers with 200 (take the type per path from `p2_refs_drafts_imports.py`; for paths not in that script, import without using a type and assert 200) |
| `test_non_builtin_imports_rejected` | `google/type/localized_text.proto` using `google.type.LocalizedText`, and `google/rpc/status.proto` using `google.rpc.Status`: 422 |
| `test_level_endpoint_for_missing_subject` | `sr.get_compatibility_level("<ns>.none") == "BACKWARD"` |
| `test_get_latest_missing` | `sr.get_latest("<ns>.none") is None` |

- [ ] **Step 2: Write `integration/tests/test_validate_real_sr.py`** (spec §10a item 6). Real `validate` with the real client:

| Test | Expected |
|---|---|
| `test_create_ok` (3 formats) | `validationType == "Ok"`; then registering the same schema under `request.name` returns 200 |
| `test_stage_a_failure` | empty schema: `Schema is empty.` |
| `test_stage_b_failure` (3 formats) | syntactically broken schema: Error, and SR was not needed |
| `test_stage_c_failure` | wrong name: message names the expected name |
| `test_stage_d_failure` | reference to an unregistered subject: D1 message |
| `test_stage_e_invalid` | Avro record with a field named `a` twice (fastavro accepts, SR rejects): message starts `Schema Registry rejected the schema: ` and contains `Duplicate field a` |
| `test_stage_e_incompatible` (3 formats) | register v1, validate an incompatible edit (`previous_name == name`): message starts `Not BACKWARD-compatible with version 1: ` |
| `test_references_end_to_end` (3 formats) | register Address; validate Customer with its row: `Warning` with `v1`; then register Customer with `{name: ref_string, subject, version: 1}` where the Protobuf name is `proto_import_path(subject)`: 200 |
| `test_level_warning_on_edit` | set the subject level to `FULL` with `PUT /config/{subject}`, validate an edit with `compatibility="BACKWARD"`: the level warning with `'FULL'` |
| `test_unreachable` | client pointed at `http://localhost:1`, `sr_timeout_seconds=1`: the E3 message |

- [ ] **Step 3: Write `integration/tests/test_python_clients.py`** (spec §10a items 2 and 3). For each case, call `validate` first and register under the name it accepts, then serialize with the real `confluent_kafka` serializer configured with `record_subject_name_strategy`, `auto.register.schemas=False`, `use.latest.version=True`, and produce the bytes to Kafka. Cases: Avro simple; Avro with a dotted `name` and a conflicting `namespace`; Avro with a reference (pass `Schema(text, "AVRO", [SchemaReference(...)])`); JSON simple; JSON with an external `$ref`; Protobuf simple; Protobuf importing `google/protobuf/timestamp.proto`; Protobuf importing another Schema as `<ns path>/Address.proto`. Protobuf classes are generated per test with `grpc_tools.protoc --python_out` into `tmp_path`. Each test asserts the serializer returns bytes, and that the same serializer fails with `Subject '<expected_name>' not found` before registration.

- [ ] **Step 4: Run the integration suite.**

Run: `.venv/bin/python -m pytest integration/tests -m integration -q`
Expected: all pass, none skipped. Where the real registry contradicts a unit-test assumption, the observation wins: fix the code and the unit test, and record the deviation in `VERIFICATION.md`.

- [ ] **Step 5: Move the Java checks and run them.**

```bash
git mv integration/probes/java integration/java 2>/dev/null || mv integration/probes/java integration/java
cd integration/java && docker run --rm --network validator-it_default -v "$PWD:/work" -v "$PWD/../../../.cache/m2:/root/.m2" -w /work maven:3.9.9-eclipse-temurin-17 sh -c 'mvn -q -B package dependency:build-classpath -Dmdep.outputFile=target/cp.txt && java -cp target/classes:$(cat target/cp.txt) probe.Main && java -cp target/classes:$(cat target/cp.txt) probe.ProtoRefs'; cd ../..
```

Expected, to copy into `VERIFICATION.md`: every `after registering` line in `probe.Main` says `OK` except the two Protobuf-with-import cases (they use a non-conforming import path on purpose and fail with `Subject 'p5java/address.proto' not found`) and `JSON POJO with @JsonSchemaTitle` (`Incompatible schema`); in `probe.ProtoRefs`, case `[A] qualified strategy` is `OK` and `default strategy` fails, `[C]` fails to deserialize, `[D]` and `[E]` are `OK`.

- [ ] **Step 6: Write `README.md`.** Sections, in this order: what the library does (three sentences); install (`pip install .`, the five dependencies, Python 3.11); usage (a complete example: build `ValidatorConfig`, `HttpSchemaRegistryClient.from_config`, a `ValidationRequest`, call `validate`, read the three result fields); configuration (a table of every `ValidatorConfig` field with its default); what each field of the request means, including that `ref_string` is required for JSON, optional for Avro and ignored for Protobuf; the checks (one table: code A1 to E4, what it checks, the message, copied from `messages.py`); the Protobuf import-path rule with one example; running the unit tests and the integration tests; what America must still do (link to spec §11) and the client settings (link to spec §12).

- [ ] **Step 7: Write `VERIFICATION.md`.** One row per item of spec §10a (items 1 to 7): what was tested, the observed behaviour (status codes, message text), the test that pins it, and what changed in the code as a result. Add the Java results from step 5 and a "Not verified" section with the two open items from spec §10 (401/403 against a secured registry; the ACL operations the validator's user needs).

- [ ] **Step 8: Delete the Python probes, run everything, commit.**

```bash
rm -r integration/probes
.venv/bin/python -m pytest -q && .venv/bin/python -m pytest integration/tests -m integration -q
git add -u integration && git add integration/tests integration/java README.md VERIFICATION.md
git commit -m "validator: integration tests against Schema Registry 7.9.0, README, verification record"
```

Expected: both suites pass.
