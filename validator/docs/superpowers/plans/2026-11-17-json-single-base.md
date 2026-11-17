# JSON Schema Single-Base Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three open JSON Schema problems from the audit-fixes final review by making the ambiguous forms invalid: `$id` and `$schema` only at the top level, `$schema` required, and no top-level `$id` beside a top-level `$ref` in draft-07 and earlier.

**Architecture:** `schema_validator/formats/json_schema.py` keeps its broad keyword walk. The walk now also reports nested `$id` (`id` in draft-04) and nested `$schema`. With those forbidden there is one base URI, the top-level `$id`, so every `$ref` resolves with one `urljoin`. The `referencing`-based resolution added in commit 0c71a92 is removed.

**Tech Stack:** Python 3.11, jsonschema 4.26.0, pytest, coverage. Integration: the `validator-it` stack (registry :18081, Kafka :19092) and confluent-kafka 2.16.1.

**Spec:** `schema-validator-plan.md` §5.2. This plan replaces its steps 5, 6 and 8. The owner approved rules A, B and C and the root-`$ref` edge case (rule D) on 2026-11-17. The problems are described in the ledger `.superpowers/sdd/2026-11-17-audit-fixes/progress.md`, lines "R1", "R2" and "New pre-existing finding".

## Global Constraints

- Every path is relative to `validator/`, and every command runs from `validator/`. Python is `.venv/bin/python`.
- Runtime dependencies are unchanged. After this plan `json_schema.py` imports nothing from `referencing`.
- Every user-facing message is a constant in `schema_validator/messages.py`, with the exact text given here. `README.md` lists every message word for word.
- `validate` never raises. The worst failure is a false pass: when in doubt, return Error.
- Unit coverage stays at 100% line and branch: `.venv/bin/python -m coverage run --branch --source=schema_validator -m pytest tests -q && .venv/bin/python -m coverage report --fail-under=100 --show-missing`.
- Integration: `.venv/bin/python -m pytest integration/tests -q` with the stack up. Use only uniquely prefixed subjects.
- Git: stage only the files the task names, never `git add -A`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push.

## Review Focus

1. A property *named* `$id`, `id` or `$schema` (under `properties`, `patternProperties`, `definitions`, `$defs`), and those keys inside `enum`, `const`, `default` or `examples` data, must not be flagged. Task 1, `test_keyword_names_as_property_names_or_data_are_not_flagged`.
2. A nested key is flagged only when it is the keyword in that draft: a nested `$id` in draft-04 and a nested `id` in draft-07 are ordinary unknown keywords. Task 1, `test_only_the_drafts_own_id_keyword_is_flagged`.
3. The audit-fixes blow-up input (a long `$id` chain under `x-defs`) must give one short error, fast. Task 1, `test_long_nested_id_chain_is_one_error_and_fast`.
4. A fragment-only top-level `$id` (`"#foo"`, draft-07) leaves the base empty, so `#/definitions/...` refs stay internal and `address.json` needs the row `address.json`. Task 1, `test_fragment_only_root_id_keeps_the_empty_base`.
5. A schema that breaks a rule also gets no D4/D5 noise: `external_refs` is None after a rule A, B or D error. Task 1, `test_rule_errors_skip_reference_checks`.

---

### Task 1: The rules, single-base resolution, tests and docs

**Files:**
- Modify: `schema_validator/formats/json_schema.py`, `schema_validator/messages.py`
- Test: `tests/test_json_schema.py`, `tests/test_validate.py` (only where a JSON request lacks `$schema`), `integration/tests/test_python_clients.py`, `integration/tests/test_validate_real_sr.py` (only where a JSON schema lacks `$schema`), `integration/differential.py` (only if a JSON seed lacks `$schema`)
- Docs: `schema-validator-plan.md` §5.2 steps 5, 6 and 8, `README.md` (the messages and the JSON rules list), `VERIFICATION.md` (one entry for these rules, citing the three examples)

**Interfaces:**
- Produces these messages. `{paths}` is up to 3 JSON Pointers (RFC 6901: `~` → `~0`, `/` → `~1`, array items by index), sorted, each in single quotes, joined with `", "`, followed by `" and {n} more"` when there are more than 3. `{kw}` is the keyword as written (`$id`, or `id` in draft-04).
  - `JSON_SCHEMA_MISSING = 'JSON schema needs a "$schema", for example "$schema": "http://json-schema.org/draft-07/schema#".'` (rule C)
  - `JSON_NESTED_ID = 'Only the top level of a JSON schema may have "{kw}". Remove it from {paths}. A type that needs its own "{kw}" belongs in its own Schema, referenced with "$ref".'` (rule A)
  - `JSON_NESTED_SCHEMA = 'Only the top level of a JSON schema may have "$schema". Remove it from {paths}. A JSON schema uses one draft.'` (rule B)
  - `JSON_ROOT_ID_WITH_REF = 'In draft-07 and earlier, a "$ref" at the top level makes the "{kw}" next to it ignored. Move the "$ref" into "allOf".'` (rule D)
