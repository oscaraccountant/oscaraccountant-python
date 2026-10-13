"""The SDK against clients/mock-server over real HTTP.

The mock server checks every request against the OpenAPI document of the public API and refuses what the API
would not accept, so a request the SDK builds wrongly fails here even when the unit tests pass.
"""

import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from oscaraccountant import (
    AsyncOscar,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    ConnectionError,
    InternalServerError,
    NotFoundError,
    Oscar,
    PermissionDeniedError,
    RateLimitError,
    RequestOptions,
    TimeoutError,
    _cli_login,
)
from oscaraccountant.types import (
    CancelInvoiceBodyRequest,
    CreatePartyPayloadRequest,
    IssueInvoiceBodyRequest,
    RecordInvoicePayloadRequest,
    ReviseInvoiceBodyRequest,
)

from .conftest import MockServer

pytestmark = pytest.mark.integration

INVOICE = '01J90000000000000000000116'
PARTY = '01J90000000000000000000179'
DOCUMENT = '01J90000000000000000000096'
BANK_TRANSACTION = '01J90000000000000000000029'
SEEDED_FILE = 'fil0000001'
UUID_V4 = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
PDF = b'%PDF-1.4\n% integration test\n'

REVISION = {
    'invoice_date': '2026-09-01',
    'due_date': '2026-09-15',
    'currency': 'EUR',
    'lines': [{'description': 'Consulting', 'net_minor': 10000, 'vat_code': 'VAT24', 'tax_minor': 2400}],
}
RECORD = {
    'direction': 'purchase',
    'party_id': PARTY,
    'number': 'INV-2026-001',
    'revision': REVISION,
}


def record_body(number: str = 'INV-2026-001') -> RecordInvoicePayloadRequest:
    return RecordInvoicePayloadRequest.model_validate({**RECORD, 'number': number})


