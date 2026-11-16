"""Response bodies that exist only in the HTTP layer."""

from typing import Literal

from pydantic import BaseModel, Field

from acl_api.domain.operations import GLOBAL_OPERATIONS, SUBJECT_OPERATIONS, Operation


class Health(BaseModel):
    status: Literal["UP"] = "UP"


class OperationCatalog(BaseModel):
    subject: list[Operation] = list(SUBJECT_OPERATIONS)
    global_: list[Operation] = Field(list(GLOBAL_OPERATIONS), alias="global")


class ErrorBody(BaseModel):
    error: str
