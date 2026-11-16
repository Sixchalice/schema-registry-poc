"""Errors raised below the HTTP layer. api/error_handlers.py maps them to status codes."""


class AclApiError(Exception):
    """Base class; the message is safe to show to API callers."""


class CliRejectedArguments(AclApiError):
    """The CLI refused the request (bad operation, subject with a global-only operation, ...)."""


class CliFailed(AclApiError):
    """The CLI could not do its job, e.g. Kafka is unreachable."""


class UnexpectedCliOutput(CliFailed):
    """The CLI succeeded but printed something this service cannot parse."""


class CliTimedOut(AclApiError):
    """The CLI did not finish in time and was killed."""


class WriteQueueFull(AclApiError):
    """Too many writes are already waiting; the caller should retry later."""
