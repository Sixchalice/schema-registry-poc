# Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every finding of the independent audit (`/Users/noamram/validator-audit/REPORT.md`, 2026-11-17) that is accepted below, with unit and real-registry tests, so `validate` has no known false pass.

**Architecture:** The fixes stay in the existing pipeline. Version and name conflicts go into the reference walk (stage D). `$id`-aware `$ref` resolution goes into the JSON analyser. Top-level enum and fixed go into the Avro analyser. Mode and level checks go into stage E, through two new `SchemaRegistryClient` methods. The HTTP client gets a wall-clock deadline, no redirects and no raw-body echo.

**Tech Stack:** As in `docs/superpowers/plans/2026-11-16-schema-validator.md`: Python 3.11, fastavro 1.13.1, jsonschema 4.26.0, grpcio-tools 1.85.1, requests 2.34.2, pytest, coverage. Integration: the `validator-it` Docker stack (Schema Registry 7.9.0 on :18081 and :18082, Kafka on :19092), confluent-kafka 2.16.1, and the Java probes in `integration/java`.

**Spec:** `schema-validator-plan.md` (the rules) and `/Users/noamram/validator-audit/REPORT.md` (the findings; its reproduction scripts are in `/Users/noamram/validator-audit/`). Every task that changes a rule also updates the spec section it names, so the spec stays the source of truth.

## Triage of the audit

| Finding | Take | Task |
|---|---|---|
| F1 one subject at two versions (silent corruption) | **Agree, critical.** Reproduced: the result is Warning; the Python bytes `0a…0e…` carry `total.amount=7`, and a reader with Money v1 gets 3 | 1 |
| F2 JSON `$ref` ignores `$id` | **Agree.** Reproduced J2: the result is Warning, and the Python producer fails with `Unresolvable: address.json` | 2 |
| F3 Java Protobuf needs QualifiedReferenceSubjectNameStrategy | **Already known.** It is a documented producer setting (spec §2, §12; `probe.ProtoRefs`). The reviewer had no docs, by design. No code change | none |
| F4 the level change is checked at the old level | **Agree on the problem, not on the fix.** The registry has no "check under level X" call, so the suggested fix does not work. Recommended: refuse a level change and a schema change in the same save. **Needs owner OK** | 7 |
| F5 Avro top level must be a record | **Agree.** Our own `AVRO_NESTED_IN_REFERENCE` advice ("move it to its own Schema") is impossible for an enum today | 3 |
| F6 subject mode not checked | **Agree.** Cheap, and it also covers a READONLY global mode (a DR standby) | 6 |
| F7 transitive-only names need no row | **Disagree.** Deliberate (spec §5.1 step 6). It works in the registry and both clients. Requiring a row would pin the latest version and cause F1 conflicts more often. Task 1 closes the real risk | none |
| L1 no total deadline | Agree (small) | 5 |
| L2 raw body echoed | Agree: `_error_message` falls back to `response.text` | 5 |
| L3 redirect turns POST into GET | Agree (one-line fix) | 5 |
| L4 JSON root `$ref` / type array | Disagree: keep as a platform rule | none |
| L5 JSON reference cycles | Disagree: keep as a platform rule (deletion order, D3 for every format) | none |
| L6 Avro custom ref_string gives two errors | Agree: allow only an empty value or the schema name, with one message | 4 |
| L7 `https://json-schema.org/draft-07/schema` | Reversed during execution: jsonschema reads https draft-07 as 2020-12 (DeprecationWarning), so it stays rejected. `-` in a title stays rejected (platform rule) | 2 |
| L8 deep JSON / lone surrogate become internal errors | Agree: give them real messages. Request type checks: no, a wrong type is a platform bug and INTERNAL_ERROR is right for it | 2, 4 |
| L9 `existing_names` holding the edited name | Agree (defensive, one line) | 4 |

## Global Constraints

