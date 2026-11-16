import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acl_api.domain.models import AclChange, AclSnapshot
from acl_api.domain.operations import resolve_operations
from acl_api.errors import CliRejectedArguments
from acl_api.main import create_app
from acl_api.settings import Settings

USER, PASSWORD = "admin", "secret"


class FakeBackend:
    """In-memory AclBackend with the CLI's merge rules for the cases the tests use."""

    def __init__(self, write_delay: float = 0.0):
        self.subject_operations: dict[str, dict[str, list[str]]] = {}
        self.global_operations: dict[str, list[str]] = {}
        self.list_calls = 0
        self.writes: list[tuple[str, AclChange]] = []  # every add/remove call, in order
        self.write_delay = write_delay
        self.active_writes = 0
        self.max_active_writes = 0
        self.error: Exception | None = None  # raised by the next call, if set
        self.reject: Callable[[AclChange], bool] = lambda change: False  # -> CliRejectedArguments

    async def list_acls(self) -> AclSnapshot:
        self.list_calls += 1
        self._raise_if_set()
        return AclSnapshot(subject_operations=self.subject_operations, global_operations=self.global_operations)

    async def add(self, change: AclChange) -> None:
        await self._write("add", change)

    async def remove(self, change: AclChange) -> None:
        await self._write("remove", change)

    async def _write(self, action: str, change: AclChange) -> None:
        self.writes.append((action, change))
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            await asyncio.sleep(self.write_delay)
            if self.reject(change):
                raise CliRejectedArguments(f"rejected: {change.principals}")
            self._apply(change, add=action == "add")
        finally:
            self.active_writes -= 1

    def _apply(self, change: AclChange, add: bool) -> None:
        self._raise_if_set()
        operations = resolve_operations(change.operations)
        for principal in change.affected_principals():
            if change.changes_subject_acls():
                for subject in change.affected_subjects():
                    ops = self.subject_operations.setdefault(principal, {}).setdefault(subject, [])
                    self._merge(ops, {op for op in operations if op.is_subject_operation}, add)
            if change.changes_global_acls():
                ops = self.global_operations.setdefault(principal, [])
                self._merge(ops, {op for op in operations if not op.is_subject_operation}, add)

    @staticmethod
    def _merge(current: list[str], operations: set, add: bool) -> None:
        names = {str(op) for op in operations}
        merged = set(current) | names if add else set(current) - names
        current[:] = sorted(merged)

    def _raise_if_set(self) -> None:
        if self.error is not None:
            error, self.error = self.error, None
            raise error


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cli = tmp_path / "sr-acl-cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    config = tmp_path / "sr.properties"
    config.write_text("kafkastore.bootstrap.servers=PLAINTEXT://kafka:9092\n")
    return Settings(user=USER, password=PASSWORD, cli_path=cli, cli_config=config, max_pending_writes=2)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def client(settings: Settings, backend: FakeBackend):
    with TestClient(create_app(settings, backend)) as test_client:
        yield test_client


@pytest.fixture
def auth() -> tuple[str, str]:
    return USER, PASSWORD


def run(coroutine):
    return asyncio.run(coroutine)
