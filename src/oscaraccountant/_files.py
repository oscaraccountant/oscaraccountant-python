"""Files: the upload of a multipart body and the bytes of a download (clients/spec/README.md, section 3.8)."""

import email.message
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx

from .generated import FileInput

#: The file name of an upload when the caller gives none and the content has none.
DEFAULT_FILENAME = 'upload'
DEFAULT_CONTENT_TYPE = 'application/octet-stream'


@dataclass(frozen=True)
class PreparedFile:
    """A file to upload, read once so that every retry sends the same bytes."""

    filename: str
    content: bytes
    content_type: str

    def as_httpx(self) -> tuple[str, bytes, str]:
        return (self.filename, self.content, self.content_type)


def _basename(name: str) -> str:
    return name.replace('\\', '/').rsplit('/', 1)[-1]


def prepare_file(value: FileInput) -> PreparedFile:
    """Read a `FileInput`: bytes, a binary file, or a tuple `(name, content)` or `(name, content, type)`."""
    filename: str | None = None
    content_type = DEFAULT_CONTENT_TYPE
    if isinstance(value, tuple):
        filename = value[0]
        content = value[1]
        if len(value) == 3:
            content_type = value[2]
    else:
        content = value
    if isinstance(content, bytes | bytearray | memoryview):
        data = bytes(content)
    else:
        if filename is None:
            name = getattr(content, 'name', None)
            filename = _basename(name) if isinstance(name, str) else None
        read = content.read()
        if not isinstance(read, bytes):
            raise TypeError('A file to upload must be opened in binary mode.')
        data = read
    return PreparedFile(filename=filename or DEFAULT_FILENAME, content=data, content_type=content_type)


def filename_from_disposition(value: str | None) -> str | None:
    """The file name of a `Content-Disposition` header (`filename*` wins), without any directory part."""
    if not value:
        return None
    message = email.message.Message()
    message['content-disposition'] = value
    name = message.get_filename()
    if not isinstance(name, str) or not name:
        return None
    return _basename(name) or None


class _DownloadBase:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def _header(self, name: str) -> str | None:
        value: str | None = self._response.headers.get(name)
        return value

    @property
    def content_type(self) -> str | None:
        """The `Content-Type` of the response."""
        return self._header('content-type')

    @property
    def content_length(self) -> int | None:
        """The `Content-Length` of the response, when the server gives it."""
        value = self._header('content-length')
        return int(value) if value is not None and value.isdigit() else None

    @property
    def filename(self) -> str | None:
        """The file name of `Content-Disposition`."""
        return filename_from_disposition(self._header('content-disposition'))

    @property
    def request_id(self) -> str | None:
        """The `X-Request-Id` of the response."""
        return self._header('x-request-id')


class Download(_DownloadBase):
    """The bytes of a download. Read them once, with `read()` or `iter_bytes()`; both close the response."""

    def iter_bytes(self, chunk_size: int | None = None) -> Iterator[bytes]:
        try:
            yield from self._response.iter_bytes(chunk_size)
        finally:
            self._response.close()

    def read(self) -> bytes:
        try:
            return self._response.read()
        finally:
            self._response.close()

    def close(self) -> None:
        self._response.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class AsyncDownload(_DownloadBase):
    """The bytes of a download, read asynchronously. `aread()` and `aiter_bytes()` close the response."""

    async def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_bytes(chunk_size):
                yield chunk
        finally:
            await self._response.aclose()

    async def aread(self) -> bytes:
        try:
            return await self._response.aread()
        finally:
            await self._response.aclose()

    async def aclose(self) -> None:
        await self._response.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
