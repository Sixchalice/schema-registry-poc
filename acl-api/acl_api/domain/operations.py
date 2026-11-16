"""The operations sr-acl-cli can grant (from `sr-acl-cli --help`, cp-schema-registry 7.9.0)."""

from collections.abc import Iterable
from enum import StrEnum

WILDCARD = "*"


class Operation(StrEnum):
    SUBJECT_READ = "SUBJECT_READ"
    SUBJECT_WRITE = "SUBJECT_WRITE"
    SUBJECT_DELETE = "SUBJECT_DELETE"
    SUBJECT_COMPATIBILITY_READ = "SUBJECT_COMPATIBILITY_READ"
    SUBJECT_COMPATIBILITY_WRITE = "SUBJECT_COMPATIBILITY_WRITE"
    GLOBAL_COMPATIBILITY_READ = "GLOBAL_COMPATIBILITY_READ"
    GLOBAL_COMPATIBILITY_WRITE = "GLOBAL_COMPATIBILITY_WRITE"
    GLOBAL_READ = "GLOBAL_READ"

    @property
    def is_subject_operation(self) -> bool:
        return self.name.startswith("SUBJECT_")


SUBJECT_OPERATIONS = tuple(op for op in Operation if op.is_subject_operation)
GLOBAL_OPERATIONS = tuple(op for op in Operation if not op.is_subject_operation)

# Another name the CLI accepts for an operation.
_ALIASES = {"GLOBAL_SUBJECTS_READ": Operation.GLOBAL_READ}


def resolve_operations(names: Iterable[str]) -> frozenset[Operation]:
    """The operations the CLI applies for these names: case-insensitive, with its alias, and '*'
    meaning all. Unknown names are ignored, because the CLI rejects them before this is used."""
    resolved: set[Operation] = set()
    for name in names:
        if name == WILDCARD:
            return frozenset(Operation)
        upper = name.upper()
        if upper in _ALIASES:
            resolved.add(_ALIASES[upper])
        elif upper in Operation.__members__:
            resolved.add(Operation[upper])
    return frozenset(resolved)
