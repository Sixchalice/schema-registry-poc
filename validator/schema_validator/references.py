"""Stage D: reference rows, their resolution in Schema Registry and the cross-check."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from schema_validator import messages
from schema_validator.models import ReferenceInput, proto_import_path
from schema_validator.sr_client import SchemaRegistryClient, SchemaVersion, SrReference

# Levels below the referenced row's own schema (which is level 0).
_MAX_DEPTH = 20


def normalise_rows(
    schema_type: str, references: Sequence[ReferenceInput]
) -> tuple[list[ReferenceInput], list[str]]:
    """Fill in each row's ref_string. Returns (rows, D6 errors).

    Gives the same value as `rules._effective_ref_string`, so stages A and D agree:
    AVRO always uses schema_name (stage A rejects any other text), PROTOBUF always
    derives it from schema_name, and JSON keeps the given value. A JSON row without a
    ref_string (None or empty) gets D6 and is left out. Stage A stops unsupported types
    before this stage, so anything that is not AVRO or PROTOBUF is handled as JSON.
    """
    rows: list[ReferenceInput] = []
    errors: list[str] = []
    for ref in references:
        if schema_type == "PROTOBUF":
            rows.append(ReferenceInput(
                ref.schema_name, proto_import_path(ref.schema_name), ref.version))
        elif schema_type == "AVRO":
            rows.append(ReferenceInput(
                ref.schema_name, ref.schema_name, ref.version))  # stage A: equal or empty
        elif not ref.ref_string:
            errors.append(messages.REF_STRING_REQUIRED.format(s=ref.schema_name))
        else:
            rows.append(ref)
    return rows, errors


@dataclass(frozen=True)
class Resolution:
    # One per row that passed D1, D2 and the version check:
    # (name=ref_string, subject=schema_name, version=the row's version).
    resolved: list[SrReference]
    # Reference name -> schema text, direct and transitive, dependencies first.
    # The first text stored under a name is kept.
    referenced: dict[str, str]
    errors: list[str]  # D1, D2, D3, REF_TOO_DEEP, then D7 conflicts
    # Reference name -> ref_string of the row it was first loaded for (a row maps to itself).
    provided_by: dict[str, str] = field(default_factory=dict)
    # Subject -> its latest version, for each row whose subject exists (D1 passed).
    latest: dict[str, int] = field(default_factory=dict)


def resolve(
    name: str, schema_type: str, rows: Sequence[ReferenceInput], sr: SchemaRegistryClient
) -> Resolution:
    """Spec §6 steps 2 to 4. `rows` come from `normalise_rows`.

    Every `SchemaRegistryError` raised by `sr` propagates to the caller.
    """
    resolved: list[SrReference] = []
    referenced: dict[str, str] = {}
    provided_by: dict[str, str] = {}
    errors: list[str] = []
    latest_versions: dict[str, int] = {}
    # Every (subject, version) fetched during this call, None when not registered.
    fetched: dict[tuple[str, int], SchemaVersion | None] = {}
    # D7: subject -> version -> parent subjects (None for this schema's own row), and
    # reference name -> subjects, over the whole graph.
    uses: dict[str, dict[int, set[str | None]]] = {}
    names: dict[str, set[str]] = {}

    def report(error: str) -> None:
        if error not in errors:
            errors.append(error)

    for row in rows:
        latest = sr.get_latest(row.schema_name)
        if latest is None:
            report(messages.REF_NOT_REGISTERED.format(s=row.schema_name))
            continue
        latest_versions[row.schema_name] = latest.version
        # Stage A guarantees an int >= 1 here.
        version = row.version if row.version is not None else latest.version
        if version == latest.version:
            chosen = latest
        else:
            found = sr.get_version(row.schema_name, version)
            if found is None:
                report(messages.REF_VERSION_NOT_FOUND.format(
                    s=row.schema_name, v=version, latest=latest.version))
                continue
            chosen = found
        if chosen.schema_type != schema_type:
            report(messages.REF_WRONG_FORMAT.format(
                s=row.schema_name, their_type=chosen.schema_type, my_type=schema_type))
            continue
        key = row.ref_string if row.ref_string is not None else row.schema_name
        resolved.append(SrReference(key, row.schema_name, version))
        fetched.setdefault((row.schema_name, version), chosen)
        uses.setdefault(row.schema_name, {}).setdefault(version, set()).add(None)
        names.setdefault(key, set()).add(row.schema_name)
        error = _walk(
            name, row.schema_name, key, chosen, sr, fetched, referenced, provided_by, report,
            uses, names)
        if error is not None:
            report(error)

    for subject, versions in sorted(uses.items()):
        if len(versions) > 1:
            own = [v for v, parents in versions.items() if None in parents]
            if own:
                report(messages.REF_VERSION_CONFLICT_DIRECT.format(
                    s=subject, uses=_describe_uses(versions), chosen=own[0]))
            else:
                report(messages.REF_VERSION_CONFLICT.format(
                    s=subject, uses=_describe_uses(versions), latest=max(versions)))
    for ref_name, subjects in sorted(names.items()):
        if len(subjects) > 1:
            report(messages.REF_NAME_CONFLICT.format(
                x=ref_name, subjects=", ".join(f"'{s}'" for s in sorted(subjects))))

    return Resolution(resolved=resolved, referenced=referenced, errors=errors, provided_by=provided_by,
        latest=latest_versions)


def _describe_uses(versions: dict[int, set[str | None]]) -> str:
    """`v1 through 'a.B', v2 directly`: one entry per version, ascending."""
    entries = []
    for version, parents in sorted(versions.items()):
        through = "' and '".join(sorted(p for p in parents if p is not None))
        if None not in parents:
            entries.append(f"v{version} through '{through}'")
        elif through:
            entries.append(f"v{version} directly and through '{through}'")
        else:
            entries.append(f"v{version} directly")
    return ", ".join(entries)


def _walk(
    name: str,
    row_subject: str,
    root_key: str,
    root: SchemaVersion,
    sr: SchemaRegistryClient,
    fetched: dict[tuple[str, int], SchemaVersion | None],
    referenced: dict[str, str],
    provided_by: dict[str, str],
    report: Callable[[str], None],
    uses: dict[str, dict[int, set[str | None]]],
    names: dict[str, set[str]],
) -> str | None:
    """Depth-first walk of one row's references, adding texts to `referenced` post-order.

    Returns the error that stopped the walk (D3 or REF_TOO_DEEP), or None. A missing
    nested schema (D1) is passed to `report` and the walk goes on with the others.
    A schema is walked again only when reached at a deeper level than before, so the
    depth limit applies to the longest path and the walk ends even on cycles that do
    not involve `name` (at most _MAX_DEPTH + 1 walks of each schema). Every edge is
    recorded in `uses` and `names` (D7), also when the target is not walked again.
    """
    if row_subject == name:
        return messages.REF_CYCLE.format(s=row_subject, name=name)
    deepest: dict[tuple[str, int], int] = {(row_subject, root.version): 0}
    stack: list[tuple[str, SchemaVersion, int, Iterator[SrReference]]] = [
        (root_key, root, 0, iter(root.references))
    ]
    while stack:
        key, schema, depth, children = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            referenced.setdefault(key, schema.schema)
            provided_by.setdefault(key, root_key)
            continue
        if child.subject == name:
            return messages.REF_CYCLE.format(s=row_subject, name=name)
        child_depth = depth + 1
        if child_depth > _MAX_DEPTH:
            return messages.REF_TOO_DEEP.format(s=row_subject)
        node = (child.subject, child.version)
        uses.setdefault(child.subject, {}).setdefault(child.version, set()).add(schema.subject)
        names.setdefault(child.name, set()).add(child.subject)
        if deepest.get(node, -1) >= child_depth:
            continue
        if node not in fetched:
            fetched[node] = sr.get_version(child.subject, child.version)
        found = fetched[node]
        if found is None:
            report(messages.REF_NOT_REGISTERED.format(s=child.subject))
            continue
        deepest[node] = child_depth
        stack.append((child.name, found, child_depth, iter(found.references)))
    return None


def through_rows(
    rows: Sequence[ReferenceInput], external_refs: set[str], provided_by: dict[str, str]
) -> set[str]:
    """AVRO: a name that only a reference of a reference provides counts as a use of the
    row it was loaded for, as the registry loads it with that row. A name that has a row
    of its own stays itself."""
    ref_strings = {row.ref_string for row in rows}
    return {x if x in ref_strings else provided_by.get(x, x) for x in external_refs}


def cross_check(
    schema_type: str, rows: Sequence[ReferenceInput], external_refs: set[str]
) -> list[str]:
    """D4 (sorted) then D5 (row order). Exact string match on ref_string."""
    ref_strings = {row.ref_string for row in rows}
    errors = [
        messages.REF_MISSING_ROW.format(x=x) for x in sorted(external_refs - ref_strings)
    ]
    for row in rows:
        if row.ref_string in external_refs:
            continue
        if schema_type == "PROTOBUF":
            errors.append(messages.REF_UNUSED_PROTOBUF.format(
                s=row.schema_name, path=row.ref_string))
        else:
            errors.append(messages.REF_UNUSED.format(x=row.ref_string))
    return errors


def pinned_warning(resolved: Sequence[SrReference], latest: Mapping[str, int]) -> str:
    entries = []
    newer = False
    for ref in resolved:
        newest = latest.get(ref.subject, ref.version)
        if ref.version < newest:
            newer = True
            entries.append(f"{ref.subject} v{ref.version} (latest is v{newest})")
        else:
            entries.append(f"{ref.subject} v{ref.version}")
    template = messages.PINNED_WARNING_NEWER if newer else messages.PINNED_WARNING
    return template.format(list=", ".join(entries))
