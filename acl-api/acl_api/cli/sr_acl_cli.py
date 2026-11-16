"""Runs sr-acl-cli as a subprocess. Each run is a fresh JVM (~1 s, ~2 CPU-seconds)."""

import asyncio
import logging
from pathlib import Path

from acl_api.cli.list_output import parse_list_output
from acl_api.domain.models import AclChange, AclSnapshot
from acl_api.errors import CliFailed, CliRejectedArguments, CliTimedOut

log = logging.getLogger(__name__)

# When the CLI rejects its arguments, it prints the reason and then its usage table, which starts
# with one of these lines. Other failures (e.g. Kafka unreachable) print a stack trace instead.
_USAGE_TABLE_STARTS = ("Option ", "Usage:")


class SrAclCli:
    """Implements services.acl_service.AclBackend with the real CLI."""

    def __init__(self, executable: Path, config: Path, timeout: float):
        self._executable = executable
        self._config = config
        self._timeout = timeout

    async def list_acls(self) -> AclSnapshot:
        return parse_list_output(await self._run("--list"))

    async def add(self, change: AclChange) -> None:
        await self._run("--add", *_change_arguments(change))

    async def remove(self, change: AclChange) -> None:
        await self._run("--remove", *_change_arguments(change))

    async def _run(self, *arguments: str) -> str:
        """Returns stdout. Raises CliRejectedArguments, CliFailed or CliTimedOut."""
        process = await asyncio.create_subprocess_exec(
            str(self._executable), f"--config={self._config}", *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError:
            raise CliTimedOut(f"sr-acl-cli did not finish within {self._timeout:g}s") from None
        finally:
            if process.returncode is None:  # timed out or cancelled: don't leave the JVM running
                process.kill()
                await process.wait()

        if process.returncode != 0:
            raise _failure(arguments, process.returncode, stderr.decode(errors="replace"))
        return stdout.decode(errors="replace")


def _change_arguments(change: AclChange) -> list[str]:
    # The --name=value form keeps a value that starts with '-' from being read as an option.
    return [
        *(f"--principal={principal}" for principal in change.principals),
        *(f"--subject={subject}" for subject in change.subjects),
        *(f"--topic={topic}" for topic in change.topics),
        *(f"--operation={operation}" for operation in change.operations),
    ]


def _failure(arguments: tuple[str, ...], returncode: int, stderr: str) -> CliRejectedArguments | CliFailed:
    """The CLI exits with 1 both for bad arguments and for crashes; tell them apart by the output."""
    lines = stderr.splitlines()
    usage_start = next((i for i, line in enumerate(lines) if line.startswith(_USAGE_TABLE_STARTS)), None)
    if usage_start is not None:
        reason = " ".join(line.strip() for line in lines[:usage_start] if line.strip())
        return CliRejectedArguments(reason or "sr-acl-cli rejected the arguments")

    log.error("sr-acl-cli %s exited with %s:\n%s", " ".join(arguments), returncode, stderr)
    # The first line that is not a stack frame is the exception and its message.
    summary = next((line.strip() for line in lines if line.strip() and not line.startswith("\t")),
                   f"exit code {returncode}")
    return CliFailed(f"sr-acl-cli failed: {summary}")
