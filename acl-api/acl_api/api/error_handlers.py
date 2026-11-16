"""Turns every error into a JSON body {"error": "..."} with the right status code."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from acl_api.api.schemas import ErrorBody
from acl_api.errors import AclApiError, CliFailed, CliRejectedArguments, CliTimedOut, WriteQueueFull

# Looked up along the exception's class hierarchy, so subclasses inherit their parent's status.
_STATUS = {
    CliRejectedArguments: 400,
    CliFailed: 502,
    WriteQueueFull: 503,
    CliTimedOut: 504,
}
_HEADERS = {WriteQueueFull: {"Retry-After": "5"}}


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AclApiError, _service_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)


def _error_response(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(ErrorBody(error=message).model_dump(), status_code=status, headers=headers)


async def _service_error(_: Request, error: AclApiError) -> JSONResponse:
    kind = next((cls for cls in type(error).__mro__ if cls in _STATUS), None)
    status = _STATUS.get(kind, 500)
    return _error_response(status, str(error), _HEADERS.get(kind))


async def _http_error(_: Request, error: HTTPException) -> JSONResponse:
    return _error_response(error.status_code, str(error.detail), error.headers)


async def _validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
    messages = []
    for problem in error.errors():
        location = "/".join(str(part) for part in problem["loc"] if part != "body")
        messages.append(f"{location}: {problem['msg']}" if location else problem["msg"])
    return _error_response(400, "; ".join(messages))
