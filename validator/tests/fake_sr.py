"""In-memory Schema Registry used by unit tests."""
from __future__ import annotations

from collections.abc import Sequence

from schema_validator.sr_client import (
    CompatibilityVerdict,
    SchemaVersion,
    SrReference,
)


class FakeSchemaRegistry:
    """Implements SchemaRegistryClient."""

    def __init__(self) -> None:
        self._subjects: dict[str, list[SchemaVersion]] = {}
        self.levels: dict[str, str] = {}
        self.modes: dict[str, str] = {}
        self.global_mode = "READWRITE"
        self.verdict: CompatibilityVerdict = CompatibilityVerdict(True)
        self.fail_with: Exception | None = None
        self.calls: list[tuple] = []

    def add(self, subject: str, schema: str, schema_type: str = "AVRO",
            references: Sequence[SrReference] = ()) -> int:
        versions = self._subjects.setdefault(subject, [])
        version = len(versions) + 1
        versions.append(SchemaVersion(subject, version, schema_type, schema, tuple(references)))
        return version

    def _check_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get_latest(self, subject: str) -> SchemaVersion | None:
        self.calls.append(("get_latest", subject))
        self._check_fail()
        versions = self._subjects.get(subject)
        return versions[-1] if versions else None

    def get_version(self, subject: str, version: int) -> SchemaVersion | None:
        self.calls.append(("get_version", subject, version))
        self._check_fail()
        versions = self._subjects.get(subject, [])
        return versions[version - 1] if 1 <= version <= len(versions) else None

    def get_compatibility_level(self, subject: str) -> str:
        self.calls.append(("get_compatibility_level", subject))
        self._check_fail()
        return self.levels.get(subject, "BACKWARD")

    def get_mode(self, subject: str) -> str:
        self.calls.append(("get_mode", subject))
        self._check_fail()
        return self.modes.get(subject, self.global_mode)

    def check_compatibility(self, subject: str, schema: str, schema_type: str,
                            references: Sequence[SrReference]) -> CompatibilityVerdict:
        self.calls.append(("check_compatibility", subject, schema, schema_type, tuple(references)))
        self._check_fail()
        return self.verdict

    def is_registered(self, subject: str, schema: str, schema_type: str,
                      references: Sequence[SrReference]) -> bool:
        self.calls.append(("is_registered", subject, schema, schema_type, tuple(references)))
        self._check_fail()
        # Exact text match; the real registry compares parsed schemas (covered by integration).
        return any(v.schema == schema and v.schema_type == schema_type
                   and tuple(v.references) == tuple(references)
                   for v in self._subjects.get(subject, []))
