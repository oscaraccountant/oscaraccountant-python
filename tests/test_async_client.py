import httpx
import pytest

from oscaraccountant import (
    AsyncOscar,
    AuthenticationError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    RequestOptions,
    _errors,
)
from oscaraccountant.types import StoredDocument

from .conftest import BASE, KEY
from .helpers import ORGANIZATION, STORED, Thing, problem, spec

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(hooks):
    async with AsyncOscar(api_key=KEY, base_url=BASE, **hooks.async_options()) as oscar:
        yield oscar


async def test_request(api, client):
    route = api.get('/api/things/t1/').respond(200, json={'id': 't1'})
    assert (await client.request(spec(), response_type=Thing, options=None)).id == 't1'
    assert route.calls.last.request.headers['authorization'] == f'Bearer {KEY}'
    assert client.base_url == BASE


async def test_retries(api, client, hooks):
    route = api.get('/api/things/t1/')
    route.side_effect = [
        httpx.ConnectError('reset'),
        httpx.Response(429, headers={'Retry-After': '3'}),
        httpx.Response(200, json={'id': 't1'}),
    ]
    await client.request(spec(), response_type=Thing, options=None)
    assert hooks.sleeps == [0.5, 3.0]


async def test_retries_end(api, client, hooks):
    api.get('/api/things/t1/').respond(500, json=problem('boom', 500))
    with pytest.raises(InternalServerError):
        await client.request(spec(), response_type=Thing, options=None)
    assert len(hooks.sleeps) == 2


async def test_long_retry_after(api, client, hooks):
    api.get('/api/things/t1/').respond(429, headers={'Retry-After': '61'})
    with pytest.raises(RateLimitError):
        await client.request(spec(), response_type=Thing, options=None)
    assert hooks.sleeps == []


async def test_timeout(api, client):
    api.get('/api/things/t1/').mock(side_effect=httpx.ConnectTimeout('slow'))
    with pytest.raises(_errors.TimeoutError):
        await client.request(spec(), response_type=Thing, options=RequestOptions(max_retries=0))


async def test_idempotent_post(api, client):
    route = api.post('/api/things/t1/')
    route.side_effect = [httpx.Response(502), httpx.Response(200, json={'id': 't1'})]
    await client.request(spec(method='POST', idempotent=True), response_type=Thing, options=None)
    assert {call.request.headers['idempotency-key'] for call in route.calls} == {'00000000-0000-4000-8000-000000000001'}


async def test_async_token_provider(api, hooks):
    async def provider():
        return 'async-token'

    route = api.get('/api/things/t1/').respond(200, json={'id': 't1'})
    async with AsyncOscar(token_provider=provider, base_url=BASE, **hooks.async_options()) as oscar:
        await oscar.request(spec(), response_type=Thing, options=None)
    assert route.calls.last.request.headers['authorization'] == 'Bearer async-token'


async def test_missing_credential(api, hooks):
    async with AsyncOscar(base_url=BASE, **hooks.async_options()) as oscar:
        with pytest.raises(AuthenticationError):
            await oscar.request(spec(), response_type=Thing, options=None)


async def test_401_with_the_cli_login(api, hooks, fake_cli):
    fake_cli.answer('old-token')
    route = api.get('/api/things/t1/')

    def answer(request):
        if request.headers['authorization'] == 'Bearer old-token':
            fake_cli.answer('new-token')
            return httpx.Response(401, json=problem('token_expired', 401))
        return httpx.Response(200, json={'id': 't1'})

    route.side_effect = answer
    async with AsyncOscar(credential='cli', base_url=BASE, **hooks.async_options()) as oscar:
        await oscar.request(spec(), response_type=Thing, options=None)
    assert route.call_count == 2


async def test_request_no_content(api, client):
    route = api.delete('/api/things/t1/').respond(204)
    await client.request_no_content(spec(method='DELETE', response='none'), options=None)
    assert route.called


async def test_upload_download_and_pages(api, client):
    api.post('/api/companies/c1/files/').respond(201, json=STORED)
    stored = await client.files.upload('c1', file=b'%PDF')
    assert isinstance(stored, StoredDocument)
    api.get('/api/companies/c1/files/f1/content/').respond(200, content=b'data')
    download = await client.files.download('c1', 'f1')
    assert await download.aread() == b'data'
    route = api.get('/api/organizations/')
    route.side_effect = [
        httpx.Response(200, json={'items': [ORGANIZATION], 'next_cursor': 'c2'}),
        httpx.Response(200, json={'items': [dict(ORGANIZATION, id='o2')], 'next_cursor': None}),
    ]
    assert [item.id async for item in client.organizations.list()] == ['o1', 'o2']


async def test_download_error(api, client):
    api.get('/api/companies/c1/files/f1/content/').respond(404, json=problem('not_found', 404))
    with pytest.raises(NotFoundError):
        await client.files.download('c1', 'f1')


async def test_injected_http_client_is_not_closed(hooks):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'id': 'x'})))
    async with AsyncOscar(api_key=KEY, base_url=BASE, http_client=http, **hooks.async_options()) as oscar:
        assert (await oscar.request(spec(), response_type=Thing, options=None)).id == 'x'
    assert not http.is_closed
    await http.aclose()


async def test_default_sleep_is_asyncio(api):
    route = api.get('/api/things/t1/')
    route.side_effect = [httpx.Response(503, headers={'Retry-After': '0'}), httpx.Response(200, json={'id': 't1'})]
    async with AsyncOscar(api_key=KEY, base_url=BASE) as oscar:
        await oscar.request(spec(), response_type=Thing, options=None)
    assert route.call_count == 2
