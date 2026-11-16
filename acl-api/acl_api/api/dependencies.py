"""FastAPI dependencies: the service, the authenticated user and the request body."""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError

from acl_api.domain.models import AclChange
from acl_api.services.acl_service import AclService
from acl_api.settings import Settings

_basic_auth = HTTPBasic(auto_error=False, realm="acl-api")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _acl_service(request: Request) -> AclService:
    return request.app.state.acl_service


def _authenticated_user(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_auth)],
    settings: Annotated[Settings, Depends(_settings)],
) -> str:
    if credentials is None or not _matches(credentials, settings):
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": 'Basic realm="acl-api"'})
    return credentials.username


def _matches(credentials: HTTPBasicCredentials, settings: Settings) -> bool:
    # Compare both parts every time (no short-circuit), in constant time.
    user_ok = secrets.compare_digest(credentials.username.encode(), settings.user.encode())
    password_ok = secrets.compare_digest(
        credentials.password.encode(), settings.password.get_secret_value().encode())
    return user_ok & password_ok


async def _acl_change(request: Request) -> AclChange:
    """The JSON body whatever the Content-Type, so a plain `curl -d '{...}'` works too."""
    try:
        return AclChange.model_validate_json(await request.body() or b"null")
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error


AclServiceDep = Annotated[AclService, Depends(_acl_service)]
AuthenticatedUser = Annotated[str, Depends(_authenticated_user)]
AclChangeBody = Annotated[AclChange, Depends(_acl_change)]

# OpenAPI documentation for AclChangeBody, which FastAPI can't infer from a dependency.
ACL_CHANGE_OPENAPI = {"requestBody": {"required": True, "content": {
    "application/json": {"schema": AclChange.model_json_schema()}}}}
