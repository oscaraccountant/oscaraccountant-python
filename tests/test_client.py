import json
import re
import uuid

import httpx
import pytest
import respx

from oscaraccountant import (
    DEFAULT_BASE_URL,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    Oscar,
    RateLimitError,
    RequestOptions,
    ResponseValidationError,
    __version__,
    _errors,
)
from oscaraccountant.generated import JsonBody
from oscaraccountant.types import StoredDocument

from .conftest import BASE, KEY
from .helpers import ORGANIZATION, STORED, Thing, problem, spec


@pytest.fixture
def client(hooks):
    with Oscar(api_key=KEY, base_url=BASE, **hooks.sync_options()) as oscar:
        yield oscar


def test_request_sends_the_headers(api, client):
    route = api.get('/api/things/t%2F1/').respond(200, json={'id': 't1', 'extra': 1})
    thing = client.request(spec(path_params={'thing_id': 't/1'}), response_type=Thing, options=None)
    assert thing.id == 't1'
    sent = route.calls.last.request
    assert sent.headers['authorization'] == f'Bearer {KEY}'
    assert sent.headers['accept'] == 'application/json'
    assert sent.headers['user-agent'].startswith(f'oscaraccountant-python/{__version__} (')
    assert 'idempotency-key' not in sent.headers


def test_construction_reads_the_environment(monkeypatch):
    monkeypatch.setenv('OSCAR_API_KEY', 'osk_env')
    monkeypatch.setenv('OSCAR_BASE_URL', 'https://env.example.test/')
    with respx.mock() as mock, Oscar() as oscar:
        route = mock.get('https://env.example.test/api/things/t1/').respond(200, json={'id': 't1'})
        assert oscar.base_url == 'https://env.example.test'
        oscar.request(spec(), response_type=Thing, options=None)
    assert route.calls.last.request.headers['authorization'] == 'Bearer osk_env'


def test_default_base_url():
    with Oscar(api_key=KEY) as oscar:
        assert oscar.base_url == DEFAULT_BASE_URL


@pytest.mark.parametrize(
    'options',
    [{'base_url': 'http://api.example.test'}, {'timeout': 0}, {'max_retries': -1}],
)
def test_bad_options(options):
    with pytest.raises(ConfigurationError):
        Oscar(api_key=KEY, **options)


def test_missing_credential_sends_nothing(api, hooks):
    route = api.get('/api/things/t1/').respond(200, json={'id': 't1'})
    with Oscar(base_url=BASE, **hooks.sync_options()) as oscar, pytest.raises(AuthenticationError) as info:
        oscar.request(spec(), response_type=Thing, options=None)
    assert info.value.status is None
    assert not route.called


def test_token_provider_is_called_before_each_attempt(api, hooks):
    tokens = iter(['t1', 't2'])
    route = api.get('/api/things/t1/')
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={'id': 't1'})]
    with Oscar(token_provider=lambda: next(tokens), base_url=BASE, **hooks.sync_options()) as oscar:
        oscar.request(spec(), response_type=Thing, options=None)
    assert [call.request.headers['authorization'] for call in route.calls] == ['Bearer t1', 'Bearer t2']


@pytest.mark.parametrize(
    ('status', 'kind'),
    [(400, BadRequestError), (404, NotFoundError), (409, ConflictError), (401, AuthenticationError)],
)
def test_errors_are_not_retried(api, client, hooks, status, kind):
    route = api.get('/api/things/t1/').respond(
        status, json=problem('some_code', status), headers={'X-Request-Id': 'r9'}
    )
    with pytest.raises(kind) as info:
        client.request(spec(), response_type=Thing, options=None)
    assert info.value.code == 'some_code'
    assert info.value.request_id == 'r9'
    assert route.call_count == 1
    assert hooks.sleeps == []


def test_get_is_retried_with_backoff(api, client, hooks):
    hooks.jitter_value = 0.5
    route = api.get('/api/things/t1/')
    route.side_effect = [httpx.Response(500), httpx.Response(502), httpx.Response(200, json={'id': 't1'})]
    assert client.request(spec(), response_type=Thing, options=None).id == 't1'
    assert route.call_count == 3
    assert hooks.sleeps == [0.5 * 0.875, 1.0 * 0.875]


def test_retries_end_with_the_last_error(api, client, hooks):
    route = api.get('/api/things/t1/').respond(503, json=problem('unavailable', 503))
    with pytest.raises(InternalServerError, match='503 unavailable'):
        client.request(spec(), response_type=Thing, options=None)
    assert route.call_count == 3
    assert hooks.sleeps == [0.5, 1.0]


