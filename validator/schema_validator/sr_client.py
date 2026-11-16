"""Schema Registry client protocol, value types and errors."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SrReference:
    name: str
    subject: str
    version: int


@dataclass(frozen=True)
class SchemaVersion:
    subject: str
    version: int
    schema_type: str
    schema: str
    references: tuple[SrReference, ...] = ()


@dataclass(frozen=True)
class CompatibilityVerdict:
    is_compatible: bool
    messages: tuple[str, ...] = ()


class SchemaRegistryError(Exception):
    """Base class for Schema Registry failures."""


class SchemaRegistryUnavailable(SchemaRegistryError):
    """Timeout, connection error, 5xx or unreadable body (E3)."""


class SchemaRegistryUnauthorized(SchemaRegistryError):
    """401 or 403 (E4)."""


class SchemaRegistryRejected(SchemaRegistryError):
    """Other 4xx with an error_code; str(e) is SR's message (E1)."""


class SchemaRegistryClient(Protocol):
    def get_latest(self, subject: str) -> SchemaVersion | None:
        """Latest version of the subject, or None on 404."""
        ...

    def get_version(self, subject: str, version: int) -> SchemaVersion | None:
        """A specific version, or None on 404."""
        ...

    def get_compatibility_level(self, subject: str) -> str: ...

    def check_compatibility(
        self,
        subject: str,
        schema: str,
        schema_type: str,
        references: Sequence[SrReference],
    ) -> CompatibilityVerdict: ...
