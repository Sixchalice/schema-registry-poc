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
        # When set, fail_with applies only to the named methods; None means every method.
        self.fail_methods: set[str] | None = None
        self.calls: list[tuple] = []

    def add(self, subject: str, schema: str, schema_type: str = "AVRO",
            references: Sequence[SrReference] = ()) -> int:
        versions = self._subjects.setdefault(subject, [])
        version = len(versions) + 1
        versions.append(SchemaVersion(subject, version, schema_type, schema, tuple(references)))
        return version

    def fail_only(self, method: str, error: Exception) -> None:
        """Make just this method raise `error`."""
        self.fail_with = error
        self.fail_methods = {method}

    def _check_fail(self) -> None:
        method = self.calls[-1][0]
        if self.fail_with is not None and (self.fail_methods is None or method in self.fail_methods):
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

    def registered_version(self, subject: str, schema: str, schema_type: str,
                           references: Sequence[SrReference]) -> int | None:
        self.calls.append(("registered_version", subject, schema, schema_type, tuple(references)))
        self._check_fail()
        # Exact text match; the real registry compares parsed schemas (covered by integration).
        for v in self._subjects.get(subject, []):
            if (v.schema == schema and v.schema_type == schema_type
                    and tuple(v.references) == tuple(references)):
                return v.version
        return None