- The rules:
  - The id keyword is `id` when `$schema` (without a trailing `#`) is draft-04, otherwise `$id`, including when `$schema` is missing.
  - **Rule A:** every object the existing walk reaches, other than the root, that has the id keyword as a key, whatever its value.
  - **Rule B:** the same for the `$schema` key.
  - **Rule C:** the root has no `$schema`.
  - **Rule D:** `$schema` is draft-04, -06 or -07, and the root has both `$ref` and the id keyword.
  - Errors are added after the step 6 errors, in the order C, D, A, B.
  - The step 6 meta-schema check is unchanged; it still runs with the draft-07 default when `$schema` is missing.
- Resolution:
  - `base` is `urldefrag(urljoin("", root_id)).url` for a string root id, otherwise `""`.
  - A `$ref` target is `urldefrag(urljoin(base, ref)).url`. It is internal when it is `""` or equals `base`; otherwise it is an external ref.
  - A malformed root id or `$ref` keeps the existing `JSON_INVALID` "is not a valid URI" error, with `external_refs=None`.
  - `external_refs` is None after any rule A, B or D error. Rule C alone still returns the refs.
- Remove `_readings`, `_id_of`, `_subschema_ids`, `_dicts` and every `referencing` import.

- [ ] **Step 1: Write the failing unit tests** in `tests/test_json_schema.py`. D7 below is `http://json-schema.org/draft-07/schema#`.
  - `test_missing_schema_keyword_is_an_error`: `{"title":"a.B","type":"object","properties":{"t":{"type":"array","items":[{"type":"string"}]}}}` (issue 3) → `JSON_SCHEMA_MISSING` is in the errors.
  - `test_nested_schema_keyword_is_an_error`:
    - Schema (issue 2): root `$schema` 2020-12, `$id` `https://acme.example/o.json`, `properties.p = {"$schema": D7, "properties": {"t": {"$id": "https://acme.example/sub/", "$ref": "a.json"}}}`.
    - Expected errors: exactly `[JSON_NESTED_ID with kw="$id", paths="'/properties/p/properties/t'"; JSON_NESTED_SCHEMA with paths="'/properties/p'"]`, and `external_refs is None`.
  - `test_nested_id_under_unknown_keyword_is_an_error`: draft-07, root `$id` `https://acme.example/o.json`, `"x-defs": {"A": {"$id": "https://acme.example/a.json", "type": "string"}}` → `JSON_NESTED_ID` with paths `'/x-defs/A'`.
  - `test_long_nested_id_chain_is_one_error_and_fast`: 2,000 nested `{"$id": "dN/", "x": ...}` under `x-defs`, with a `$ref` at the bottom. Expect exactly one `JSON_NESTED_ID` whose paths end with `" and 1997 more"`, and `analyse` taking under 1 s (`time.perf_counter`).
  - `test_more_than_three_paths_are_summarised`: 5 nested `$id`s → the 3 sorted paths, then ` and 2 more`.
  - `test_root_id_beside_root_ref_in_draft07_is_an_error`: draft-07 root with `$id`, `$ref` `#/definitions/O`, `"type": "object"` → `JSON_ROOT_ID_WITH_REF` with kw `$id`. The same in 2020-12 → no such error.
  - `test_keyword_names_as_property_names_or_data_are_not_flagged`: `properties` keys `$id`, `id` and `$schema`, and an `enum`/`const`/`default`/`examples` containing `{"$id": "x", "$schema": "y"}` → no rule errors.
  - `test_only_the_drafts_own_id_keyword_is_flagged`: draft-04 with a nested `$id` → no rule A error. Draft-04 with a nested `id` → rule A with kw `id`. Draft-07 with a nested `id` → no rule A error.
  - `test_fragment_only_root_id_keeps_the_empty_base`: draft-07, root `$id` `#foo`, refs `#/definitions/x` and `address.json` → `external_refs == {"address.json"}`.
  - `test_rule_errors_skip_reference_checks`: a nested `$id` plus a `$ref` `other.json` → `external_refs is None`. Missing `$schema` plus `$ref` `other.json` → `external_refs == {"other.json"}`.
  - Keep J2 (`test_relative_ref_under_id_resolves_to_absolute`), J5b (`test_ref_to_own_id_with_fragment_is_internal`), the malformed-URI tests, `test_draft04_uses_id` (root `id`) and every test of the walk.
  - Change the tests whose shapes now break a rule so that they expect the rule's error: J5 `test_ref_to_internal_id_is_internal`, `test_nested_id_moves_the_base`, `test_fragment_id_keeps_the_base`, the `UNREGISTERED_ID_SHAPES` tests, `test_id_beside_ref_moves_the_base_from_2019_09`, `test_base_under_an_unknown_keyword_depends_on_how_the_ref_is_reached`, `test_same_document_ref_under_an_unregistered_id_is_external`, `test_id_under_a_nested_schema_keyword_is_not_internal`, `test_id_with_a_fragment_is_registered_with_it`, `test_keyword_of_the_wrong_type_for_one_reading_does_not_raise`, `test_without_schema_keyword_both_readings_need_a_reference` and `test_no_schema_keyword_uses_draft7`. Delete one only when its shape is now covered by a new test above, and say which in the report.