class Sleeps:
    """The injected sleep: it records the delay and does not wait."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)

    async def asleep(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def sleeps():
    return Sleeps()


@pytest.fixture
def oscar(server, sleeps):
    with Oscar(api_key=server.api_key, base_url=server.url, sleep=sleeps, jitter=lambda: 0.0) as client:
        yield client


def api_requests(server: MockServer) -> list[dict[str, Any]]:
    return [entry for entry in server.requests() if entry['path'].startswith('/api/')]


# ---------------------------------------------------------------------------
# Every resource
# ---------------------------------------------------------------------------


def test_every_operation(server, oscar):
    company = server.company_id
    organization = server.organization_id

    # Lists: one page, and the iterator.
    assert [account.code for account in oscar.accounts.list(company)]
    assert oscar.bank_accounts.list(company, limit=20).first_page().items
    assert list(oscar.bank_transactions.list(company, date_from=dt.date(2026, 1, 1)))
    assert list(oscar.dimensions.list(company)) is not None
    assert list(oscar.documents.list(company))
    assert list(oscar.invoices.list(company))
    assert list(oscar.parties.list(company))
    assert list(oscar.periods.list(company))
    assert list(oscar.vat_codes.list(company))
    assert [item.id for item in oscar.organizations.list()] == [organization]
    assert [item.id for item in oscar.companies.list(organization)] == [company]
    assert list(oscar.files.list(company))

    # Single objects.
    assert oscar.bank_transactions.retrieve(company, BANK_TRANSACTION).id == BANK_TRANSACTION
    assert oscar.documents.retrieve(company, DOCUMENT).id == DOCUMENT
    assert oscar.invoices.retrieve(company, INVOICE).id == INVOICE
    assert oscar.parties.retrieve(company, PARTY).id == PARTY
    assert oscar.companies.retrieve(company).id == company
    assert oscar.organizations.retrieve(organization).id == organization
    assert oscar.me.retrieve().credential.kind == 'api_key'
    assert oscar.files.retrieve(company, SEEDED_FILE).id == SEEDED_FILE

    # Reports.
    oscar.general_ledger.retrieve(company, date_from='2026-01-01', date_to='2026-09-30')
    oscar.open_items.retrieve(company, as_of=dt.date(2026, 9, 30))
    oscar.reports.balance_sheet(company, date=dt.date(2026, 9, 30), simplified=True)
    oscar.reports.profit_and_loss(company, date_from='2026-01-01', date_to='2026-09-30')
    oscar.trial_balance.retrieve(company, period='2026-09')
    oscar.vat_preview.retrieve(company, period_from='2026-09-01', period_to='2026-09-30')

    # The invoice inbox.
    assert oscar.inbox.retrieve(company).address.endswith('@inbox.oscaraccountant.com')
    assert [email.id for email in oscar.inbox.list_emails(company)]
    oscar.inbox.rotate(company)

    # Commands.
    oscar.invoices.record(company, record_body())
    oscar.invoices.cancel(company, INVOICE, CancelInvoiceBodyRequest(reason='Entered twice'))
    oscar.invoices.issue(company, INVOICE, body=IssueInvoiceBodyRequest(number='S-2026-001'))
    oscar.invoices.issue(company, INVOICE)
    oscar.invoices.revise(
        company, INVOICE, ReviseInvoiceBodyRequest.model_validate({'reason': 'Wrong date', 'revision': REVISION})
    )
    oscar.parties.create(
        company, CreatePartyPayloadRequest.model_validate({'name': 'Supplier OÜ', 'kind': 'supplier', 'country': 'EE'})
    )

    # Files.
    stored = oscar.files.upload(company, file=('a.pdf', PDF, 'application/pdf'), kind='receipt')
    assert oscar.files.download(company, stored.id).read() == PDF

    log = api_requests(server)
    assert [entry for entry in log if entry['errors'] or entry['status'] >= 400] == []
    assert len({entry['operation_id'] for entry in log}) == 36


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_walks_every_page(server, oscar):
    invoices = [invoice.id for invoice in oscar.invoices.list(server.company_id, limit=1)]

    assert len(invoices) == 3
    assert len(set(invoices)) == 3
    queries = [dict(entry['query']) for entry in api_requests(server)]
    assert [query.get('limit') for query in queries] == ['1', '1', '1']
    assert 'cursor' not in queries[0]
    assert all('cursor' in query for query in queries[1:])


def test_pagination_one_page_and_pages(server, oscar):
    paginator = oscar.invoices.list(server.company_id, limit=2)

    first = paginator.first_page()
    pages = list(paginator.pages())

    assert len(first.items) == 2
    assert first.next_cursor is not None
    assert [len(page.items) for page in pages] == [2, 1]
    assert pages[-1].next_cursor is None


def test_pagination_is_lazy(server, oscar):
    items = iter(oscar.invoices.list(server.company_id, limit=1))
    assert api_requests(server) == []

    next(items)

    assert len(api_requests(server)) == 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_not_found(server, oscar):
    with pytest.raises(NotFoundError) as caught:
        oscar.invoices.retrieve(server.company_id, '01J99999999999999999999999')

    assert caught.value.status == 404
    assert caught.value.code == 'not_found'
    assert str(caught.value).startswith('404 not_found')


def test_another_company_is_not_found(server, oscar):
    with pytest.raises(NotFoundError):
        oscar.companies.retrieve('cmp9999999')


def test_wrong_api_key(server):
    with (
        Oscar(api_key='osk_wrong', base_url=server.url) as client,
        pytest.raises(AuthenticationError) as caught,
    ):
        client.me.retrieve()

    assert caught.value.status == 401
    assert caught.value.code == 'authentication_failed'
    assert 'osk_wrong' not in str(caught.value)


def test_read_only_key_cannot_write(server):
    key = server.control('POST', 'api-keys', {'scope': 'read'})['key']
    with Oscar(api_key=key, base_url=server.url) as client:
        assert client.me.retrieve().credential.scopes == ['read']
        with pytest.raises(PermissionDeniedError) as caught:
            client.invoices.record(server.company_id, record_body())

    assert caught.value.status == 403
    assert caught.value.code == 'insufficient_scope'


def test_validation_error(server, oscar):
    with pytest.raises(BadRequestError) as caught:
        oscar.invoices.list(server.company_id, limit=500).first_page()

    assert caught.value.code == 'validation_error'
    assert [error['field'] for error in caught.value.errors] == ['query.limit']


def test_no_credential_sends_nothing(server, monkeypatch):
    with Oscar(base_url=server.url) as client, pytest.raises(AuthenticationError) as caught:
        client.me.retrieve()

    assert caught.value.status is None
    assert api_requests(server) == []


# ---------------------------------------------------------------------------
# Retries and timeouts
# ---------------------------------------------------------------------------

BUSY = {'kind': 'json', 'value': {'type': 'about:blank', 'title': 'Busy', 'status': 503, 'code': 'busy'}}


def test_retry_after_503(server, oscar, sleeps):
    server.script({'match': {'path': '/api/me/'}, 'times': 2, 'response': {'status': 503, 'body': BUSY}})

    assert oscar.me.retrieve().user.email

    assert sleeps.delays == [0.5, 1.0]
    assert [entry['status'] for entry in api_requests(server)] == [503, 503, 200]


def test_retries_exhausted(server, oscar, sleeps):
    server.script({'match': {'path': '/api/me/'}, 'times': 3, 'response': {'status': 503, 'body': BUSY}})

    with pytest.raises(InternalServerError) as caught:
        oscar.me.retrieve()

    assert caught.value.code == 'busy'
    assert sleeps.delays == [0.5, 1.0]
    assert len(api_requests(server)) == 3


def test_retry_after_header(server, oscar, sleeps):
    throttled = {'kind': 'json', 'value': {'title': 'Slow down', 'status': 429, 'code': 'throttled'}}
    server.script(
        {'match': {'path': '/api/me/'}, 'response': {'status': 429, 'headers': {'retry-after': '2'}, 'body': throttled}}
    )

    oscar.me.retrieve()

    assert sleeps.delays == [2.0]


def test_retry_after_too_long_ends_the_retries(server, oscar, sleeps):
    throttled = {'kind': 'json', 'value': {'title': 'Slow down', 'status': 429, 'code': 'throttled'}}
    server.script(
        {
            'match': {'path': '/api/me/'},
            'response': {'status': 429, 'headers': {'retry-after': '120'}, 'body': throttled},
        }
    )

    with pytest.raises(RateLimitError):
        oscar.me.retrieve()

    assert sleeps.delays == []


def test_connection_closed_is_retried(server, oscar, sleeps):
    server.script({'match': {'path': '/api/me/'}, 'fault': 'close'})

    oscar.me.retrieve()

    assert sleeps.delays == [0.5]


def test_connection_error_after_the_last_retry(server, oscar, sleeps):
    server.script({'match': {'path': '/api/me/'}, 'times': 3, 'fault': 'close'})

    with pytest.raises(ConnectionError):
        oscar.me.retrieve()

    assert sleeps.delays == [0.5, 1.0]


def test_timeout_is_retried(server, sleeps):
    server.script({'match': {'path': '/api/me/'}, 'delay_ms': 1500})
    with Oscar(api_key=server.api_key, base_url=server.url, timeout=0.3, sleep=sleeps, jitter=lambda: 0.0) as client:
        client.me.retrieve()

    assert sleeps.delays == [0.5]


def test_timeout_of_a_call(server, oscar):
    server.script({'match': {'path': '/api/me/'}, 'delay_ms': 1500})

    with pytest.raises(TimeoutError):
        oscar.me.retrieve(options=RequestOptions(timeout=0.3, max_retries=0))


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_retry_sends_the_same_key(server, oscar, sleeps):
    server.script(
        {'match': {'operation_id': 'commands_record_invoice'}, 'response': {'status': 503, 'body': BUSY}},
    )

    outcome = oscar.invoices.record(server.company_id, record_body())

    keys = [entry['headers']['idempotency-key'] for entry in api_requests(server)]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert UUID_V4.match(keys[0])
    assert outcome.replayed is False


def test_each_call_gets_its_own_key(server, oscar):
    oscar.invoices.record(server.company_id, record_body())
    oscar.invoices.record(server.company_id, record_body())

    keys = [entry['headers']['idempotency-key'] for entry in api_requests(server)]
    assert keys[0] != keys[1]


def test_caller_key_replays(server, oscar):
    options = RequestOptions(idempotency_key='record-inv-2026-001')

    first = oscar.invoices.record(server.company_id, record_body(), options=options)
    second = oscar.invoices.record(server.company_id, record_body(), options=options)

    assert first.replayed is False
    assert second.replayed is True
    assert [entry['headers']['idempotency-key'] for entry in api_requests(server)] == ['record-inv-2026-001'] * 2


def test_caller_key_with_another_body_conflicts(server, oscar):
    options = RequestOptions(idempotency_key='record-inv-2026-002')
    oscar.invoices.record(server.company_id, record_body('INV-1'), options=options)

    with pytest.raises(ConflictError) as caught:
        oscar.invoices.record(server.company_id, record_body('INV-2'), options=options)

    assert caught.value.code == 'idempotency_conflict'


def test_no_key_on_a_get(server, oscar):
    oscar.me.retrieve()

    assert 'idempotency-key' not in api_requests(server)[0]['headers']


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_upload_and_download(server, oscar):
    stored = oscar.files.upload(
        server.company_id,
        file=('receipt.pdf', PDF, 'application/pdf'),
        kind='receipt',
        received_on=dt.date(2026, 9, 20),
    )

    assert stored.name == 'receipt.pdf'
    assert stored.size_bytes == len(PDF)
    assert stored.sha256 == hashlib.sha256(PDF).hexdigest()
    assert stored.id in [item.id for item in oscar.files.list(server.company_id)]
    download = oscar.files.download(server.company_id, stored.id)
    assert download.filename == 'receipt.pdf'
    assert download.content_type is not None
    assert download.content_type.startswith('application/pdf')
    assert b''.join(download.iter_bytes()) == PDF
    upload = api_requests(server)[0]
    assert {part['name'] for part in upload['body']['parts']} == {'file', 'kind', 'received_on'}


def test_upload_from_an_open_file(server, oscar, tmp_path):
    path = tmp_path / 'scan.pdf'
    path.write_bytes(PDF)

    with path.open('rb') as handle:
        stored = oscar.files.upload(server.company_id, file=handle)

    assert stored.name == 'scan.pdf'


def test_upload_retry_sends_the_same_bytes(server, oscar, sleeps):
    server.script({'match': {'operation_id': 'companies_files_create'}, 'response': {'status': 503, 'body': BUSY}})

    oscar.files.upload(server.company_id, file=('a.pdf', PDF, 'application/pdf'))

    first, second = api_requests(server)
    assert first['body']['parts'] == second['body']['parts']
    assert first['headers']['idempotency-key'] == second['headers']['idempotency-key']


def test_download_of_the_seeded_file(server, oscar):
    with oscar.files.download(server.company_id, SEEDED_FILE) as download:
        content = download.read()

    assert hashlib.sha256(content).hexdigest() == oscar.files.retrieve(server.company_id, SEEDED_FILE).sha256
    assert download.filename == 'invoice.pdf'


# ---------------------------------------------------------------------------
# The async client
# ---------------------------------------------------------------------------


@pytest.fixture
async def aoscar(server, sleeps):
    async with AsyncOscar(
        api_key=server.api_key, base_url=server.url, sleep=sleeps.asleep, jitter=lambda: 0.0
    ) as client:
        yield client


@pytest.mark.anyio
async def test_async_pagination_and_retrieve(server, aoscar):
    invoices = [invoice.id async for invoice in aoscar.invoices.list(server.company_id, limit=1)]

    assert len(invoices) == 3
    assert (await aoscar.invoices.retrieve(server.company_id, invoices[0])).id == invoices[0]
    assert (await aoscar.invoices.list(server.company_id, limit=2).first_page()).next_cursor is not None


@pytest.mark.anyio
async def test_async_retry_and_command(server, aoscar, sleeps):
    server.script({'match': {'operation_id': 'commands_record_invoice'}, 'response': {'status': 503, 'body': BUSY}})

    outcome = await aoscar.invoices.record(server.company_id, record_body())

    assert outcome.replayed is False
    assert sleeps.delays == [0.5]


@pytest.mark.anyio
async def test_async_upload_and_download(server, aoscar):
    stored = await aoscar.files.upload(server.company_id, file=('b.pdf', PDF, 'application/pdf'))

    download = await aoscar.files.download(server.company_id, stored.id)
    chunks = [chunk async for chunk in download.aiter_bytes()]

    assert b''.join(chunks) == PDF
    assert download.filename == 'b.pdf'


@pytest.mark.anyio
async def test_async_error(server, aoscar):
    with pytest.raises(NotFoundError):
        await aoscar.parties.retrieve(server.company_id, '01J99999999999999999999999')


# ---------------------------------------------------------------------------
# Step 3: the login of the CLI, with a fake `oscar` on PATH
# ---------------------------------------------------------------------------

FAKE_OSCAR = """
import json, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
with (here / 'calls.log').open('a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\\n')
if args == ['--version']:
    print('oscar-cli 0.1.0')
    sys.exit(0)
