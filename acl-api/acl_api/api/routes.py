"""The endpoints. Every route except /health requires HTTP Basic auth."""

from fastapi import APIRouter

from acl_api.api.dependencies import ACL_CHANGE_OPENAPI, AclChangeBody, AclServiceDep, AuthenticatedUser
from acl_api.api.schemas import ErrorBody, Health, OperationCatalog
from acl_api.domain.models import AclListing

_ERRORS = {code: {"model": ErrorBody} for code in (400, 401, 502, 503, 504)}

router = APIRouter(responses=_ERRORS)


@router.get("/health", response_model=Health)
async def health(service: AclServiceDep, deep: bool = False) -> Health:
    """Liveness. With deep=true, also reads the ACLs through the CLI (checks Kafka)."""
    if deep:
        await service.check_backend()
    return Health()


@router.get("/operations", response_model=OperationCatalog, response_model_by_alias=True)
async def operations(_: AuthenticatedUser) -> OperationCatalog:
    """The operation names that can be granted."""
    return OperationCatalog()


@router.get("/acls", response_model=AclListing)
async def list_acls(service: AclServiceDep, _: AuthenticatedUser,
                    principal: str | None = None, subject: str | None = None) -> AclListing:
    """Current ACLs, optionally filtered by exact principal and/or subject (`sr-acl-cli --list`)."""
    return await service.list_acls(principal, subject)


@router.post("/acls", response_model=AclListing, openapi_extra=ACL_CHANGE_OPENAPI)
async def add_acls(service: AclServiceDep, user: AuthenticatedUser, change: AclChangeBody) -> AclListing:
    """Grant operations (`sr-acl-cli --add`). Returns the touched records as they are now."""
    return await service.add(change, actor=user)


@router.delete("/acls", response_model=AclListing, openapi_extra=ACL_CHANGE_OPENAPI)
async def remove_acls(service: AclServiceDep, user: AuthenticatedUser, change: AclChangeBody) -> AclListing:
    """Revoke operations (`sr-acl-cli --remove`). Returns the touched records as they are now."""
    return await service.remove(change, actor=user)