- Every path is relative to `validator/`, and every command runs from `validator/`. Python is `.venv/bin/python`.
- Runtime dependencies are unchanged: the standard library, `fastavro`, `jsonschema`, `grpcio-tools`, `protobuf`, `requests`. `confluent-kafka` only in `integration/`.
- Every user-facing message is a constant in `schema_validator/messages.py`, with the exact text given in this plan. `README.md` lists every message word for word, so each task that adds or changes a message updates README in the same commit.
- `validate` never raises. The worst failure is a false pass: when a task can't tell, it returns Error.
- Unit coverage stays at 100% line and branch: `.venv/bin/python -m coverage run --branch --source=schema_validator -m pytest tests -q && .venv/bin/python -m coverage report --fail-under=100 --show-missing`.
- Integration: `.venv/bin/python -m pytest integration/tests -q` with the stack up (`docker compose -p validator-it -f integration/docker-compose.yml up -d`). Create only uniquely prefixed subjects. Never set the **global** mode or level on the shared registries.
- Do not stop or reconfigure `validator-it-*` or `sr-acl-poc-*` containers, except where Task 6 or 7 changes the secured registry's ACL list in `integration/docker-compose.yml`, and then only that service.
- Git: stage only the files a task names, never `git add -A`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push.

## Review Focus

1. A diamond whose two paths reach one subject at the **same** version must still pass (no false conflict). Task 1, `test_diamond_same_version_is_not_a_conflict`.
2. Two rows that each reach Money at a different version, with no direct row for Money, must be a conflict. Task 1, `test_conflict_between_two_transitive_paths`.
3. A `$id` on a nested subschema moves the base for the `$ref`s below it, and a fragment-only `$id` (`"#foo"`) does not. Task 2, `test_nested_id_moves_the_base` and `test_fragment_id_keeps_the_base`.
4. A subject with no mode of its own, under a READONLY global mode, must give Error. Task 6, `test_mode_inherited_from_global_blocks`, on the fake (never on the shared registry).
5. An edit with a different level on a subject that has no versions (soft-deleted) must not get the Task 7 error. Task 7, `test_level_change_without_versions_is_not_blocked`.

## Task order

Tasks 1 to 4 are independent. Tasks 5, 6 and 7 run in that order, because all three touch `sr_client.py`, `tests/fake_sr.py` and `validator.py`. Task 8 runs last.

---

### Task 1: One version per subject and one Schema per reference name (F1)

**Files:**
- Modify: `schema_validator/references.py` (`resolve`, `_walk`), `schema_validator/messages.py`
- Test: `tests/test_references.py`, `tests/test_validate.py`, `integration/tests/test_validate_real_sr.py`, `integration/tests/test_python_clients.py`
- Docs: spec §4 stage D table and §6, `README.md`, `VERIFICATION.md`

**Interfaces:**
- Produces: two new messages. `REF_VERSION_CONFLICT = "'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Save the Schemas that use an older version again so that they use v{latest}, then save this schema."` and `REF_NAME_CONFLICT = "The reference name '{x}' is used for more than one Schema: {subjects}. Each Schema needs its own reference name."` They are added to `Resolution.errors`, after the per-row errors, sorted by `{s}` / `{x}`.
- `{uses}`: one entry per version, ascending, joined with `", "`. An entry is `v{n} directly` for this schema's own row, or `v{n} through '{p}'` (several parents sorted and joined with `"' and '"`). If one version is reached both directly and through others, the entry is `v{n} directly and through '{p}'`. `{latest}` is the highest version. `{subjects}` is the sorted subjects, each in single quotes and joined with `", "`.

- [ ] **Step 1: Write the failing unit tests** in `tests/test_references.py`, on `FakeSchemaRegistry`:
  - `test_version_conflict_direct_and_transitive`: the t17 setup (Money v1, Line refs Money v1, Money v2; rows Line and Money). The errors equal `["'a.Money' is used at more than one version: v1 through 'a.Line', v2 directly. A schema can use only one version of it. Save the Schemas that use an older version again so that they use v2, then save this schema."]`.
  - `test_conflict_between_two_transitive_paths`: rows B and C only. B refs Money v1 and C refs Money v2. Expect `v1 through 'a.B', v2 through 'a.C'`.
  - `test_diamond_same_version_is_not_a_conflict`: B and C both ref Money v1, and there are rows B and C. Expect no errors.
  - `test_name_conflict`: JSON. Row `address.json` → `a.Address`; `a.Address` refs `address.json` → `a.Other`. Expect `["The reference name 'address.json' is used for more than one Schema: 'a.Address', 'a.Other'. Each Schema needs its own reference name."]`.
  - `test_conflict_recorded_on_a_skipped_revisit`: the second path reaches a node already walked at a deeper level, and it is still recorded.
