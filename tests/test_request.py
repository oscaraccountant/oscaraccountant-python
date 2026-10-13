import datetime as dt
import io
import json

import httpx
import pytest
from pydantic import Field

from oscaraccountant import UsageError
from oscaraccountant._request import Settings, encode_path, prepare_call, wire_value
from oscaraccountant.generated import (
    BooksInvoiceStateEnum,
    JsonBody,
    MultipartBody,
    OscarModel,
    Request,
    RequestOptions,
)

from .conftest import BASE


def settings(**changes):
    values = {
        'base_url': BASE,
        'timeout': 60.0,
        'max_retries': 2,
        'user_agent': 'oscaraccountant-python/test',
        'default_headers': {},
        'clock': lambda: 0.0,
        'jitter': lambda: 0.0,
        'uuid_factory': lambda: 'generated-key',
    }
    values.update(changes)
    return Settings(**values)


def spec(**changes):
    values = {
        'operation_id': 'op',
        'method': 'GET',
        'path': '/api/companies/{company_id}/things/',
        'path_params': {'company_id': 'c1'},
        'query': {},
        'body': None,
        'idempotent': False,
        'response': 'json',
    }
    values.update(changes)
    return Request(**values)


class Payload(OscarModel):
    name: str | None = None
    note: str | None = None
    when: dt.date | None = None


def test_encode_path():
    assert encode_path('/a/{x}/b/{y}/', {'x': 'a b/c', 'y': 'A-z_0.9~'}) == '/a/a%20b%2Fc/b/A-z_0.9~/'
    assert encode_path('/a/{x}/', {'x': 'ö?#'}) == '/a/%C3%B6%3F%23/'


@pytest.mark.parametrize('values', [{}, {'x': ''}])
def test_encode_path_refuses_missing(values):
    with pytest.raises(ValueError, match="'x'"):
        encode_path('/a/{x}/', values)


@pytest.mark.parametrize('value', ['.', '..'])
def test_encode_path_refuses_a_dot_segment(value):
    # A URL parser would resolve `..` (and `%2E%2E`) and reach another path.
    with pytest.raises(UsageError, match="'x'"):
        encode_path('/a/{x}/', {'x': value})
    assert encode_path('/a/{x}/', {'x': '...'}) == '/a/.../'


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (None, None),
        (True, 'true'),
        (False, 'false'),
        (BooksInvoiceStateEnum('issued'), 'issued'),
        (BooksInvoiceStateEnum('brand_new'), 'brand_new'),
        ('text', 'text'),
        (50, '50'),
        (0.1, '0.1'),
        (1e20, '100000000000000000000'),
        (dt.date(2026, 9, 27), '2026-09-27'),
        (dt.datetime(2026, 9, 27, 10, 0, tzinfo=dt.UTC), '2026-09-27T10:00:00+00:00'),
    ],
)
def test_wire_value(value, expected):
    assert wire_value(value) == expected


def test_wire_value_refuses_other_types():
    with pytest.raises(TypeError, match='list'):
        wire_value([1])


def test_get_call():
    call = prepare_call(settings(), spec(query={'state': 'issued', 'limit': 5, 'cursor': None, 'x': True}), None)
    assert call.url == f'{BASE}/api/companies/c1/things/'
    assert call.query == [('state', 'issued'), ('limit', '5'), ('x', 'true')]
    assert call.idempotency_key is None
    assert call.retryable
    assert call.timeout == 60.0
    assert call.max_retries == 2
    request = call.build(httpx.Client(), 'tok')
    assert request.headers['authorization'] == 'Bearer tok'
    assert request.headers['accept'] == 'application/json'
    assert request.headers['user-agent'] == 'oscaraccountant-python/test'
    assert 'idempotency-key' not in request.headers
    assert request.url.params.multi_items() == [('state', 'issued'), ('limit', '5'), ('x', 'true')]
    assert request.extensions['timeout'] == httpx.Timeout(60.0).as_dict()


