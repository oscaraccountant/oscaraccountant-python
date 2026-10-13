"""What the sync and the async client share: the settings, the credential and the retry decisions."""

import os
import random
import time
import uuid
from collections.abc import Callable, Mapping
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import _errors
from ._base_url import resolve_base_url
from ._credentials import AsyncTokenProvider, Credential, CredentialOption, resolve_credential
from ._request import Call, Settings
from ._retry import retry_delay
from ._user_agent import user_agent

ModelT = TypeVar('ModelT', bound=BaseModel)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _jitter() -> float:
    return random.random()  # noqa: S311 - a backoff jitter, not a secret


def configure(
    *,
    api_key: str | None,
    token_provider: AsyncTokenProvider | None,
    credential: CredentialOption | None,
    profile: str | None,
    base_url: str | None,
    timeout: float,
    max_retries: int,
    default_headers: Mapping[str, str] | None,
    clock: Callable[[], float] | None,
    jitter: Callable[[], float] | None,
    uuid_factory: Callable[[], str] | None,
) -> tuple[Settings, Credential]:
    """The settings and the credential of a new client. It reads the environment once, here."""
    if timeout <= 0:
        raise _errors.ConfigurationError('timeout must be more than 0 seconds.')
    if max_retries < 0:
        raise _errors.ConfigurationError('max_retries must be 0 or more.')
    environ = dict(os.environ)
    resolved_url = resolve_base_url(base_url, environ)
    settings = Settings(
        base_url=resolved_url,
        timeout=timeout,
        max_retries=max_retries,
        user_agent=user_agent(),
        default_headers=dict(default_headers or {}),
        clock=clock or time.time,
        jitter=jitter or _jitter,
        uuid_factory=uuid_factory or _new_uuid,
    )
    source = resolve_credential(
        api_key=api_key,
        token_provider=token_provider,
        credential=credential,
        profile=profile,
        base_url=resolved_url,
        environ=environ,
        clock=settings.clock,
    )
    return settings, source


def transport_error(exc: httpx.RequestError) -> _errors.OscarError:
    """The error of an attempt that got no response."""
    target = f'{exc.request.method} {exc.request.url.copy_with(query=None)}'
    error: _errors.OscarError
    if isinstance(exc, httpx.TimeoutException):
        error = _errors.TimeoutError(f'{target} timed out.')
    else:
        error = _errors.ConnectionError(f'{target} got no response: {type(exc).__name__}.')
    error.__cause__ = exc
    return error


def next_delay(
    settings: Settings, call: Call, retry: int, *, retryable: bool, headers: Mapping[str, str] | None
) -> float | None:
    """The delay before retry `retry` (1, 2, ...), or `None` when the call must not be sent again."""
    if not retryable or not call.retryable or retry > call.max_retries:
        return None
    return retry_delay(retry, headers, now=settings.clock(), jitter=settings.jitter())


def parse_model(response_type: type[ModelT], response: httpx.Response) -> ModelT:
    """The body of a success response as the type of the operation."""
    try:
        return response_type.model_validate_json(response.content)
    except ValidationError as exc:
        raise _errors.ResponseValidationError(
            f'The {response.status_code} response is not a valid {response_type.__name__}.',
            status=response.status_code,
            request_id=response.headers.get('x-request-id'),
        ) from exc
