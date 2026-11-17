# Reference versions: what the platform must change

For the platform team. The validator library now needs a version in every reference row. A request without versions fails, so ship the library upgrade and the changes below in the same platform release.

## What to change

1. **Add a version dropdown to each reference row.** Fill it from the registry: `GET /subjects/{subject}/versions`. On create, preselect the latest version; the user can pick another. There is no blank and no "latest" choice.
2. **Preselect on edit from the registry, not from platform storage.** `GET /subjects/{subject}/versions/latest` for the schema being edited lists `references[].version` for each reference name. That is the single source of truth. It needs no migration for schemas registered before this change. Storing the version on the platform side is optional.
3. **Send the chosen version to the validator** as `ReferenceInput.version` (an integer of 1 or more).
4. **Register with exactly what the user chose:** `{name, subject, version}` per reference. Do not look up the latest version at registration.
5. **Keep the reference rows in the registry's order.** On edit, keep the rows in the order of the latest version's `references[]`, matching each row to its entry by subject (not by name). A row added during an edit goes at the end and preselects the latest version. Register in row order. The registry's lookup (`POST /subjects/{s}`) compares references in order: the same schema with its references reversed is not found (404, 40403), so a reordered save registers an identical new version.
6. **Producers and consumers:** no configuration change. Producers with `use.latest.version` use the references stored with the latest version of their own subject; consumers use the references of the writer schema's version (found by schema id). A producer that builds its own copy of the schema (a Java Avro producer with referenced records copied in, or classes generated from a referenced subject) should build from those pinned versions, not from the referenced subject's latest. Checked with `probe.Pinned`: the Java `KafkaAvroSerializer` with `use.latest.version` does not fail when the copy has a newer version of the referenced record. It writes with the registered (pinned) schema and silently leaves out the fields the pinned version lacks.

## Messages users can see

- No version sent (a platform bug, not reachable from the dropdown): `Select a version for reference '{s}'.`
- The version does not exist: `'{s}' has no version {v} (the latest is {latest}).`
- A newer version exists (a warning): `References will be pinned to: {list}.` where each entry is `S vN` or `S vN (latest is vM)`, followed by ` To update one, select a newer version for it.` when at least one is not at its latest.
- Level and schema changed in one save: `The compatibility level and the schema cannot change in the same save. Schema Registry checks a new version under the level in force when it is registered (now '{sr_level}'). Save the new level '{compatibility}' with the schema and its reference versions unchanged first, then change the schema.` A changed reference version counts as a schema change. It appears when the level in force differs from the new one and the schema, its reference rows or their versions differ from the latest registered version.
- Level changed and the references only reordered: `The references are listed in a different order than in the registered version ({order}). Keep that order, so that only the compatibility level changes.` `{order}` is the registered order, as `S vN` joined by `, `. It appears when the rows hold the same references and versions as the latest version but in another order.
- One subject used at two versions (three variants; each tells the user which version to select):
  - When one of the versions is this schema's own row and it is the highest: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Select the same version for '{s}' as the Schemas it comes through, or update those Schemas to v{chosen}: select v{chosen} for '{s}' in their references and save them.`
  - When this schema's own row is not the highest version: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Select v{latest} for '{s}', the version the Schemas it comes through use.`
  - When none of the versions is this schema's own row: `'{s}' is used at more than one version: {uses}. A schema can use only one version of it. Update the Schemas that use an older version to v{latest}: select v{latest} for '{s}' in their references and save them. Then save this schema.`

## Why

- **Predictable edits.** Before, every save pinned each reference to the latest version, so re-saving unchanged text could silently register a new version that used another team's newer referenced schema. Now what the user sees in the form is what gets registered, and a save that changes nothing registers nothing new.
- **The level-change gap is closed.** An edit that only changed the compatibility level could register a new version (with newer reference versions) checked only under the old level. The validator now treats a changed reference version as a schema change and asks for separate saves.
- **Version conflicts are fixable by the user.** The conflict messages say which version to select; the user no longer has to ask another team to re-save their schema.

Not covered: a definition that exists only in a newer version of a reference (for example a JSON `address.json#/definitions/Zip` added in v2) is still rejected by the registry, without a hint. The validator does not update references automatically.
