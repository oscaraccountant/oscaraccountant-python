"""The errors of the SDK (clients/spec/README.md, section 3.7).

Every error derives from `OscarError`. A response with a status of 400 or above becomes an `ApiError`, or
the subtype of its status. `ConnectionError` and `TimeoutError` have no response; they subclass
`OscarError`, not the builtins of the same name.
"""

import json
from collections.abc import Mapping

#: The number of characters of a body that is not a problem that go in `detail`.
MAX_DETAIL = 2000


class OscarError(Exception):
    """The base of every error of the SDK."""


class ConfigurationError(OscarError):
    """The client got an option it cannot use, for example a base URL that is not allowed."""


class UsageError(OscarError, ValueError):
    """A call the client cannot send as given: a path value that is empty, `.` or `..` (section 3.2)."""


class ApiError(OscarError):
    """The API answered with an error, as an RFC 9457 problem."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None,
        code: str,
        title: str | None = None,
        detail: str | None = None,
        errors: list[dict[str, object]] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        #: The HTTP status, or `None` for an `AuthenticationError` raised before a request.
        self.status = status
        #: The stable, machine-readable code of the problem, `unknown` when the body is not a problem.
        self.code = code
        self.title = title
        self.detail = detail
        #: One entry per invalid field.
        self.errors: list[dict[str, object]] = errors if errors is not None else []
        #: The `X-Request-Id` of the response.
        self.request_id = request_id


class BadRequestError(ApiError):
    """400."""


class AuthenticationError(ApiError):
    """401, or no credential is available: then `status` is `None` and nothing was sent."""

    @classmethod
    def no_credential(cls, message: str) -> 'AuthenticationError':
        """The error of a credential that cannot be found or used, before any request."""
        return cls(message, status=None, code='no_credential')


class PermissionDeniedError(ApiError):
    """403."""


class NotFoundError(ApiError):
    """404."""


class ConflictError(ApiError):
    """409."""


class UnprocessableEntityError(ApiError):
    """422."""


class RateLimitError(ApiError):
    """429."""


class InternalServerError(ApiError):
    """500 and above."""


class ConnectionError(OscarError):
    """The request got no response: DNS, a refused or reset connection, TLS. Raised after the last retry."""


class TimeoutError(OscarError):
    """An attempt ran out of time. Raised after the last retry."""


class ResponseValidationError(OscarError):
    """A success response whose body does not match the type of the operation."""

    def __init__(self, message: str, *, status: int, request_id: str | None) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id


_BY_STATUS: dict[int, type[ApiError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def error_class(status: int) -> type[ApiError]:
    """The subtype of `ApiError` for an HTTP status of 400 or above."""
    if status >= 500:
        return InternalServerError
    return _BY_STATUS.get(status, ApiError)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _problem(body: bytes) -> dict[str, object] | None:
    """The body as a problem, or `None` when it is not JSON or has no string `code`."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get('code'), str):
        return None
    return parsed


def api_error(status: int, headers: Mapping[str, str], body: bytes, reason: str) -> ApiError:
    """The `ApiError` of a response with a status of 400 or above.

    `headers` must look up names without regard to case, as `httpx.Headers` does.
    """
    request_id = headers.get('x-request-id') or None
    problem = _problem(body)
    if problem is None:
        code = 'unknown'
        title: str | None = reason or None
        detail = body.decode('utf-8', errors='replace')[:MAX_DETAIL] or None
        errors: list[dict[str, object]] = []
    else:
        code = str(problem['code'])
        title = _text(problem.get('title'))
        detail = _text(problem.get('detail'))
        raw_errors = problem.get('errors')
        errors = [item for item in raw_errors if isinstance(item, dict)] if isinstance(raw_errors, list) else []
    message = f'{status} {code}'
    text = detail or title
    if text:
        message = f'{message}: {text}'
    if request_id:
        message = f'{message} (request {request_id})'
    return error_class(status)(
        message,
        status=status,
        code=code,
        title=title,
        detail=detail,
        errors=errors,
        request_id=request_id,
    )