def test_max_retries_per_call(api, client, hooks):
    route = api.get('/api/things/t1/').respond(503)
    with pytest.raises(InternalServerError):
        client.request(spec(), response_type=Thing, options=RequestOptions(max_retries=0))
    assert route.call_count == 1


def test_retry_after_wins(api, client, hooks):
    route = api.get('/api/things/t1/')
    route.side_effect = [
        httpx.Response(429, headers={'Retry-After': '7'}, json=problem('rate_limited', 429)),
        httpx.Response(200, json={'id': 't1'}),
    ]
    client.request(spec(), response_type=Thing, options=None)
    assert hooks.sleeps == [7.0]


def test_long_retry_after_ends_the_retries(api, client, hooks):
    api.get('/api/things/t1/').respond(429, headers={'Retry-After': '120'}, json=problem('rate_limited', 429))
    with pytest.raises(RateLimitError):
        client.request(spec(), response_type=Thing, options=None)
    assert hooks.sleeps == []


def test_post_without_key_is_sent_once(api, client, hooks):
    route = api.post('/api/things/t1/').respond(503)
    with pytest.raises(InternalServerError):
        client.request(spec(method='POST'), response_type=Thing, options=None)
    assert route.call_count == 1


def test_idempotent_post_reuses_its_key(api, client, hooks):
    route = api.post('/api/things/t1/')
    route.side_effect = [
        httpx.Response(409, json=problem('idempotency_in_progress', 409)),
        httpx.ConnectError('reset'),
        httpx.Response(201, json={'id': 't1'}, headers={'Idempotent-Replayed': 'true'}),
    ]
    client.request(spec(method='POST', idempotent=True), response_type=Thing, options=None)
    keys = [call.request.headers['idempotency-key'] for call in route.calls]
    assert keys == ['00000000-0000-4000-8000-000000000001'] * 3


def test_caller_key(api, client):
    route = api.post('/api/things/t1/').respond(200, json={'id': 't1'})
    options = RequestOptions(idempotency_key='my-key', extra_headers={'X-Trace': '1'})
    client.request(spec(method='POST', idempotent=True), response_type=Thing, options=options)
    assert route.calls.last.request.headers['idempotency-key'] == 'my-key'
    assert route.calls.last.request.headers['x-trace'] == '1'


def test_connection_error_after_retries(api, client, hooks):
    route = api.get('/api/things/t1/').mock(side_effect=httpx.ConnectError('refused'))
    with pytest.raises(_errors.ConnectionError, match=re.escape('GET https://api.example.test/api/things/t1/')) as info:
        client.request(spec(), response_type=Thing, options=None)
    assert isinstance(info.value.__cause__, httpx.ConnectError)
    assert route.call_count == 3


def test_timeout_error(api, client, hooks):
    api.get('/api/things/t1/').mock(side_effect=httpx.ReadTimeout('slow'))
    with pytest.raises(_errors.TimeoutError, match='timed out'):
        client.request(spec(), response_type=Thing, options=RequestOptions(max_retries=1))
    assert hooks.sleeps == [0.5]


def test_invalid_success_body(api, client):
    api.get('/api/things/t1/').respond(200, json={'nope': 1}, headers={'X-Request-Id': 'r1'})
    with pytest.raises(ResponseValidationError) as info:
        client.request(spec(), response_type=Thing, options=None)
    assert info.value.status == 200
    assert info.value.request_id == 'r1'


def test_request_no_content(api, client):
    route = api.delete('/api/things/t1/').respond(204)
    assert client.request_no_content(spec(method='DELETE', response='none'), options=None) is None
    assert route.called


def test_401_with_the_cli_login_fetches_once_more(api, hooks, fake_cli):
    fake_cli.answer('old-token')
    route = api.get('/api/things/t1/')

    def answer(request):
        if request.headers['authorization'] == 'Bearer old-token':
            fake_cli.answer('new-token')
            return httpx.Response(401, json=problem('token_expired', 401))
        return httpx.Response(200, json={'id': 't1'})

    route.side_effect = answer
    with Oscar(credential='cli', base_url=BASE, **hooks.sync_options()) as oscar:
        oscar.request(spec(), response_type=Thing, options=None)
    assert [call.request.headers['authorization'] for call in route.calls] == ['Bearer old-token', 'Bearer new-token']
    assert hooks.sleeps == []