if args[:3] != ['auth', 'token', '--json'] or '--base-url' not in args:
    sys.stderr.write('usage: oscar auth token --json --base-url URL\\n')
    sys.exit(2)
state_path = here / 'state.json'
state = json.loads(state_path.read_text(encoding='utf-8'))
base_url = args[args.index('--base-url') + 1]
if base_url != state['base_url']:
    sys.stderr.write('No profile has the base URL ' + base_url + '. Run oscar login.\\n')
    sys.exit(3)
tokens = state['tokens']
token = tokens.pop(0) if len(tokens) > 1 else tokens[0]
state_path.write_text(json.dumps(state), encoding='utf-8')
profile = args[args.index('--profile') + 1] if '--profile' in args else 'default'
answer = {'token': token, 'expires_at': state['expires_at'], 'kind': 'oauth', 'base_url': state['answer_base_url'],
          'profile': profile}
print(json.dumps(answer))
"""


class FakeCli:
    """A fake `oscar` executable, first on PATH: a Python script, run through a `.cmd` file on Windows."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        if os.name == 'nt':
            script = directory / 'oscar.py'
            script.write_text(FAKE_OSCAR, encoding='utf-8')
            (directory / 'oscar.cmd').write_text(f'@"{sys.executable}" "%~dp0oscar.py" %*\r\n', encoding='utf-8')
        else:
            script = directory / 'oscar'
            script.write_text(f'#!{sys.executable}\n{FAKE_OSCAR}', encoding='utf-8')
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def serve(self, base_url: str, *tokens: str, expires_at: str | None, answer_base_url: str | None = None) -> None:
        state = {
            'base_url': base_url,
            'answer_base_url': answer_base_url or base_url,
            'tokens': list(tokens),
            'expires_at': expires_at,
        }
        (self.directory / 'state.json').write_text(json.dumps(state), encoding='utf-8')

    def calls(self) -> list[list[str]]:
        log = self.directory / 'calls.log'
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    directory = tmp_path / 'bin'
    directory.mkdir()
    monkeypatch.setenv('PATH', f'{directory}{os.pathsep}{os.environ.get("PATH", "")}')
    _cli_login._checked.clear()
    return FakeCli(directory)


