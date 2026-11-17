# Reference Versions Per Row Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every reference row carries the version the user picked, and `validate` checks and uses exactly that version, says when a newer one exists, and treats a changed version as a schema change in the level-change rule.

**Architecture:** `ReferenceInput` gains a `version`. Stage A requires it. Stage D (`references.resolve`) fetches the chosen version instead of the latest, still reads the latest for the hint, and reports an unknown version. The pinned warning shows `(latest is vM)`. The F1 conflict message gets a form for when this schema's own row is one of the conflicting versions. Stage E's level-change rule (E6) compares `(name, subject, version)`.

**Tech Stack:** Python 3.11, pytest, coverage. Integration: the `validator-it` stack (registry :18081, secured :18082, Kafka :19092) and confluent-kafka 2.16.1.

**Spec:** `docs/superpowers/specs/2026-11-17-reference-versions-design.md` (approved 2026-11-17), which changes `schema-validator-plan.md`.

## Global Constraints

- Every path is relative to `validator/`, and every command runs from `validator/`. Python is `.venv/bin/python`.
- Runtime dependencies are unchanged.
- Every user-facing message is a constant in `schema_validator/messages.py`, with the exact text given here. `README.md` lists every message word for word, so update README in the same commit as a message change.
- `validate` never raises. The worst failure is a false pass: when in doubt, return Error.
- Unit coverage stays at 100% line and branch: `.venv/bin/python -m coverage run --branch --source=schema_validator -m pytest tests -q && .venv/bin/python -m coverage report --fail-under=100 --show-missing`.
- Integration: `.venv/bin/python -m pytest integration/tests -m integration -q` with the stack up. Use uniquely prefixed subjects only. Never change a global level or mode.
- Git: stage only the files you changed, never `git add -A`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push.

## Review Focus

1. A row whose version is the latest costs one registry call, not two. Task 1, `test_row_at_latest_fetches_only_latest`.
2. A version above the latest (7 when the latest is 3) is "no version 7", not a crash. Task 1, `test_version_above_latest_is_not_found`.
3. A soft-deleted version: the registry answers 404 / 40402 for it, so it is "not found". Task 1, the integration test `test_soft_deleted_version_is_not_found`.
4. The walk follows the pinned version's own references, not the latest version's. Task 1, `test_walk_follows_the_pinned_versions_references`.
5. A Protobuf row keeps its version when `normalise_rows` derives its import path. Task 1, `test_protobuf_row_keeps_its_version`.

---

### Task 1: The version in the row, stage A, stage D and the warning