- [ ] **Step 2: Run them and check that they fail**: `.venv/bin/python -m pytest tests/test_references.py -q -k "conflict or diamond"`. Expected: 4 failures, and the diamond test passes.
- [ ] **Step 3: Implement it.** In `resolve`, keep `uses: dict[str, dict[int, set[str | None]]]` (subject → version → parents, with `None` for the row itself) and `names: dict[str, set[str]]` (reference name → subjects). Record each row after D1 and D2. Pass both dicts into `_walk` and record **every child edge before the `deepest` skip** (parent = the subject of the schema whose references are being iterated). After the loop, add `REF_VERSION_CONFLICT` for each subject with more than one version, then `REF_NAME_CONFLICT` for each name with more than one subject.
- [ ] **Step 4: Run the unit tests and check that they pass**: `.venv/bin/python -m pytest tests -q`, then the coverage command. Expected: everything passes and coverage is 100%.
- [ ] **Step 5: Add a pipeline test**: in `tests/test_validate.py`, `test_version_conflict_is_an_error` runs the t17 setup through `validate`, expects `valid=False` with the message above, and expects no `check_compatibility` call.
- [ ] **Step 6: Add the integration tests.** In `test_validate_real_sr.py`, `test_version_conflict_real_sr` runs the t17 setup on :18081 and expects Error. In `test_python_clients.py`, `test_two_versions_of_one_type_corrupt_data` documents why: register the setup directly, serialize with the Python `AvroSerializer`, then decode the payload with `fastavro.schemaless_reader` using Order with Money v1 inlined everywhere (what Java resolves). It asserts `total.amount != 7`.
- [ ] **Step 7: Run them**: `.venv/bin/python -m pytest integration/tests -q -k "conflict or corrupt"`. Expected: both pass.
- [ ] **Step 8: Update the docs and commit.** Add the D-row to the spec ("D7: one version per subject, one Schema per reference name, across the whole graph") and add the two messages to README. Add a VERIFICATION row citing audit F1. Commit: `validator: one version per subject across the reference graph (audit F1)`.

### Task 2: JSON `$ref` resolved against `$id`, https draft-07, deep schemas (F2, L7, L8a)

> Reversed during execution: the https draft-07 part (L7) was made and then reverted, because jsonschema reads `https://json-schema.org/draft-07/schema` as 2020-12 with a DeprecationWarning; it stays rejected (`test_https_draft07_rejected`).

**Files:**
- Modify: `schema_validator/formats/json_schema.py`, `schema_validator/models.py` (`DEFAULT_JSON_DRAFTS`), `schema_validator/messages.py`
- Test: `tests/test_json_schema.py`, `integration/tests/test_python_clients.py`
- Docs: spec §5.2 steps 5 and 8, README

**Interfaces:**
- Produces: `_external_refs(schema: dict[str, Any], id_keyword: str) -> set[str]`, where `id_keyword` is `"id"` when `$schema` is draft-04 and `"$id"` otherwise. Each external reference is the `$ref` resolved against its base URI, without its fragment. The new message `JSON_TOO_DEEP = "JSON schema is nested too deeply."`
- Rule (spec §5.2 step 8, new text): the base URI starts as the root's `$id` (if any). A subschema with a string `$id` moves the base to `urljoin(base, id)` without its fragment. A fragment-only `$id` keeps the base. A `$ref` resolves to `urldefrag(urljoin(base, ref)).url`. It is internal when the result is `""` or equals any base set inside the document; otherwise it is external, and its row's `ref_string` must equal it exactly. Without any `$id`, behaviour is unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_json_schema.py`:
  - `test_relative_ref_under_id_resolves_to_absolute` (J2): root `$id` `https://acme.example/schemas/order.json`, `$ref` `address.json` → `external_refs == {"https://acme.example/schemas/address.json"}`.
  - `test_ref_to_internal_id_is_internal` (J5): → `set()`.
  - `test_ref_to_own_id_with_fragment_is_internal` (J5b): → `set()`.
  - `test_nested_id_moves_the_base`: the subschema has `$id` `https://x/sub/`, and the `$ref` below it is `a.json` → `{"https://x/sub/a.json"}`.
  - `test_fragment_id_keeps_the_base`: `$id` `#foo` on a subschema → the refs below it resolve against the root base.
  - `test_draft04_uses_id`: `$schema` draft-04 with root `id` → resolved against it; a `$id` is ignored there.
  - `test_https_draft07_accepted`: `$schema` `https://json-schema.org/draft-07/schema#` → no errors, checked with Draft7.
  - `test_deep_schema_gives_too_deep`: properties nested 200 levels → `errors == ["JSON schema is nested too deeply."]`.