def device_token(server: MockServer) -> str:
    """An OAuth access token of the mock server, through the device grant, as `oscar login --device` gets it."""
    grant = httpx.post(
        f'{server.url}/oauth/device_authorization',
        data={'client_id': 'oscar-cli', 'scope': 'read write', 'resource': server.resource},
        timeout=10,
    ).json()
    server.control('POST', 'device/approve', {'device_code': grant['device_code']})
    tokens = httpx.post(
        f'{server.url}/oauth/token',
        data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'device_code': grant['device_code'],
            'client_id': 'oscar-cli',
        },
        timeout=10,
    )
    tokens.raise_for_status()
    token: str = tokens.json()['access_token']
    return token


def in_one_hour() -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_cli_login_with_the_option(server, fake_cli):
    fake_cli.serve(server.url, device_token(server), expires_at=in_one_hour())

    with Oscar(credential='cli', base_url=server.url) as client:
        me = client.me.retrieve()
        client.me.retrieve()

    assert me.credential.kind == 'oauth'
    # The version once, then one token that the SDK keeps until it expires.
    assert fake_cli.calls() == [['--version'], ['auth', 'token', '--json', '--base-url', server.url]]


def test_cli_login_with_the_variable_and_a_profile(server, fake_cli, monkeypatch):
    fake_cli.serve(server.url, device_token(server), expires_at=in_one_hour())
    monkeypatch.setenv('OSCAR_USE_CLI_LOGIN', '1')
    monkeypatch.setenv('OSCAR_PROFILE', 'work')

    with Oscar(base_url=server.url) as client:
        client.organizations.retrieve(server.organization_id)

    assert fake_cli.calls()[-1] == ['auth', 'token', '--json', '--base-url', server.url, '--profile', 'work']