**Files:**
- Modify: `schema_validator/models.py` (`ReferenceInput`), `schema_validator/rules.py` (A5), `schema_validator/references.py` (`normalise_rows`, `Resolution`, `resolve`, `pinned_warning`), `schema_validator/validator.py` (pass the latest versions to `pinned_warning`), `schema_validator/messages.py`
- Test: `tests/test_rules.py`, `tests/test_references.py`, `tests/test_validate.py`, every test and helper that builds a `ReferenceInput` (`tests/`, `integration/tests/`, `integration/differential.py`), `integration/tests/test_validate_real_sr.py`, `integration/tests/test_python_clients.py`
- Docs: `schema-validator-plan.md`:
  - §2 "Reference versions" row: "Chosen by the user per reference row (a dropdown of the subject's versions). The platform registers with exactly that version."
  - §3.1 `ReferenceInput`, §4 stage A and D tables, §6, and §11 step 3 (register with the row's version, no lookup of the latest).
  - §12 (client guidance): a producer that builds its own copy of a schema with references (a Java Avro producer with referenced records copied into its schema, or classes generated from a referenced subject) must build from the version pinned in the registered version of its own subject, not from the referenced subject's latest version. Python with `use.latest.version` does this itself.
  - Also `README.md` (input, messages) and `VERIFICATION.md` (one entry).

**Interfaces:**
- Produces:
  - `ReferenceInput(schema_name: str, ref_string: str | None = None, version: int | None = None)`. `None` exists only so stage A can report a missing version.
  - `Resolution.latest: dict[str, int]`, the latest version of each row's subject, with default `{}`.
  - `pinned_warning(resolved: Sequence[SrReference], latest: Mapping[str, int]) -> str`.
- New and changed messages:
  - `REF_VERSION_REQUIRED = "Select a version for reference '{s}'."` (stage A; it sets `references_ok=False`). It is reported once per row whose `version` is `None`, a `bool`, not an `int`, or below 1, in row order, after the duplicate messages.
  - `REF_VERSION_NOT_FOUND = "'{s}' has no version {v} (the latest is {latest})."` (stage D, in place of the walk for that row).
  - `PINNED_WARNING = "References will be pinned to: {list}."` when every reference is at its latest version, and `PINNED_WARNING_NEWER = "References will be pinned to: {list}. To update one, select a newer version for it."` when at least one is not. Each `{list}` entry is `{subject} v{n}`, or `{subject} v{n} (latest is v{m})` when `n < m`, joined with `", "`, in row order.
  - `REF_VERSION_CONFLICT_DIRECT = "'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Select the same version for '{s}' as the Schemas it comes through, or update those Schemas to v{chosen}: select v{chosen} for '{s}' in their references and save them."` It is used when one of the conflicting versions has the row itself as a parent; `{chosen}` is that version.
  - `REF_VERSION_CONFLICT` (no row of this schema among the conflicting versions) becomes `"'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Update the Schemas that use an older version to v{latest}: select v{latest} for '{s}' in their references and save them. Then save this schema."` Re-saving no longer moves a reference to the latest, so the old advice ("save them again") would not work.
- `resolve`, per row:
  1. `latest = get_latest(subject)`. If it is None, D1 as today.
  2. If `row.version == latest.version`, use `latest`. Otherwise `pinned = get_version(subject, row.version)`, and if that is None give `REF_VERSION_NOT_FOUND` and skip the row.
  3. D2 uses the pinned version's `schema_type`.
  4. `resolved` gets `SrReference(key, subject, row.version)`, `uses` records `row.version`, and the walk starts from the pinned version.
  5. `latest[subject] = latest.version`.

- [ ] **Step 1: Write the failing tests.**
  - `test_missing_version_is_required` (parametrized over `None`, `0`, `-1`, `True`, `"2"`): `check_input` gives `REF_VERSION_REQUIRED` with `s` the schema name, and `references_ok is False`.
  - `test_pinned_older_version_is_used`: `Money` has v1 and v2, the row is at v1 → `resolved == [SrReference("a.Money", "a.Money", 1)]` and `referenced` holds v1's text.
  - `test_row_at_latest_fetches_only_latest`: the row is at v2, the latest → the subject's only fake call is `get_latest`.
  - `test_version_above_latest_is_not_found`: the row is at v7 with the latest v3 → errors `["'a.Money' has no version 7 (the latest is 3)."]` and `resolved == []`.
  - `test_walk_follows_the_pinned_versions_references`: `Line` v1 refs `Money` v1, and `Line` v2 refs `Money` v2. The row is `Line` at v1 → only `Money` v1 is in `uses` (no conflict), and `referenced` holds `Money` v1's text.
  - `test_protobuf_row_keeps_its_version`: `normalise_rows("PROTOBUF", [ReferenceInput("a.B", None, 3)])` → the row's version is 3.
  - `test_pinned_warning_shows_newer_versions`: `pinned_warning([SrReference("a.M", "a.M", 1), SrReference("a.A", "a.A", 3)], {"a.M": 3, "a.A": 3})` → `"References will be pinned to: a.M v1 (latest is v3), a.A v3. To update one, select a newer version for it."`
  - `test_pinned_warning_all_latest_has_no_update_hint`: `pinned_warning([SrReference("a.A", "a.A", 3)], {"a.A": 3})` → `"References will be pinned to: a.A v3."`
  - `test_version_conflict_without_own_row_says_update_them`: the two-transitive-paths case → the new `REF_VERSION_CONFLICT` text with `latest=2`.
  - `test_version_conflict_with_own_row_suggests_selecting`: rows `Line` (v1, which refs `Money` v1) and `Money` at v2 → the `REF_VERSION_CONFLICT_DIRECT` text with `uses="v1 through 'a.Line', v2 directly"` and `chosen=2`.
- [ ] **Step 2: Run them and check that they fail**: `.venv/bin/python -m pytest tests -q`.
- [ ] **Step 3: Implement the interfaces above.**
- [ ] **Step 4: Give every existing `ReferenceInput` a version.** Use the version the test registered, usually 1. Where a test relied on "latest", use the latest number it created. Run the unit suite, then the coverage command. Expected: pass, 100%.
- [ ] **Step 5: Integration tests** in `test_validate_real_sr.py`:
  - `test_older_pinned_version_registers_and_produces`: `Money` v1 and v2. A row at v1 → Warning naming `Money v1 (latest is v2)`. Register with that version, and the Python serializer produces.
  - `test_unknown_version_is_not_found`: a row at v9 → `REF_VERSION_NOT_FOUND`.
  - `test_soft_deleted_version_is_not_found`: register v1 and v2 of an unreferenced subject, soft-delete v1 (`DELETE /subjects/{s}/versions/1`), and a row at v1 gives `REF_VERSION_NOT_FOUND` with `latest` 2.
  - Also update the existing integration tests and `integration/differential.py` to send versions.
  - Run the integration suite. Expected: all pass.
- [ ] **Step 6: Update the docs and commit**: `validator: the user picks each reference's version`. Include the two untracked files, `docs/superpowers/specs/2026-11-17-reference-versions-design.md` and this plan.

### Task 2: The level-change rule compares reference versions

**Files:**
- Modify: `schema_validator/validator.py` (the E6 block)
- Test: `tests/test_validate.py`, `integration/tests/test_validate_real_sr.py`
- Modify also: `schema_validator/messages.py` (`LEVEL_AND_SCHEMA_CHANGED` becomes `"The compatibility level and the schema cannot change in the same save. Schema Registry checks a new version under the level in force when it is registered (now '{sr_level}'). Save the new level '{compatibility}' with the schema and its reference versions unchanged first, then change the schema."`; update the tests that assert the old text)
- Docs: `schema-validator-plan.md` §4 (the E6 paragraph: "unchanged" now includes the versions, and the known gap is closed), `README.md` (the E6 row, and the new message text), `VERIFICATION.md` (one entry)

**Interfaces:**
- Consumes: `Resolution.resolved` with the chosen versions (Task 1).
- The rule: on an edit whose level differs, with a latest version present, the save counts as unchanged when both hold:
  - `{(r.name, r.subject, r.version) for r in latest.references} == {(r.name, r.subject, r.version) for r in resolution.resolved}`;
  - `sr.is_registered(name, request.schema, schema_type, resolution.resolved)`.

  Otherwise the result is `LEVEL_AND_SCHEMA_CHANGED`, with no compatibility call. Remove the code comment about the version gap.

- [ ] **Step 1: Write the failing tests** in `tests/test_validate.py`:
  - `test_level_change_with_a_newer_reference_version_is_an_error`: `Order` v1 is registered with `Money` v1. `Money` now has v2. An edit changes the level and selects `Money` v2 with the same text → `LEVEL_AND_SCHEMA_CHANGED` and no `check_compatibility` call.
  - `test_level_change_with_the_same_reference_version_warns`: the same setup, but `Money` stays at v1 → Warning with `LEVEL_WARNING`.
  - Rewrite `test_lookup_uses_latest_versions_own_references` as `test_lookup_uses_the_selected_versions`: `is_registered` receives `resolution.resolved`.
- [ ] **Step 2: Run them and check that they fail.**
- [ ] **Step 3: Implement the rule.**
- [ ] **Step 4: Run the unit suite and coverage.** Expected: pass, 100%.
- [ ] **Step 5: Integration test** `test_level_change_gap_is_closed` in `test_validate_real_sr.py`:
  - `Money` v1, then `Order` v1 referencing `Money` v1, with the subject level NONE (`set_subject_level` fixture). Then register `Money` v2, which renames a field.
  - Edit `Order` with FULL, the same text and `Money` v1 → Warning.
  - The same edit with `Money` v2 → `LEVEL_AND_SCHEMA_CHANGED`.
  - Run the integration suite. Expected: all pass.
- [ ] **Step 6: Update the docs and commit**: `validator: a changed reference version is a schema change for the level rule`.

### Task 3: Platform note and verification

**Files:**
- Create: `docs/platform-reference-versions.md`
- Modify: `VERIFICATION.md` (test counts, coverage figures, one differential line)

- [ ] **Step 1: Write the platform note:**
  - **The dropdown:** one per reference row, filled from `GET /subjects/{s}/versions`, with the latest preselected on create.
  - **Preselection on edit** comes from the registry, not from platform storage: `GET /subjects/{subject}/versions/latest` lists `references[].version` for each reference name. That is the single source of truth, and it needs no migration for schemas registered earlier. Storing the version on the platform side is optional.
  - **The request:** send the chosen version as `ReferenceInput.version`.
  - **Registration:** register with exactly `{name, subject, version}`, with no lookup of the latest.
  - **Rollout:** a request without versions now fails with `Select a version for reference '{s}'.`, so upgrade the validator library in the same platform release that adds the dropdown.
  - **Client guidance:** producers build from the pinned versions (the spec §12 line).
  - **New messages:** the ones the user can see.
  - **Why:** predictable edits, and the level-change gap closed.
- [ ] **Step 2: Unit suite and coverage.** Expected: all pass, 100%.
- [ ] **Step 3: Integration suite.** Expected: all pass.
- [ ] **Step 4: Differential run.** Run `.venv/bin/python integration/differential.py --cases 400 --seed 9 --out /private/tmp/claude-501/-Users-noamram-Projects-acl-schema-registry-validator/2ea86272-c3ba-4883-9a42-ff70e358f6a6/scratchpad/differential-versions.tsv; echo exit=$?`. Expected: `exit=0`, with 0 `FALSE-PASS` and 0 `INTERNAL` in the printed table.
- [ ] **Step 5: Messages check** (the command from VERIFICATION.md). Expected: it prints `[]`.
- [ ] **Step 6: Commit**: `validator: platform note and verification record for reference versions`.
