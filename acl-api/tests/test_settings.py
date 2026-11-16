import pytest
from pydantic import ValidationError

from acl_api.settings import Settings


@pytest.fixture
def files(tmp_path):
    cli = tmp_path / "sr-acl-cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    config = tmp_path / "sr.properties"
    config.write_text("")
    return cli, config


def test_reads_acl_api_environment(monkeypatch, files):
    cli, config = files
    monkeypatch.setenv("ACL_API_PASSWORD", "pw")
    monkeypatch.setenv("ACL_API_CLI_PATH", str(cli))
    monkeypatch.setenv("ACL_API_CLI_CONFIG", str(config))
    monkeypatch.setenv("ACL_API_MAX_PENDING_WRITES", "7")

    settings = Settings()

    assert settings.password.get_secret_value() == "pw"
    assert settings.max_pending_writes == 7
    assert settings.user == "acl-admin"
    assert "pw" not in repr(settings)


@pytest.mark.parametrize("overrides, field", [
    ({"password": ""}, "password"),
    ({"cli_config": "/does/not/exist"}, "cli_config"),
    ({"max_pending_writes": 0}, "max_pending_writes"),
    ({"cli_timeout": -1}, "cli_timeout"),
])
def test_invalid_settings_fail_at_startup(files, overrides, field):
    cli, config = files
    values = {"password": "pw", "cli_path": cli, "cli_config": config, **overrides}

    with pytest.raises(ValidationError, match=field):
        Settings(**values)


def test_cli_must_be_executable(files):
    cli, config = files
    cli.chmod(0o644)

    with pytest.raises(ValidationError, match="not executable"):
        Settings(password="pw", cli_path=cli, cli_config=config)


def test_password_is_required(monkeypatch, files):
    cli, config = files
    monkeypatch.delenv("ACL_API_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="password"):
        Settings(cli_path=cli, cli_config=config)