def test_options_override_the_client():
    options = RequestOptions(timeout=5.0, max_retries=0)
    call = prepare_call(settings(), spec(), options)
    assert call.timeout == 5.0
    assert call.max_retries == 0


def test_negative_retries_refused():
    with pytest.raises(ValueError, match='max_retries'):
        prepare_call(settings(), spec(), RequestOptions(max_retries=-1))


def test_headers_cannot_replace_the_credential_or_the_key():
    call = prepare_call(
        settings(default_headers={'X-Team': 'a', 'Authorization': 'Bearer evil'}),
        spec(method='POST', idempotent=True, body=None),
        RequestOptions(
            extra_headers={'x-team': 'b', 'Idempotency-Key': 'evil', 'accept': 'text/plain'},
            idempotency_key='mine',
        ),
    )
    request = call.build(httpx.Client(), 'tok')
    assert request.headers.get_list('x-team') == ['b']
    assert request.headers.get_list('authorization') == ['Bearer tok']
    assert request.headers.get_list('idempotency-key') == ['mine']
    assert request.headers.get_list('accept') == ['text/plain']


def test_idempotency_key_is_made_once():
    keys = iter(['k1', 'k2'])
    call = prepare_call(settings(uuid_factory=lambda: next(keys)), spec(method='POST', idempotent=True), None)
    assert call.idempotency_key == 'k1'
    assert call.retryable
    assert call.build(httpx.Client(), 'a').headers['idempotency-key'] == 'k1'
    assert call.build(httpx.Client(), 'b').headers['idempotency-key'] == 'k1'


def test_no_key_for_an_operation_that_does_not_accept_it():
    call = prepare_call(settings(), spec(method='POST'), RequestOptions(idempotency_key='mine'))
    assert call.idempotency_key is None
    assert not call.retryable


def test_json_body_holds_only_the_fields_set():
    body = JsonBody(Payload(name='ö', note=None))
    call = prepare_call(settings(), spec(method='POST', body=body), None)
    request = call.build(httpx.Client(), 'tok')
    assert request.headers['content-type'] == 'application/json'
    assert json.loads(request.content) == {'name': 'ö', 'note': None}


def test_json_body_uses_aliases():
    class Aliased(OscarModel):
        from_: str = Field(default='x', alias='from')

    call = prepare_call(settings(), spec(method='POST', body=JsonBody(Aliased(from_='a'))), None)
    assert json.loads(call.content or b'') == {'from': 'a'}


def test_multipart_body():
    body = MultipartBody(
        fields={
            'kind': BooksInvoiceStateEnum('issued'),
            'received_on': dt.date(2026, 9, 1),
            'bank_transaction_id': None,
        },
        files={'file': ('invoice.pdf', b'%PDF', 'application/pdf')},
    )
    call = prepare_call(settings(), spec(method='POST', idempotent=True, body=body), None)
    assert call.data == {'kind': 'issued', 'received_on': '2026-09-01'}
    request = call.build(httpx.Client(), 'tok')
    assert request.headers['content-type'].startswith('multipart/form-data; boundary=')
    content = request.read()
    assert b'name="kind"\r\n\r\nissued' in content
    assert b'name="received_on"\r\n\r\n2026-09-01' in content
    assert b'bank_transaction_id' not in content
    assert b'name="file"; filename="invoice.pdf"' in content
    assert b'Content-Type: application/pdf' in content


def test_multipart_stream_is_read_once_and_replayed():
    stream = io.BytesIO(b'abc')
    body = MultipartBody(fields={}, files={'file': stream})
    call = prepare_call(settings(), spec(method='POST', idempotent=True, body=body), None)
    first = call.build(httpx.Client(), 'tok').read()
    second = call.build(httpx.Client(), 'tok').read()
    for content in (first, second):
        assert b'name="file"; filename="upload"' in content
        assert b'\r\n\r\nabc\r\n' in content
    assert stream.read() == b''
