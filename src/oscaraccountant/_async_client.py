"""The async client, `AsyncOscar`. It is the runtime of the generated async resources."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Self, TypeVar

import httpx
from pydantic import BaseModel

from ._client_base import configure, next_delay, parse_model, transport_error
from ._constants import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from ._credentials import AsyncTokenProvider, CredentialOption
from ._errors import OscarError, api_error
from ._files import AsyncDownload
from ._pagination import AsyncPage
from ._request import Call, prepare_call
from ._retry import is_retryable_status
from .generated import AsyncResources, Request, RequestOptions

ModelT = TypeVar('ModelT', bound=BaseModel)
PageT = TypeVar('PageT', bound=BaseModel)
ItemT = TypeVar('ItemT', bound=BaseModel)


class AsyncOscar(AsyncResources):
    """The async client of the Oscar API. It takes the options of `Oscar`.

    `token_provider` may also return an awaitable, and `sleep` is async (`asyncio.sleep` by default).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        token_provider: AsyncTokenProvider | None = None,
        credential: CredentialOption | None = None,
        profile: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[], float] | None = None,
        uuid_factory: Callable[[], str] | None = None,
    ) -> None:
        self._settings, self._credential = configure(
            api_key=api_key,
            token_provider=token_provider,
            credential=credential,
            profile=profile,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            clock=clock,
            jitter=jitter,
            uuid_factory=uuid_factory,
        )
        self._sleep = sleep or asyncio.sleep
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(follow_redirects=False)
        super().__init__(self)

    @property
    def base_url(self) -> str:
        """The normalized base URL of the API."""
        return self._settings.base_url

    async def aclose(self) -> None:
        """Close the HTTP client, unless the caller gave it."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    # The runtime of the generated resources (generated/_runtime.py, AsyncRuntime).

    async def request(
        self,
        request: Request,
        *,
        response_type: type[ModelT],
        options: RequestOptions | None,
    ) -> ModelT:
        response = await self._send(prepare_call(self._settings, request, options), stream=False)
        return parse_model(response_type, response)

    async def request_no_content(self, request: Request, *, options: RequestOptions | None) -> None:
        await self._send(prepare_call(self._settings, request, options), stream=False)

    def paginate(
        self,
        request: Request,
        *,
        page_type: type[PageT],
        item_type: type[ItemT],
        options: RequestOptions | None,
    ) -> AsyncPage[PageT, ItemT]:
        async def fetch(page_request: Request) -> PageT:
            return await self.request(page_request, response_type=page_type, options=options)

        return AsyncPage(fetch, request, item_type)

    async def download(self, request: Request, *, options: RequestOptions | None) -> AsyncDownload:
        return AsyncDownload(await self._send(prepare_call(self._settings, request, options), stream=True))

    async def _send(self, call: Call, *, stream: bool) -> httpx.Response:
        """Send the call, with its retries. A response of 400 or above becomes an `ApiError`."""
        retry = 0
        refreshed = False
        while True:
            http_request = call.build(self._http, await self._credential.atoken())
            headers: Mapping[str, str] | None = None
            error: OscarError
            try:
                response = await self._http.send(http_request, stream=stream)
            except httpx.RequestError as exc:
                error = transport_error(exc)
                retryable = True
            else:
                if response.status_code < 400:
                    return response
                body = await response.aread()
                await response.aclose()
                api = api_error(response.status_code, response.headers, body, response.reason_phrase)
                if api.status == 401 and self._credential.refreshable and not refreshed:
                    refreshed = True
                    self._credential.invalidate()
                    continue
                error = api
                retryable = is_retryable_status(response.status_code, api.code)
                headers = response.headers
            retry += 1
            delay = next_delay(self._settings, call, retry, retryable=retryable, headers=headers)
            if delay is None:
                raise error
            await self._sleep(delay)