def test_cli_login_asks_again_after_a_401(server, fake_cli):
    fake_cli.serve(server.url, 'revoked-token', device_token(server), expires_at=in_one_hour())

    with Oscar(credential='cli', base_url=server.url) as client:
        assert client.me.retrieve().credential.kind == 'oauth'

    assert [entry['status'] for entry in api_requests(server)] == [401, 200]
    assert len([call for call in fake_cli.calls() if call[:2] == ['auth', 'token']]) == 2


def test_cli_login_expired_token_is_asked_again(server, fake_cli):
    token = device_token(server)
    fake_cli.serve(server.url, token, expires_at='2026-01-01T00:00:00Z')

    with Oscar(credential='cli', base_url=server.url) as client:
        client.me.retrieve()
        client.me.retrieve()

    assert len([call for call in fake_cli.calls() if call[:2] == ['auth', 'token']]) == 2


def test_cli_login_refuses_another_base_url(server, fake_cli):
    fake_cli.serve(server.url, device_token(server), expires_at=in_one_hour(), answer_base_url='https://other.test')

    with Oscar(credential='cli', base_url=server.url) as client, pytest.raises(AuthenticationError) as caught:
        client.me.retrieve()

    assert caught.value.status is None
    assert api_requests(server) == []


def test_cli_login_not_logged_in(server, fake_cli):
    fake_cli.serve('https://another.example.test', 'token', expires_at=None)

    with Oscar(credential='cli', base_url=server.url) as client, pytest.raises(AuthenticationError) as caught:
        client.me.retrieve()

    assert 'oscar login' in str(caught.value)
    assert api_requests(server) == []


@pytest.mark.anyio
async def test_cli_login_async(server, fake_cli):
    fake_cli.serve(server.url, device_token(server), expires_at=in_one_hour())

    async with AsyncOscar(credential='cli', base_url=server.url) as client:
        me = await client.me.retrieve()

    assert me.credential.kind == 'oauth'