- [ ] **Step 2: Run them and check that they fail**: `.venv/bin/python -m pytest tests/test_json_schema.py -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement it** in `json_schema.py`:
  - `_walk(schema: dict[str, Any], id_keyword: str) -> tuple[list[str], list[str], list[str]]`: the string `$ref` values, the JSON Pointers of nested objects with the id keyword, and the pointers of nested objects with `$schema`. It is iterative, and it visits the same nodes as today's walk.
  - `_paths(pointers: list[str]) -> str`: the `{paths}` format.
  - Add the four messages to `messages.py`.
- [ ] **Step 4: Fix the requests and schemas the rules now reject.** Run `.venv/bin/python -m pytest tests -q`. Every JSON schema in a test, fixture or seed that lacks `$schema` and is not testing rule C gets `"$schema": "http://json-schema.org/draft-07/schema#"`. Expected: all pass. Then run the coverage command; expected 100%.
- [ ] **Step 5: Add the integration tests** in `test_python_clients.py`. Each validates on :18081 and then shows the real client failure that the rule prevents (register the schema directly first where the client needs it).
  - `test_json_without_schema_keyword_is_rejected` (issue 3): `validate` gives `JSON_SCHEMA_MISSING`, and `JSONSerializer` fails with `SchemaError`.
  - `test_json_nested_schema_keyword_is_rejected` (issue 2): `validate` gives `JSON_NESTED_ID` and `JSON_NESTED_SCHEMA`. With the row `https://acme.example/sub/a.json`, the serializer fails with `Unresolvable`.
  - Change `test_json_internal_id_ref_works` (J5) into `test_json_nested_id_is_rejected`, and make `test_json_id_under_a_keyword_the_draft_does_not_know_is_external` expect `JSON_NESTED_ID`.
  - Keep `test_json_id_absolute_ref_string_works` (J3) and `test_json_id_relative_ref_is_rejected` (J2) passing.
- [ ] **Step 6: Run the integration suite**: `.venv/bin/python -m pytest integration/tests -q`. Expected: all pass.
- [ ] **Step 7: Update the docs.**
  - Spec §5.2: step 5 becomes "`$schema` required and in `allowed_json_drafts`". Add the new steps for rules A, B and D. Step 8 becomes the single-base resolution, and the `referencing` text goes. Keep the "Verified:" sentences that still hold.
  - README: the four messages word for word, and the JSON rules list.
  - VERIFICATION: one entry with the three examples, what each client does with them, and the tests that pin them.
- [ ] **Step 8: Commit**: `validator: JSON $id and $schema only at the top level; $schema required`.

### Task 2: Verification

**Files:**
- Modify: `VERIFICATION.md` (test counts, coverage figures, one differential line)

- [ ] **Step 1: Unit suite and coverage.** Expected: all pass, 100% line and branch.
- [ ] **Step 2: Integration suite.** Expected: all pass.
- [ ] **Step 3: Differential run.** Run `.venv/bin/python integration/differential.py --cases 700 --seed 6 --out /private/tmp/claude-501/-Users-noamram-Projects-acl-schema-registry-validator/2ea86272-c3ba-4883-9a42-ff70e358f6a6/scratchpad/differential-json.tsv; echo exit=$?`. Expected: `exit=0`, and no `FALSE-PASS` or `INTERNAL` rows (`cut -f1 <tsv> | sort | uniq -c`).
- [ ] **Step 4: Messages check.** Run `.venv/bin/python -c "import re,schema_validator.messages as m; t=open('README.md').read(); print([k for k,v in vars(m).items() if k.isupper() and isinstance(v,str) and not all(s in t for s in re.split(r'\{[^}]*\}',v) if len(s)>8)])"`. Expected: it prints `[]`.
- [ ] **Step 5: No leftover dependency.** Run `grep -n referencing schema_validator/formats/json_schema.py`. Expected: no output.
- [ ] **Step 6: Commit**: `validator: verification record for the JSON top-level rules`.
