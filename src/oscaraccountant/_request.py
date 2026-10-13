"""One call, from the request spec of a generated method to the bytes on the wire.

clients/spec/README.md, sections 3.2 (the URL), 3.3 (the headers), 3.5 (`Idempotency-Key`) and 3.8 (the
upload).
"""

import datetime as dt
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from urllib.parse import quote

import httpx

from ._errors import UsageError
from ._files import PreparedFile, prepare_file
from .generated import JsonBody, MultipartBody, Request, RequestOptions

#: The headers a caller may not set, for one call or for the client.
PROTECTED_HEADERS = frozenset({'authorization', 'idempotency-key'})

_PATH_NAME = re.compile(r'\{([^{}]+)\}')


@dataclass(frozen=True)
class Settings:
    """The settings of a client that every call reads."""

    base_url: str
    timeout: float
    max_retries: int
    user_agent: str
    default_headers: Mapping[str, str]
    #: The current time, a UNIX time in seconds: `Retry-After` dates and token expiry.
    clock: Callable[[], float]
    #: A number in `[0, 1)` for the backoff.
    jitter: Callable[[], float]
    #: A UUID version 4 as a string, for `Idempotency-Key`.
    uuid_factory: Callable[[], str]


def encode_path(template: str, values: Mapping[str, str]) -> str:
    """The path with each `{name}` replaced by its value, percent-encoded except the unreserved characters."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values.get(name)
        if not value or value in {'.', '..'}:
            # A URL parser removes or merges such a segment (also `%2E%2E`): the call would reach another path.
            raise UsageError(f'The path parameter {name!r} must not be missing, empty, `.` or `..`.')
        return quote(value, safe='')

    return _PATH_NAME.sub(replace, template)


def _number(value: float) -> str:
    """The decimal form of a float: `0.1`, `100000000000000000000`, never an exponent."""
    return format(Decimal(repr(value)), 'f')


def wire_value(value: object) -> str | None:
    """A query or multipart value as text: `None` is not set, a boolean is `true` or `false`."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, str | int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    raise TypeError(f'A value of type {type(value).__name__} cannot go in a query or a form.')


def _headers(settings: Settings, options: RequestOptions | None, accept: str) -> httpx.Headers:
    headers = httpx.Headers({'Accept': accept, 'User-Agent': settings.user_agent})
    extra = [settings.default_headers]
    if options is not None and options.extra_headers:
        extra.append(options.extra_headers)
    for mapping in extra:
        for name, value in mapping.items():
            if name.lower() not in PROTECTED_HEADERS:
                headers[name] = value
    return headers


@dataclass(frozen=True)
class Call:
    """One call, ready to send. Every attempt sends the same URL, headers, body and `Idempotency-Key`."""

    method: str
    url: str
    query: list[tuple[str, str]]
    headers: httpx.Headers
    timeout: float
    max_retries: int
    idempotency_key: str | None
    content: bytes | None = None
    data: dict[str, str] | None = None
    files: dict[str, PreparedFile] | None = field(default=None)

    @property
    def retryable(self) -> bool:
        """A `GET` or a request with an `Idempotency-Key` may be sent again. No other request is."""
        return self.method == 'GET' or self.idempotency_key is not None

    def build(self, http: httpx.Client | httpx.AsyncClient, token: str) -> httpx.Request:
        """The HTTP request of one attempt, with the credential of that attempt."""
        headers = self.headers.copy()
        headers['Authorization'] = f'Bearer {token}'
        if self.idempotency_key is not None:
            headers['Idempotency-Key'] = self.idempotency_key
        files = {name: item.as_httpx() for name, item in self.files.items()} if self.files is not None else None
        return http.build_request(
            self.method,
            self.url,
            params=tuple(self.query),
            headers=headers,
            content=self.content,
            data=self.data,
            files=files,
            timeout=self.timeout,
        )


def prepare_call(settings: Settings, request: Request, options: RequestOptions | None) -> Call:
    """The call of a generated request spec: every part that stays the same on each attempt."""
    timeout = settings.timeout if options is None or options.timeout is None else options.timeout
    max_retries = settings.max_retries if options is None or options.max_retries is None else options.max_retries
    if max_retries < 0:
        raise ValueError('max_retries must be 0 or more.')
    idempotency_key = None
    if request.idempotent:
        chosen = options.idempotency_key if options is not None else None
        idempotency_key = chosen or settings.uuid_factory()
    accept = '*/*' if request.response == 'binary' else 'application/json'
    headers = _headers(settings, options, accept)
    query = [(name, text) for name, value in request.query.items() if (text := wire_value(value)) is not None]
    url = settings.base_url + encode_path(request.path, request.path_params)
    content = None
    data = None
    files = None
    if isinstance(request.body, JsonBody):
        payload = request.body.value.model_dump(mode='json', by_alias=True, exclude_unset=True)
        content = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        headers['Content-Type'] = 'application/json'
    elif isinstance(request.body, MultipartBody):
        data = {name: text for name, value in request.body.fields.items() if (text := wire_value(value)) is not None}
        files = {name: prepare_file(value) for name, value in request.body.files.items()}
    return Call(
        method=request.method,
        url=url,
        query=query,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        idempotency_key=idempotency_key,
        content=content,
        data=data,
        files=files,
    )
