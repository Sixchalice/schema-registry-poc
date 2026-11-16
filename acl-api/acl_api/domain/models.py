"""ACL change requests, the ACL state read from the CLI, and the views returned by the API."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from acl_api.domain.operations import WILDCARD, resolve_operations


def _printable(value: str) -> str:
    # Control characters (newlines in particular) would break parsing of the CLI's `--list` output.
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("values must be non-blank and contain no control characters")
    return value


CliValue = Annotated[str, AfterValidator(_printable)]


def _collapse_wildcard(values: list[str]) -> list[str]:
    """'*' anywhere means just '*', as in the CLI; otherwise de-duplicated, order kept."""
    return [WILDCARD] if WILDCARD in values else list(dict.fromkeys(values))


def _topic_subjects(topic: str) -> list[str]:
    return [WILDCARD] if topic == WILDCARD else [f"{topic}-key", f"{topic}-value"]


class AclChange(BaseModel):
    """Body of POST and DELETE /acls: the same arguments as sr-acl-cli --add/--remove."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"example": {
            "principals": ["c17.svc-a"], "topics": ["orders"], "operations": ["SUBJECT_READ"]}},
    )

    principals: list[CliValue] = Field(min_length=1, description="'*' means all principals")
    subjects: list[CliValue] = Field(default=[], description="'*' means all subjects")
    topics: list[CliValue] = Field(default=[], description="Topic T means subjects T-key and T-value")
    operations: list[CliValue] = Field(min_length=1, description="Operation names, '*' means all")

    def affected_principals(self) -> list[str]:
        return _collapse_wildcard(self.principals)

    def affected_subjects(self) -> list[str]:
        topic_subjects = [subject for topic in self.topics for subject in _topic_subjects(topic)]
        return _collapse_wildcard(self.subjects + topic_subjects)

    def changes_subject_acls(self) -> bool:
        return any(op.is_subject_operation for op in resolve_operations(self.operations))

    def changes_global_acls(self) -> bool:
        return any(not op.is_subject_operation for op in resolve_operations(self.operations))


class _ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class SubjectAcl(_ApiModel):
    principal: str
    subject: str
    operations: list[str]


class GlobalAcl(_ApiModel):
    principal: str
    operations: list[str]


class AclListing(_ApiModel):
    subject_acls: list[SubjectAcl] = []
    global_acls: list[GlobalAcl] = []


class AclSnapshot(BaseModel):
    """Every ACL record on the ACL topic, as printed by `sr-acl-cli --list`.
    Records whose operations were all removed stay on the topic, with an empty list."""

    model_config = ConfigDict(frozen=True)

    subject_operations: dict[str, dict[str, list[str]]] = {}  # principal -> subject -> operations
    global_operations: dict[str, list[str]] = {}              # principal -> operations

    def listing(self, principal: str | None = None, subject: str | None = None) -> AclListing:
        """Non-empty ACLs, sorted, optionally filtered by exact principal and/or subject.
        Global ACLs have no subject, so a subject filter leaves them out."""
        subject_acls = [
            SubjectAcl(principal=p, subject=s, operations=ops)
            for p, by_subject in sorted(self.subject_operations.items()) if principal in (None, p)
            for s, ops in sorted(by_subject.items()) if subject in (None, s) and ops
        ]
        global_acls = [
            GlobalAcl(principal=p, operations=ops)
            for p, ops in sorted(self.global_operations.items())
            if principal in (None, p) and subject is None and ops
        ]
        return AclListing(subject_acls=subject_acls, global_acls=global_acls)

    def records_touched_by(self, change: AclChange) -> AclListing:
        """The records the CLI writes for this change, with their current operations (empty after a
        full removal): principals x subjects for subject operations, principals for global ones."""
        principals = change.affected_principals()
        subject_acls = [
            SubjectAcl(principal=p, subject=s, operations=self.subject_operations.get(p, {}).get(s, []))
            for p in principals for s in change.affected_subjects()
        ] if change.changes_subject_acls() else []
        global_acls = [
            GlobalAcl(principal=p, operations=self.global_operations.get(p, []))
            for p in principals
        ] if change.changes_global_acls() else []
        return AclListing(subject_acls=subject_acls, global_acls=global_acls)
