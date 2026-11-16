"""Service configuration, read from ACL_API_* environment variables."""

import os
from pathlib import Path

from pydantic import Field, FilePath, PositiveFloat, PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated at startup: a missing password, CLI or CLI config stops the service from starting."""

    model_config = SettingsConfigDict(env_prefix="ACL_API_", extra="ignore", validate_default=True)

    user: str = Field("acl-admin", min_length=1, description="HTTP Basic user name")
    password: SecretStr = Field(description="HTTP Basic password")
    cli_path: FilePath = Field(Path("/opt/sr-acl-cli/bin/sr-acl-cli"), description="sr-acl-cli executable")
    cli_config: FilePath = Field(
        Path("/etc/acl-api/sr-acl-cli.properties"),
        description="Schema Registry properties for the CLI (Kafka connection, ACL topic)",
    )
    cli_timeout: PositiveFloat = Field(60.0, description="Seconds before a CLI run is killed")
    max_pending_writes: PositiveInt = Field(20, description="Writes allowed to wait; more get 503")
    log_level: str = Field("INFO", description="Python log level")

    @field_validator("password")
    @classmethod
    def _password_not_empty(cls, password: SecretStr) -> SecretStr:
        if not password.get_secret_value():
            raise ValueError("must not be empty")
        return password

    @field_validator("cli_path")
    @classmethod
    def _cli_is_executable(cls, path: Path) -> Path:
        if not os.access(path, os.X_OK):
            raise ValueError(f"{path} is not executable")
        return path