- [ ] **Step 2: Run them and check that they fail**: `.venv/bin/python -m pytest tests/test_json_schema.py -q`.
- [ ] **Step 3: Implement it.** Two iterative passes over the same traversal as today: the first collects the bases, the second resolves the refs. Use `urllib.parse.urljoin` and `urldefrag`. Add `"https://json-schema.org/draft-07/schema"` to `DEFAULT_JSON_DRAFTS`, and pass `{"$schema": "http://json-schema.org/draft-07/schema#"}` to `validator_for` for it. Catch `RecursionError` around `check_schema` and return `JSON_TOO_DEEP`.
- [ ] **Step 4: Run the unit suite and coverage.** Expected: pass, 100%.
- [ ] **Step 5: Add the integration tests** in `test_python_clients.py`, each registering and producing with the Python serializer:
  - `test_json_id_relative_ref_is_rejected`: J2 gives Error naming `https://acme.example/schemas/address.json`.
  - `test_json_id_absolute_ref_string_works`: J3 gives Warning, registers and produces, and a bad value is refused by the client.
  - `test_json_internal_id_ref_works`: J5 gives Ok.
  - `test_json_https_draft07_works`.
- [ ] **Step 6: Run them**: `.venv/bin/python -m pytest integration/tests -q -k json`. Expected: pass.
- [ ] **Step 7: Update the docs and commit**: `validator: JSON $ref resolved against $id; https draft-07; deep schemas (audit F2, L7, L8)`.

### Task 3: Top-level Avro enum and fixed (F5)

**Files:**
- Modify: `schema_validator/formats/avro.py` (step 2 in `analyse`), `schema_validator/messages.py`
- Test: `tests/test_avro.py`, `integration/tests/test_python_clients.py`, `integration/java/src/main/java/probe/Nested.java`
- Docs: spec §5.1 step 2 and the note under it ("SR accepts top-level unions and enums…"), README