def test_401_with_the_cli_login_twice_goes_to_the_caller(api, hooks, fake_cli):
    fake_cli.answer('token')
    route = api.get('/api/things/t1/').respond(401, json=problem('token_invalid', 401))
    with Oscar(credential='cli', base_url=BASE, **hooks.sync_options()) as oscar, pytest.raises(AuthenticationError):
        oscar.request(spec(), response_type=Thing, options=None)
    assert route.call_count == 2


def test_generated_resource_upload(api, client):
    route = api.post('/api/companies/c1/files/').respond(201, json=STORED)
    stored = client.files.upload('c1', file=('a.pdf', b'%PDF', 'application/pdf'), kind='purchase_invoice')
    assert isinstance(stored, StoredDocument)
    sent = route.calls.last.request
    assert sent.headers['idempotency-key'] == '00000000-0000-4000-8000-000000000001'
    body = sent.read()
    assert b'filename="a.pdf"' in body
    assert b'name="kind"\r\n\r\npurchase_invoice' in body


def test_generated_resource_download(api, client):
    route = api.get('/api/companies/c1/files/f1/content/').respond(
        200,
        content=b'%PDF-1.7',
        headers={'Content-Type': 'application/pdf', 'Content-Disposition': 'inline; filename="a.pdf"'},
    )
    download = client.files.download('c1', 'f1')
    assert download.filename == 'a.pdf'
    assert download.content_type == 'application/pdf'
    assert download.read() == b'%PDF-1.7'
    assert route.calls.last.request.headers['accept'] == '*/*'
    assert 'format' not in route.calls.last.request.url.params


def test_download_error_is_read_and_retried(api, client, hooks):
    route = api.get('/api/companies/c1/files/f1/content/')
    route.side_effect = [httpx.Response(503, content=b'busy'), httpx.Response(404, json=problem('not_found', 404))]
    with pytest.raises(NotFoundError):
        client.files.download('c1', 'f1')
    assert hooks.sleeps == [0.5]


def test_generated_resource_pagination(api, client):
    route = api.get('/api/organizations/')
    route.side_effect = [
        httpx.Response(200, json={'items': [ORGANIZATION], 'next_cursor': 'c2'}),
        httpx.Response(200, json={'items': [dict(ORGANIZATION, id='o2')], 'next_cursor': None}),
    ]
    assert [item.id for item in client.organizations.list(limit=1)] == ['o1', 'o2']
    assert [dict(call.request.url.params) for call in route.calls] == [{'limit': '1'}, {'limit': '1', 'cursor': 'c2'}]


def test_injected_http_client_is_not_closed(hooks):
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'id': 'x'})))
    with Oscar(api_key=KEY, base_url=BASE, http_client=http, **hooks.sync_options()) as oscar:
        assert oscar.request(spec(), response_type=Thing, options=None).id == 'x'
    assert not http.is_closed
    http.close()


def test_default_headers(api, hooks):
    route = api.get('/api/things/t1/').respond(200, json={'id': 't1'})
    with Oscar(api_key=KEY, base_url=BASE, default_headers={'X-Team': 'ops'}, **hooks.sync_options()) as oscar:
        oscar.request(spec(), response_type=Thing, options=None)
    assert route.calls.last.request.headers['x-team'] == 'ops'


def test_json_body_is_sent(api, client):
    route = api.post('/api/things/t1/').respond(200, json={'id': 't1'})
    body = JsonBody(Thing(id='new'))
    client.request(spec(method='POST', body=body), response_type=Thing, options=None)
    sent = route.calls.last.request
    assert sent.headers['content-type'] == 'application/json'
    assert json.loads(sent.content) == {'id': 'new'}


def test_default_key_is_a_uuid4(api):
    route = api.post('/api/things/t1/').respond(200, json={'id': 't1'})
    with Oscar(api_key=KEY, base_url=BASE) as oscar:
        oscar.request(spec(method='POST', idempotent=True), response_type=Thing, options=None)
    key = uuid.UUID(route.calls.last.request.headers['idempotency-key'])
    assert key.version == 4
    assert str(key) == route.calls.last.request.headers['idempotency-key']


def test_default_jitter_and_sleep(api, monkeypatch):
    slept = []
    monkeypatch.setattr('time.sleep', slept.append)
    route = api.get('/api/things/t1/')
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={'id': 't1'})]
    with Oscar(api_key=KEY, base_url=BASE) as oscar:
        oscar.request(spec(), response_type=Thing, options=None)
    assert len(slept) == 1
    assert 0.375 < slept[0] <= 0.5
