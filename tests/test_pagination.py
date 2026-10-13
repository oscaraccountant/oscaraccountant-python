import httpx
import pytest

from oscaraccountant import AsyncOscar, Oscar

from .conftest import BASE, KEY
from .helpers import Thing, ThingPage, spec


def _list_spec():
    return spec(
        operation_id='things_list', path='/api/things/', path_params={}, query={'state': 'open', 'cursor': None}
    )


def _pages(route):
    route.side_effect = [
        httpx.Response(200, json={'items': [{'id': 'a'}, {'id': 'b'}], 'next_cursor': 'c2'}),
        httpx.Response(200, json={'items': [], 'next_cursor': 'c3'}),
        httpx.Response(200, json={'items': [{'id': 'c'}], 'next_cursor': None}),
    ]


def test_first_page(api, hooks):
    route = api.get('/api/things/').respond(200, json={'items': [{'id': 'a'}], 'next_cursor': 'c2'})
    with Oscar(api_key=KEY, base_url=BASE, **hooks.sync_options()) as oscar:
        page = oscar.paginate(_list_spec(), page_type=ThingPage, item_type=Thing, options=None).first_page()
    assert page.next_cursor == 'c2'
    assert route.call_count == 1
    assert dict(route.calls.last.request.url.params) == {'state': 'open'}


def test_every_item_of_every_page(api, hooks):
    route = api.get('/api/things/')
    _pages(route)
    with Oscar(api_key=KEY, base_url=BASE, **hooks.sync_options()) as oscar:
        paginator = oscar.paginate(_list_spec(), page_type=ThingPage, item_type=Thing, options=None)
        assert route.call_count == 0
        items = iter(paginator)
        assert next(items).id == 'a'
        assert route.call_count == 1
        assert [item.id for item in items] == ['b', 'c']
    assert [dict(call.request.url.params) for call in route.calls] == [
        {'state': 'open'},
        {'state': 'open', 'cursor': 'c2'},
        {'state': 'open', 'cursor': 'c3'},
    ]


def test_pages(api, hooks):
    _pages(api.get('/api/things/'))
    with Oscar(api_key=KEY, base_url=BASE, **hooks.sync_options()) as oscar:
        pages = list(oscar.paginate(_list_spec(), page_type=ThingPage, item_type=Thing, options=None).pages())
    assert [page.next_cursor for page in pages] == ['c2', 'c3', None]


@pytest.mark.anyio
async def test_async_pages(api, hooks):
    route = api.get('/api/things/')
    _pages(route)
    async with AsyncOscar(api_key=KEY, base_url=BASE, **hooks.async_options()) as oscar:
        paginator = oscar.paginate(_list_spec(), page_type=ThingPage, item_type=Thing, options=None)
        assert [item.id async for item in paginator] == ['a', 'b', 'c']
        _pages(route)
        assert [page.next_cursor async for page in paginator.pages()] == ['c2', 'c3', None]
        route.side_effect = None
        route.return_value = httpx.Response(200, json={'items': [], 'next_cursor': None})
        assert (await paginator.first_page()).items == []