**Interfaces:**
- Produces: `AVRO_TOP_LEVEL = "The top level of an Avro schema must be a single record, enum or fixed."` Step 2 accepts a dict whose `type` is in `_NAMED_TYPES`. Name derivation and the namespace rule are unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_avro.py`:
  - `test_top_level_enum`: `{"type":"enum","name":"Color","namespace":"a.b","symbols":["RED"]}` → `expected_name == "a.b.Color"`, `errors == []`, `external_refs == set()`.
  - `test_top_level_fixed`: the same, for a `fixed` of size 16.
  - `test_top_level_enum_needs_namespace`: → `[AVRO_NAMESPACE]`.
  - `test_top_level_array_still_rejected`: → `[AVRO_TOP_LEVEL]` with the new text.
  - `test_record_using_a_referenced_enum`: `referenced={"a.b.Color": <enum text>}` → `external_refs == {"a.b.Color"}` and no errors.
- [ ] **Step 2: Run them and check that they fail.**
- [ ] **Step 3: Implement it** (step 2 condition, and the message).
- [ ] **Step 4: Run the unit suite and coverage.** Expected: pass, 100%.
- [ ] **Step 5: Add the integration tests.** `test_avro_shared_enum`: validate and register `<ns>.Color`, then validate (Warning), register, produce and consume a record that references it with the Python client. In `probe.Nested`, add an `expectOk` case for the same pair with `KafkaAvroSerializer`, where the client schema has Color inlined.
- [ ] **Step 6: Run them.** Run `.venv/bin/python -m pytest integration/tests -q -k shared_enum`, then the Java command from README §"Java" (it runs the `maven:3.9.9-eclipse-temurin-17` container on `validator-it_default`, about 1 to 2 minutes with the cached `.m2`). Expected: Python passes, and `probe.Nested` prints `all cases as expected` and exits 0.
- [ ] **Step 7: Update the docs and commit**: `validator: Avro enum and fixed may be top-level schemas (audit F5)`.

### Task 4: Small request rules (L6, L8b, L9)

**Files:**
- Modify: `schema_validator/rules.py`, `schema_validator/references.py` (`normalise_rows`), `schema_validator/messages.py`
- Test: `tests/test_rules.py`, `tests/test_references.py`, `tests/test_validate.py`
- Docs: spec §3.1 (the `ref_string` comment), §4 stage A and C2, README

**Interfaces:**
- Produces:
  - `AVRO_REF_STRING = "For an Avro reference to '{s}', leave the reference text empty or write '{s}'."` (stage A5; sets `references_ok=False`).
  - `SCHEMA_NOT_TEXT = "Schema contains characters that are not valid text."` (A2; fatal).
  - `normalise_rows` for AVRO sets `ref_string = schema_name` (equal by then).
  - C2 leaves `previous_name` out of `existing_names` on an edit.

- [ ] **Step 1: Write the failing tests.**
  - `test_avro_custom_ref_string_rejected`: row `("a.Address", "address.avsc")` → exactly `[AVRO_REF_STRING with s="a.Address"]` from `validate`, and no D4/D5.
  - `test_avro_ref_string_equal_to_name_allowed`.
  - `test_lone_surrogate_schema`: schema `"\ud800"` → `[SCHEMA_NOT_TEXT]`, fatal.
  - `test_edit_ignores_own_name_in_existing_names`: `previous_name="a.B"`, `existing_names=["a.B"]` → no `NAME_EXISTS`.
  - `test_edit_still_catches_other_case_variant`: `existing_names=["A.b"]` and `name="a.B"` on an edit → `NAME_EXISTS`.
- [ ] **Step 2: Run them and check that they fail.**
- [ ] **Step 3: Implement it.** `UnicodeEncodeError` around the A2 size computation gives `SCHEMA_NOT_TEXT`.
- [ ] **Step 4: Run the unit suite and coverage.** Expected: pass, 100%.
- [ ] **Step 5: Update the docs and commit**: `validator: Avro reference text, non-text schemas, own name on edit (audit L6, L8, L9)`.

### Task 5: Registry client hardening (L1, L2, L3)

**Files:**
- Modify: `schema_validator/sr_client.py`, `schema_validator/validator.py` (`_sr_failure`), `schema_validator/messages.py`
- Test: `tests/test_sr_client.py` (extend `StubResponse` with `iter_content(chunk_size=None)` and `close()`), `tests/test_validate.py`
- Docs: spec §7 (client), README

**Interfaces:**
- Produces:
  - `SchemaRegistryRejected(message: str, code: int)` with a `.code` attribute. `message` is the body's `message` field or `""`, never `response.text`.
  - `SR_REJECTED_CODE = "Schema Registry rejected the schema (error code {code})."`
  - `_sr_failure` shows, in order: the `details:` reason if `invalid_schema_reason` finds one; else the message if it is non-empty and has no `{`; else `SR_REJECTED_CODE`.
- Behaviour: every request is sent with `allow_redirects=False` and `stream=True`. The body is read with `iter_content(chunk_size=None)` until `time.monotonic()` passes start + `timeout_seconds`, which raises `SchemaRegistryUnavailable("<method> <path>: response took longer than the timeout")`. The JSON is parsed from the collected bytes, and the response is always closed. A 3xx stays E3 (it already falls through to "not 2xx").

- [ ] **Step 1: Write the failing tests.**
  - `test_slow_body_hits_deadline`: the stub yields 5 chunks while a patched `time.monotonic` advances 1 s per chunk, with timeout 2 → `SchemaRegistryUnavailable`.
  - `test_redirects_not_followed`: the session receives `allow_redirects=False`, and a 302 stub → `SchemaRegistryUnavailable`.
  - `test_raw_body_never_in_rejected`: a 400 `{"error_code":400,"detail":"Authorization: Basic xyz"}` → `str(e) == ""` and `e.code == 400`. In `test_validate.py`, the result is `"Schema Registry rejected the schema (error code 400)."`
  - `test_structured_message_not_echoed`: a 422 whose message contains `{` with no `details:` → `SR_REJECTED_CODE`.
  - `test_plain_message_still_shown`: 422 `"Either the input schema or one its references is invalid"` → shown as it is.
- [ ] **Step 2: Run them and check that they fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the unit suite, coverage, then `.venv/bin/python -m pytest integration/tests -q`.** Expected: all pass. The real-registry message tests must be unchanged: if one changes, its message had a `{` and needs a look before you adjust anything.
- [ ] **Step 5: Re-run the reviewer's fake-registry checks**: `cd /Users/noamram/validator-audit && PYTHONPATH=/Users/noamram/Projects/acl-schema-registry/validator .venv/bin/python t13_failures.py` (PYTHONPATH comes before the copy installed in that venv). Expected: the slow case finishes in about 2 s with "Could not reach", the 302 case gives "Could not reach", and no `Authorization` appears.
- [ ] **Step 6: Update the docs and commit**: `validator: request deadline, no redirects, no raw registry bodies (audit L1-L3)`.

### Task 6: Subject mode (F6)

**Files:**
- Modify: `schema_validator/sr_client.py` (protocol + HTTP), `tests/fake_sr.py` (`modes: dict[str, str]`, default `"READWRITE"`, plus a `global_mode` attribute), `schema_validator/validator.py`, `schema_validator/messages.py`
- Test: `tests/test_sr_client.py`, `tests/test_validate.py`, `tests/test_fake_sr.py`, `integration/tests/test_sr_behaviour.py`, `integration/tests/test_validate_real_sr.py`, `integration/tests/test_secured_sr.py`
- Docs: spec §4 stage E (new E5), §7 (endpoint and ACL list), README, VERIFICATION

**Interfaces:**
- Produces:
  - `SchemaRegistryClient.get_mode(subject: str) -> str`, using `GET /mode/{s}?defaultToGlobal=true`, body `{"mode": str}`. Any other shape is a bad body.
  - `SR_MODE = "Schema Registry does not accept new versions of '{name}' right now (mode {mode}). Contact the platform team."`
  - In stage E, after a compatible verdict and before the warnings: a mode other than `READWRITE` → Error `SR_MODE`.

- [ ] **Step 1: Pin the registry's behaviour first** (`test_sr_behaviour.py`): `test_mode_default_to_global` checks that `GET /mode/<new subject>?defaultToGlobal=true` gives 200 `{"mode":"READWRITE"}`. `test_mode_readonly_subject` runs `PUT /mode/<s>` READONLY (on a fresh prefixed subject) and checks that a registration gives 422 with error_code 42205. Run them. If the first one gives 404, stop and replace `defaultToGlobal` with "on 404 40409, `GET /mode`" in the Interfaces above before going on.
- [ ] **Step 2: Write the failing unit tests**: `test_get_mode` and `test_get_mode_bad_body` in test_sr_client; `test_readonly_subject_is_an_error`, `test_mode_inherited_from_global_blocks` (fake `global_mode="READONLY"`) and `test_import_mode_is_an_error` in test_validate; and the fake tests.
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the unit suite and coverage.** Expected: pass, 100%.
- [ ] **Step 5: Add the real-registry tests.** `test_readonly_subject_rejected` (validate gives `SR_MODE`), and on :18082 `test_mode_lookup_by_validator_user`, which expects the validator user to get 200. If it gets 403, find the needed operation with `sr-acl-cli` against the secured service, add it to the `acl validator` loop in `integration/docker-compose.yml`, recreate only `schema-registry-secured`, and add the operation to the spec §7 ACL list.
- [ ] **Step 6: Run the integration suite.** Expected: pass.
- [ ] **Step 7: Update the docs and commit**: `validator: refuse when the subject is not writable (audit F6)`.

### Task 7: Level change and schema change in one save (F4)

Owner decision (2026-11-17, option 2 of three): the registry checks a new version under the level in force when it is registered, and it has no way to check under another level. So on an edit whose `compatibility` differs from the subject's current level, the schema must be unchanged. "Unchanged" means that the schema, looked up with the **latest version's own references**, is already registered under the subject. If it is not, the result is an Error, and the user saves the level first and the schema afterwards. A subject without versions is not checked. This is correct whichever order the platform uses for `PUT /config` and registration. One known gap: if the references' latest versions changed in the meantime, the re-save repins them under the old level.

**Files:**
- Modify: `schema_validator/sr_client.py` (protocol + HTTP; `40403` joins the not-found codes for this call), `tests/fake_sr.py`, `schema_validator/validator.py`, `schema_validator/messages.py`
- Test: `tests/test_sr_client.py`, `tests/test_validate.py`, `tests/test_fake_sr.py`, `integration/tests/test_validate_real_sr.py`, `integration/tests/test_secured_sr.py`
- Docs: spec §4 (replaces the "Extra warning on edit" paragraph), §7, README, VERIFICATION

**Interfaces:**
- Consumes: `get_latest`, `get_compatibility_level`.
- Produces:
  - `SchemaRegistryClient.is_registered(subject: str, schema: str, schema_type: str, references: Sequence[SrReference]) -> bool`, using `POST /subjects/{s}` with the same body as `check_compatibility`. 200 gives True; 404 with 40401 or 40403 gives False.
  - `LEVEL_AND_SCHEMA_CHANGED = "The compatibility level and the schema cannot change in the same save. Schema Registry checks a new version under the level in force when it is registered (now '{sr_level}'). Save the new level '{compatibility}' with the schema unchanged first, then change the schema."`
  - The level check moves to before the compatibility call. When the level is the same: nothing changes. When it differs and the schema is unchanged: compatibility runs and `LEVEL_WARNING` is kept. When it differs and the schema changed: only `LEVEL_AND_SCHEMA_CHANGED`, with no compatibility call.

- [ ] **Step 1: Write the failing unit tests**:
  - `test_level_and_schema_change_together_is_an_error`
  - `test_level_change_with_unchanged_schema_warns`
  - `test_level_change_without_versions_is_not_blocked`
  - `test_same_level_unchanged_behaviour`
  - `test_lookup_uses_latest_versions_own_references`
  - `is_registered` 200/404/bad-body tests in test_sr_client
- [ ] **Step 2: Run them and check that they fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the unit suite and coverage.** Expected: pass, 100%.
- [ ] **Step 5: Add the integration tests**: the reviewer's C3 (NONE→FULL with an incompatible edit) gives Error; NONE→FULL with the unchanged text gives Warning; and on :18082 the validator user can call the lookup (if it gets 403, handle it as in Task 6 step 5).
- [ ] **Step 6: Run the integration suite.** Expected: pass.
- [ ] **Step 7: Update the docs and commit**: `validator: a level change and a schema change need separate saves (audit F4)`.

### Task 8: Whole-branch verification

**Files:**
- Modify: `VERIFICATION.md` (an "Independent audit, 2026-11-17" section: one row per finding with the take, the change and the test), `README.md` (counts)

- [ ] **Step 1: Unit suite and coverage.** Expected: all pass, 100% line and branch.
- [ ] **Step 2: Integration suite.** Expected: all pass.
- [ ] **Step 3: Differential run**: `.venv/bin/python integration/differential.py --cases 700 --seed 4 --out /tmp/differential.tsv`. Expected: exit 0, with 0 false passes and 0 internal errors.
- [ ] **Step 4: Java probes** (`probe.Main`, `probe.ProtoRefs`, `probe.Nested`, using the README command). Expected: all exit 0.
- [ ] **Step 5: Re-run the reviewer's scripts against this code**: t05, t06, t07, t12 and t17 with `PYTHONPATH` pointing at `validator/`, skipping their Java parts. Expected: F1 cases give Error, J2 gives Error, J3 and J5 pass, C3 gives Error, and C4 (READONLY) gives Error.
- [ ] **Step 6: Messages check**: every constant in `messages.py` appears word for word in README (`.venv/bin/python -c "import re,schema_validator.messages as m; t=open('README.md').read(); print([k for k,v in vars(m).items() if k.isupper() and isinstance(v,str) and not all(s in t for s in re.split(r'\{[^}]*\}',v) if len(s)>8)])"` prints `[]`).
- [ ] **Step 7: Request a whole-branch review** (superpowers:requesting-code-review) against this plan and the audit report. Fix what it confirms.
- [ ] **Step 8: Commit**: `validator: verification record for the independent audit`.
