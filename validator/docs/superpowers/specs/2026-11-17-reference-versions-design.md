# Reference versions chosen per row: design

Date: 2026-11-17. Status: draft for the owner's approval. Changes `schema-validator-plan.md` (the validator spec).

## Why

Today a reference is always pinned to the referenced subject's latest version when a schema is saved. So re-saving a schema with unchanged text can silently create a new version that uses another team's newer referenced schema. That causes two problems:
- **The level-change gap:** an edit that only changes the compatibility level can register a new version (new reference versions) that is checked only under the old level.
- **F1 conflicts:** the user can't fix a version conflict themselves; they have to ask another team to re-save their schema.

Choosing the version per row makes every save predictable: what the user sees in the form is what gets registered.

## Goals

- Each reference row has a version, which the user picks from a dropdown of the referenced subject's registered versions. There is no blank or "latest" choice.
- On edit, the platform preselects in each row the version the registered schema uses, so an unrelated edit keeps the same versions.
- `validate` checks the chosen version, uses exactly that version for every stage D and E check, and says when a newer version exists.
- The level-change rule compares reference versions too, which closes the gap.
- An F1 conflict message tells the user how to fix it by choosing a version.

## Non-goals

- No "newer available" badge in the UI. The validator's warning carries that information. (The dropdown itself is the platform's.)
- No hint for a definition or message that only exists in a newer version of a reference (for example JSON `address.json#/definitions/Zip` added in Address v2). The registry still rejects such a schema in stage E; a precise hint can come later.
- No automatic updating of references.

## Anti-goals

- A re-save that changes nothing must never register a new version.
- A version conflict must never pass. The F1 check stays as strict as today.

## Approaches considered

1. **Keep "always latest", and compare reference versions in the level-change rule.** No UI change. But a level change then needs an extra save whenever a reference has moved on, and F1 conflicts still need another team.
2. **Keep the pins on edit, plus an "update references to latest" button.** Predictable, but the button, per-row update and a "newer available" indicator are extra UI.
3. **A version in the existing reference row (picked by the owner).** A dropdown of the subject's versions, always filled in, with the stored version preselected on edit. The validator's messages show when a newer version exists. This is the least UI (one more field in a row the UI already has) and it gives the user full control. A blank meaning "latest" was considered and rejected by the owner: every row names its version explicitly.

## Design

**Input.** `ReferenceInput` gains `version: int` (required).
- Stage A: a version that is missing (`None`), not an integer (a `bool` counts as not an integer) or below 1 is an error: `Select a version for reference '{s}'.` The dropdown makes this unreachable from the UI; the check protects against a platform bug.

**Stage D.**
- **Which version is used:** the row's version, always.
- **Lookups:** each row fetches its version (`get_version`) and the latest (`get_latest`), for the hint and for D1.
- **Not registered:** a subject that is not registered gives D1 as today.
- **Unknown version:** a version the subject doesn't have gives `'{s}' has no version {v} (the latest is {latest}).`
- **D2 and the walk** use the chosen version.
- **F1 (one version per subject):** unchanged as a check.
  - When one of the conflicting versions is this schema's own row and it is the highest, the message offers the fix the user can make: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Select the same version for '{s}' as the Schemas it comes through, or update those Schemas to v{chosen}: select v{chosen} for '{s}' in their references and save them.`
  - When this schema's own row is older than another conflicting version, selecting the older one for the others would be a downgrade, so: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Select v{latest} for '{s}', the version the Schemas it comes through use.`
  - Otherwise: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Update the Schemas that use an older version to v{latest}: select v{latest} for '{s}' in their references and save them. Then save this schema.` (Re-saving no longer moves a reference to the latest, so today's "save them again" advice would not work.)

**Stage E and warnings.**
- **Compatibility** is checked with exactly the chosen versions.
- **The warning** lists each reference as `S vN`, or `S vN (latest is vM)` when a newer one exists: `References will be pinned to: {list}.`, followed by ` To update one, select a newer version for it.` only when at least one reference is not at its latest.
- **Level-change rule (E6):** "unchanged" now means the same text, the same `(name, subject, version)` set as the latest registered version's references, and the registry finding that version (`is_registered`). Otherwise a level change gives the "separate saves" error, reworded to "…with the schema and its reference versions unchanged first…". This closes the gap.

**Platform side (not the validator; for the platform team):**
1. Add a version dropdown to each reference row, listing the referenced subject's registered versions (`GET /subjects/{s}/versions`). On create, preselect the latest; the user can pick another.
2. On edit, preselect from the registry: the latest registered version of the schema (`GET /subjects/{s}/versions/latest`) lists `references[].version`. That is the single source of truth, and it needs no migration. Storing it on the platform side is optional.
3. Send the version to `validate`, and register with exactly that version (no lookup of the latest at registration).
4. Upgrade the validator library in the same release as the dropdown, because requests without versions now fail.

**Producers and consumers:** no configuration change. Producers with `use.latest.version` use the references stored with the latest version of their own subject; consumers use the references of the writer schema's version (by schema id). A producer that builds its own copy of the schema (a Java Avro producer with referenced records copied in, or classes generated from a referenced subject) must build from those pinned versions, not from the referenced subject's latest. This goes into the client guidance (spec §12).

## Constraints

As in the validator spec: the message texts in `messages.py` and README word for word, 100% unit coverage, `validate` never raises, the worst failure is a false pass.

## Left to the builder

- Where in `references.resolve` the two lookups happen, and the exact `{uses}` formatting for the new F1 form.
- Which existing tests change because the warning text changes.
- Integration tests for:
  - an older pinned version that registers and that the Python client produces with;
  - a level change with an unchanged version (Warning);
  - a level change with a bumped version (Error);
  - an unknown version;
  - the "(latest is vN)" hint.
