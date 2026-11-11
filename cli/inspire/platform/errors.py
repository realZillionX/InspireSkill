"""Shared client/transport errors, independent of front-end resource models."""

from typing import Any


class _ErrorMetadata(Exception):
    """Scalar response context only; never retain requests or response objects."""

    def __init__(
        self, *args: Any, code: str | None = None, status: int | None = None,
        service: str | None = None, action: str | None = None,
        request_id: str | None = None, retry_after: float | None = None,
    ) -> None:
        super().__init__(*args)
        self.code = code
        self.status = status
        self.service = service
        self.action = action
        self.request_id = request_id
        self.retry_after = retry_after


def _error_metadata(error: BaseException) -> dict[str, Any]:
    """Copy only the supported fields when translating an exception."""
    result: dict[str, Any] = dict.fromkeys((
        "code", "status", "service", "action", "request_id", "retry_after",
    ))
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        for name in result:
            if result[name] is None:
                result[name] = getattr(cause, name, None)
        cause = cause.__cause__
    return result


class APIError(_ErrorMetadata, ValueError):
    """Internal business rejection, compatible with existing ValueError handlers."""


class InspireError(_ErrorMetadata):
    retryable = False


class ConfigurationError(InspireError):
    pass


class AuthenticationError(InspireError):
    pass


class AuthenticationCooldownError(AuthenticationError):
    def __init__(self, retry_at: float, message: str | None = None, **metadata: Any):
        super().__init__(
            message or "Account authentication is cooling down; inspect credentials before retrying.",
            **metadata,
        )
        self.retry_at = retry_at


class ClientClosedError(InspireError):
    pass


class ClientThreadError(InspireError):
    pass


class ValidationError(InspireError, ValueError):
    pass


class ResourceNotFoundError(InspireError):
    pass


class AmbiguousResourceError(InspireError):
    def __init__(self, message, candidates=(), **metadata: Any):
        super().__init__(message, **metadata)
        self.candidates = tuple(candidates)


class ResolutionIncompleteError(InspireError):
    pass


class TransportError(InspireError):
    retryable = True


class SubmissionUncertainError(InspireError):
    def __init__(self, operation_id: str, *, inspect: str = "jobs", **metadata: Any):
        super().__init__(
            f"Submission may have succeeded; inspect {inspect} before submitting again.", **metadata
        )
        self.inspect = inspect
        self.operation_id = operation_id


class MutationUncertainError(InspireError):
    pass


class WaitTimeoutError(InspireError, TimeoutError):
    pass


