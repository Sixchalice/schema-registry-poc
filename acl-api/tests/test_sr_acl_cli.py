"""SrAclCli against fake executables that behave like the real CLI."""

import os
from pathlib import Path

import pytest

from acl_api.cli.sr_acl_cli import SrAclCli
from acl_api.domain.models import AclChange
from acl_api.errors import CliFailed, CliRejectedArguments, CliTimedOut
from tests.conftest import run
from tests.test_list_output import REAL_OUTPUT


def fake_cli(tmp_path: Path, body: str) -> SrAclCli:
    executable = tmp_path / "sr-acl-cli"
    executable.write_text(f"#!/bin/sh\n{body}\n")
    executable.chmod(0o755)
    return SrAclCli(executable, tmp_path / "sr.properties", timeout=2)


def test_list_parses_stdout_and_ignores_stderr(tmp_path):
    cli = fake_cli(tmp_path, f"cat <<'EOF'\n{REAL_OUTPUT}EOF\necho 'log4j:WARN noise' >&2")

    snapshot = run(cli.list_acls())

    assert snapshot.global_operations == {"platform-admin": ["GLOBAL_READ"]}


def test_passes_config_and_values_in_name_equals_value_form(tmp_path):
    args_file = tmp_path / "args"
    cli = fake_cli(tmp_path, f'printf "%s\\n" "$@" > {args_file}')
    change = AclChange(principals=["--remove"], subjects=["s"], topics=["t"], operations=["SUBJECT_READ"])

    run(cli.add(change))

    assert args_file.read_text().splitlines() == [
        f"--config={tmp_path / 'sr.properties'}", "--add",
        "--principal=--remove", "--subject=s", "--topic=t", "--operation=SUBJECT_READ",
    ]


def test_rejected_arguments_report_the_cli_message(tmp_path):
    cli = fake_cli(tmp_path, """cat >&2 <<'EOF'
Invalid operation specified : NOPE
Option                      Description
------                      -----------
--add                       Indicates you are trying to add ACLs.
EOF
exit 1""")

    with pytest.raises(CliRejectedArguments, match="^Invalid operation specified : NOPE$"):
        run(cli.list_acls())


def test_crash_reports_the_exception_line(tmp_path):
    cli = fake_cli(tmp_path, """cat >&2 <<'EOF'
Exception in thread "main" org.apache.kafka.common.KafkaException: Failed to construct kafka consumer
\tat org.apache.kafka.clients.consumer.KafkaConsumer.<init>(KafkaConsumer.java:830)
EOF
exit 1""")

    with pytest.raises(CliFailed, match="KafkaException: Failed to construct kafka consumer"):
        run(cli.list_acls())


def test_timeout_kills_the_process(tmp_path):
    pid_file = tmp_path / "pid"
    cli = fake_cli(tmp_path, f"echo $$ > {pid_file}\nexec sleep 30")
    cli._timeout = 0.5

    with pytest.raises(CliTimedOut):
        run(cli.list_acls())

    with pytest.raises(ProcessLookupError):  # killed and reaped
        os.kill(int(pid_file.read_text()), 0)
