"""Merges ACL changes into fewer CLI calls without changing what gets written.

The CLI applies every combination of the principals, subjects and operations it is given, so changes
that differ in only one of those dimensions can share a call:
  1. same subjects, topics and operations: one call with all their principals
     (e.g. 20 principals asking for SUBJECT_READ on the same subject become one call);
  2. then, same principals and operations: one call with all their subjects and topics.
Changes that use '*' are never merged: the CLI reduces a list containing '*' to just '*', which
would silently drop the other values.

Only changes of the same action (all adds or all removes) may be merged, and only consecutive ones:
adds commute with adds and removes with removes, but an add and a remove of the same ACL don't.
"""

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass

from acl_api.domain.models import AclChange
from acl_api.domain.operations import WILDCARD


@dataclass(frozen=True)
class MergedChange:
    change: AclChange          # what to send to the CLI
    members: tuple[int, ...]   # positions of the original changes this call covers


def merge_changes(changes: Sequence[AclChange]) -> list[MergedChange]:
    singles = [MergedChange(change, (position,)) for position, change in enumerate(changes)]
    by_target = _merge(singles, key=_same_target, combine=_union_of_principals)
    return _merge(by_target, key=_same_principals, combine=_union_of_subjects)


def _same_target(change: AclChange) -> Hashable | None:
    if _uses_wildcard(change):
        return None
    return frozenset(change.subjects), frozenset(change.topics), _operations_key(change)


def _same_principals(change: AclChange) -> Hashable | None:
    # A change without subjects is either global-only or invalid. Merging it with one that has
    # subjects could make an invalid request look valid, so those stay as they are.
    if _uses_wildcard(change) or not (change.subjects or change.topics):
        return None
    return frozenset(change.principals), _operations_key(change)


def _union_of_principals(group: list[MergedChange]) -> AclChange:
    return group[0].change.model_copy(
        update={"principals": _ordered_union(merged.change.principals for merged in group)})


def _union_of_subjects(group: list[MergedChange]) -> AclChange:
    return group[0].change.model_copy(update={
        "subjects": _ordered_union(merged.change.subjects for merged in group),
        "topics": _ordered_union(merged.change.topics for merged in group),
    })


def _merge(items: list[MergedChange], key: Callable[[AclChange], Hashable | None],
           combine: Callable[[list[MergedChange]], AclChange]) -> list[MergedChange]:
    """Groups items with equal keys (None = never grouped), in order of first appearance."""
    groups: dict[Hashable, list[MergedChange]] = {}
    for position, item in enumerate(items):
        group_key = key(item.change)
        groups.setdefault(position if group_key is None else ("key", group_key), []).append(item)
    return [
        group[0] if len(group) == 1
        else MergedChange(combine(group), tuple(sorted(m for merged in group for m in merged.members)))
        for group in groups.values()
    ]


def _uses_wildcard(change: AclChange) -> bool:
    return WILDCARD in (*change.principals, *change.subjects, *change.topics)


def _operations_key(change: AclChange) -> frozenset[str]:
    return frozenset(operation.upper() for operation in change.operations)


def _ordered_union(lists: Iterable[list[str]]) -> list[str]:
    return list(dict.fromkeys(value for values in lists for value in values))
