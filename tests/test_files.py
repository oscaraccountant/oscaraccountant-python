import io

import httpx
import pytest

from oscaraccountant._files import AsyncDownload, Download, filename_from_disposition, prepare_file


def test_prepare_bytes():
    item = prepare_file(b'data')
    assert (item.filename, item.content, item.content_type) == ('upload', b'data', 'application/octet-stream')


def test_prepare_bytearray_and_memoryview():
    assert prepare_file(bytearray(b'a')).content == b'a'
    assert prepare_file(memoryview(b'b')).content == b'b'


def test_prepare_tuple():
    assert prepare_file(('a.pdf', b'x')).as_httpx() == ('a.pdf', b'x', 'application/octet-stream')
    assert prepare_file(('a.pdf', b'x', 'application/pdf')).as_httpx() == ('a.pdf', b'x', 'application/pdf')


def test_prepare_file_object_takes_its_name(tmp_path):
    path = tmp_path / 'receipt.png'
    path.write_bytes(b'\x89PNG')
    with path.open('rb') as handle:
        item = prepare_file(handle)
    assert item.filename == 'receipt.png'
    assert item.content == b'\x89PNG'


def test_prepare_file_object_with_a_tuple_name(tmp_path):
    path = tmp_path / 'receipt.png'
    path.write_bytes(b'x')
    with path.open('rb') as handle:
        assert prepare_file(('other.png', handle)).filename == 'other.png'


def test_prepare_stream_without_name():
    assert prepare_file(io.BytesIO(b'x')).filename == 'upload'


def test_prepare_refuses_text():
    with pytest.raises(TypeError, match='binary'):
        prepare_file(io.StringIO('x'))


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (None, None),
        ('', None),
        ('inline', None),
        ('attachment; filename="report.pdf"', 'report.pdf'),
        ("attachment; filename*=UTF-8''arve%20%C3%B6.pdf", 'arve ö.pdf'),
        ('attachment; filename="../../etc/passwd"', 'passwd'),
        ('attachment; filename="C:\\x\\y.pdf"', 'y.pdf'),
        ('attachment; filename="dir/"', None),
    ],
)
def test_filename_from_disposition(value, expected):
    assert filename_from_disposition(value) == expected


def _response(content=b'hello world', headers=None):
    headers = (
        headers
        if headers is not None
        else {
            'content-type': 'application/pdf',
            'content-length': '11',
            'content-disposition': 'inline; filename="a.pdf"',
            'x-request-id': 'r1',
        }
    )
    return httpx.Response(200, headers=headers, stream=httpx.ByteStream(content))


def test_download_read():
    download = Download(_response())
    assert download.content_type == 'application/pdf'
    assert download.content_length == 11
    assert download.filename == 'a.pdf'
    assert download.request_id == 'r1'
    assert download.read() == b'hello world'


def test_download_without_headers():
    download = Download(_response(headers={}))
    assert download.content_type is None
    assert download.content_length is None
    assert download.filename is None


def test_download_iter_bytes_and_context():
    with Download(_response()) as download:
        assert b''.join(download.iter_bytes(4)) == b'hello world'


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.mark.anyio
async def test_async_download():
    download = AsyncDownload(httpx.Response(200, stream=_AsyncBytes(b'abc')))
    assert await download.aread() == b'abc'
    async with AsyncDownload(httpx.Response(200, stream=_AsyncBytes(b'xyz'))) as second:
        assert [chunk async for chunk in second.aiter_bytes()] == [b'xyz']
