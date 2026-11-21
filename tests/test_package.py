import tomllib
from pathlib import Path

import httpx
import pytest

import oscaraccountant
from oscaraccountant import AsyncOscar, AsyncPaginator, Oscar, SyncPaginator, types
from oscaraccountant.types import BooksInvoice, BooksInvoicePage

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_the_project():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert project['project']['version'] == oscaraccountant.__version__


def test_version_matches_clients_version():
    version_file = ROOT.parent / 'VERSION'
    if not version_file.exists():
        pytest.skip('outside the source repository')
    assert version_file.read_text(encoding='utf-8').strip() == oscaraccountant.__version__


def test_exports():
    for name in oscaraccountant.__all__:
        assert hasattr(oscaraccountant, name)
    assert types.BooksInvoice.__name__ == 'BooksInvoice'
    assert (ROOT / 'src' / 'oscaraccountant' / 'py.typed').exists()


def _empty_page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={'items': [], 'next_cursor': None})


def test_the_paginator_types_are_public() -> None:
    # Typed on purpose: mypy --strict checks the annotations against the return type of `list()`.
    assert oscaraccountant.SyncPaginator is types.SyncPaginator
    assert oscaraccountant.AsyncPaginator is types.AsyncPaginator
    with Oscar(api_key='osk_test', http_client=httpx.Client(transport=httpx.MockTransport(_empty_page))) as oscar:
        invoices: SyncPaginator[BooksInvoicePage, BooksInvoice] = oscar.invoices.list('company')
        page: BooksInvoicePage = invoices.first_page()
        items: list[BooksInvoice] = list(invoices)
    assert page.items == []
    assert items == []


@pytest.mark.anyio
async def test_the_async_paginator_type_is_public() -> None:
    transport = httpx.MockTransport(_empty_page)
    async with AsyncOscar(api_key='osk_test', http_client=httpx.AsyncClient(transport=transport)) as oscar:
        invoices: AsyncPaginator[BooksInvoicePage, BooksInvoice] = oscar.invoices.list('company')
        page: BooksInvoicePage = await invoices.first_page()
        items: list[BooksInvoice] = [invoice async for invoice in invoices]
    assert page.items == []
    assert items == []
