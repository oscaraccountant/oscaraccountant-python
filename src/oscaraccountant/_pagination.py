"""The pages of a list method (clients/spec/README.md, section 3.6).

A list method returns a paginator. `first_page()` gives one page; iterating it gives every item of every
page. The first request carries the caller's parameters; each next one the same parameters and `cursor`
set to the last `next_cursor`, until it is null. A page is fetched only when its first item is needed.
"""

import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from typing import Generic, Protocol, TypeVar, cast

from pydantic import BaseModel

from .generated import Request

PageT = TypeVar('PageT', bound=BaseModel)
ItemT = TypeVar('ItemT', bound=BaseModel)
ItemT_co = TypeVar('ItemT_co', covariant=True)


class _PageShape(Protocol[ItemT_co]):
    @property
    def items(self) -> Sequence[ItemT_co]: ...

    @property
    def next_cursor(self) -> str | None: ...


def _next(request: Request, cursor: str) -> Request:
    return dataclasses.replace(request, query={**request.query, 'cursor': cursor})


class SyncPage(Generic[PageT, ItemT]):
    """The result of a list method of `Oscar`."""

    def __init__(self, fetch: Callable[[Request], PageT], request: Request, item_type: type[ItemT]) -> None:
        self._fetch = fetch
        self._request = request
        self._item_type = item_type

    def first_page(self) -> PageT:
        """The first page, with the caller's parameters."""
        return self._fetch(self._request)

    def pages(self) -> Iterator[PageT]:
        """Every page, one request each, fetched when needed."""
        request = self._request
        while True:
            page = self._fetch(request)
            yield page
            cursor = cast('_PageShape[ItemT]', page).next_cursor
            if not cursor:
                return
            request = _next(request, cursor)

    def __iter__(self) -> Iterator[ItemT]:
        for page in self.pages():
            yield from cast('_PageShape[ItemT]', page).items


class AsyncPage(Generic[PageT, ItemT]):
    """The result of a list method of `AsyncOscar`."""

    def __init__(self, fetch: Callable[[Request], Awaitable[PageT]], request: Request, item_type: type[ItemT]) -> None:
        self._fetch = fetch
        self._request = request
        self._item_type = item_type

    async def first_page(self) -> PageT:
        """The first page, with the caller's parameters."""
        return await self._fetch(self._request)

    async def pages(self) -> AsyncIterator[PageT]:
        """Every page, one request each, fetched when needed."""
        request = self._request
        while True:
            page = await self._fetch(request)
            yield page
            cursor = cast('_PageShape[ItemT]', page).next_cursor
            if not cursor:
                return
            request = _next(request, cursor)

    async def __aiter__(self) -> AsyncIterator[ItemT]:
        async for page in self.pages():
            for item in cast('_PageShape[ItemT]', page).items:
                yield item
