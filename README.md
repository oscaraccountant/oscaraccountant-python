# oscaraccountant

The Python SDK of the Oscar accounting API.

- Python 3.11 and later.
- A sync client, `Oscar`, and an async client, `AsyncOscar`.
- Typed models (pydantic v2) for every request and response.
- Two dependencies: `httpx` and `pydantic`.

## Install

```bash
pip install oscaraccountant
# or
uv add oscaraccountant
```

## Quick start

```python
from oscaraccountant import Oscar

oscar = Oscar()  # reads OSCAR_API_KEY

me = oscar.me.retrieve()
for invoice in oscar.invoices.list(company_id, state='issued'):
    print(invoice.id, invoice.state)
```

The async client has the same resources and methods:

```python
import asyncio

from oscaraccountant import AsyncOscar


async def main() -> None:
    async with AsyncOscar() as oscar:
        async for invoice in oscar.invoices.list(company_id, state='issued'):
            print(invoice.id)


asyncio.run(main())
```

Close a client when you do not need it any more: `oscar.close()`,
`await oscar.aclose()`, or use it as a context manager.

## Authentication

The client finds its credential in this order. The first match wins.

1. The option `api_key='osk_...'`, or `token_provider=callback`. A token
   provider returns an access token. The client calls it before each request
   and each retry. For `AsyncOscar`, the callback may be async.
2. The environment variable `OSCAR_API_KEY`.
3. The login of the `oscar` CLI, **only when you ask for it** (see below).
4. Otherwise each call raises `AuthenticationError` and sends nothing.

An empty string counts as not set. The client sends the credential as
`Authorization: Bearer <value>`.

### Use the login of the `oscar` CLI

On a developer machine, you can use the login of the `oscar` CLI instead of
an API key:

```bash
npx @oscaraccountant/cli login
```

```python
oscar = Oscar(credential='cli')  # or set OSCAR_USE_CLI_LOGIN=1
oscar = Oscar(credential='cli', profile='work')  # or set OSCAR_PROFILE=work
```

The SDK never starts a process unless you ask for it. So a server with no
`OSCAR_API_KEY` fails, and does not use the login of a developer by accident.

When you ask for it, the SDK:

- finds `oscar` in the directories of `PATH` only, never in the current
  directory, and runs it by its absolute path, with no shell;
- runs `oscar --version` once and requires `oscar-cli <version>`;
- runs `oscar auth token --json --base-url <base URL>` (and `--profile`),
  with a timeout of 30 seconds;
- refuses a token for another base URL;
- keeps the token in memory until 60 seconds before it expires. After a 401,
  it asks the CLI once more before it raises the error;
- never logs the token and never writes it to disk.

## Configuration

| Option | Default | What it does |
|---|---|---|
| `api_key` | `OSCAR_API_KEY` | The API key |
| `token_provider` | none | A callback that returns an access token |
| `credential` | none | `'cli'`: use the login of the `oscar` CLI |
| `profile` | `OSCAR_PROFILE` | The profile of the CLI login |
| `base_url` | `OSCAR_BASE_URL`, else `DEFAULT_BASE_URL` (`https://oscaraccountant.com`) | The origin of the API |
| `timeout` | `60.0` | The timeout of one attempt, in seconds |
| `max_retries` | `2` | The retries after the first attempt. `0` turns them off |
| `default_headers` | none | Headers for every call |
| `http_client` | a new `httpx.Client` | Your own HTTP client (`httpx.AsyncClient` for `AsyncOscar`) |

The base URL must use `https`. `http` is allowed only for `localhost`,
`127.0.0.1`, `[::1]` and hosts that end in `.personal`.

Each method takes `options=RequestOptions(...)` for one call:

```python
from oscaraccountant import RequestOptions

oscar.invoices.record(
    company_id,
    body=payload,
    options=RequestOptions(
        idempotency_key='my-key',
        timeout=10.0,
        max_retries=0,
        extra_headers={'X-Trace': 'abc'},
    ),
)
```

`extra_headers` cannot replace `Authorization` or `Idempotency-Key`.

## Models and enums

The models and the enums are in `oscaraccountant.types`:

```python
from oscaraccountant.types import BooksInvoice, BooksInvoiceStateEnum
```

A model keeps a field that this version of the SDK does not know. An enum
accepts a value that this version does not know: the value becomes a member
named `_unknown`, so a new value of the server does not break your code.
Money is a decimal string, never a float.

## Pagination

A list method returns a paginator:

```python
invoices = oscar.invoices.list(company_id, limit=50)

page = invoices.first_page()  # one page: page.items, page.next_cursor
for invoice in invoices:  # every item of every page
    ...
for page in invoices.pages():  # every page
    ...
```

With `AsyncOscar`: `await invoices.first_page()`, `async for invoice in
invoices`, and `async for page in invoices.pages()`.

The paginator sends a request only when you need the next item. Each page is
a separate `GET` with its own retries.

## Errors

Every error derives from `OscarError`.

| Error | When |
|---|---|
| `ApiError` | The API answered with a status of 400 or above |
| `BadRequestError` | 400 |
| `AuthenticationError` | 401, or no credential (then `status` is `None`) |
| `PermissionDeniedError` | 403 |
| `NotFoundError` | 404 |
| `ConflictError` | 409 |
| `UnprocessableEntityError` | 422 |
| `RateLimitError` | 429 |
| `InternalServerError` | 500 and above |
| `ConnectionError` | No response: DNS, a refused or reset connection, TLS |
| `TimeoutError` | An attempt ran out of time |
| `ResponseValidationError` | A success response that does not match its type |
| `ConfigurationError` | An option that the client cannot use |

`ConnectionError` and `TimeoutError` are the classes of `oscaraccountant`, not
the builtins of the same name.

An `ApiError` has `status`, `code` (stable and machine-readable, for example
`validation_error`), `title`, `detail`, `errors` (one entry per invalid
field) and `request_id` (the `X-Request-Id` of the response):

```python
from oscaraccountant import ApiError, NotFoundError

try:
    oscar.invoices.retrieve(company_id, invoice_id)
except NotFoundError:
    ...
except ApiError as error:
    print(error.status, error.code, error.detail, error.request_id)
```

## Retries and idempotency

The client sends a request again only when this is safe: a `GET`, or a
request with an `Idempotency-Key`. It retries on a connection error, a
timeout, and the statuses 408, 429, 500, 502, 503, 504, and 409 with the
code `idempotency_in_progress`.

- The delay before retry `n` is `min(8, 0.5 * 2^(n-1))` seconds, less up to
  25 percent of random jitter.
- When the response has `Retry-After`, that delay wins. A `Retry-After` of
  more than 60 seconds ends the retries.
- The client sends an `Idempotency-Key` (a UUID version 4) on every operation
  that accepts it, and the same key on every retry. Give your own key with
  `RequestOptions(idempotency_key=...)`.

## Files

Upload a file with `files.upload`. The file is bytes, a binary file, or a
tuple `(name, content)` or `(name, content, content type)`:

```python
with open('invoice.pdf', 'rb') as handle:
    stored = oscar.files.upload(company_id, file=handle, kind='purchase_invoice')

stored = oscar.files.upload(company_id, file=('invoice.pdf', data, 'application/pdf'))
```

The SDK reads the file once, so a retry sends the same bytes.

Download the bytes of a file with `files.download`:

```python
download = oscar.files.download(company_id, file_id)
print(download.filename, download.content_type, download.content_length)
with open(download.filename or 'file', 'wb') as out:
    for chunk in download.iter_bytes():
        out.write(chunk)
```

`download.read()` gives all the bytes. `AsyncOscar` gives `aiter_bytes()` and
`await aread()`.

## Tests of your code

The client takes these options, so your tests can control it:

| Option | Replaces |
|---|---|
| `http_client` | The HTTP layer, for example `httpx.Client(transport=httpx.MockTransport(handler))` |
| `clock` | The current time, a UNIX time in seconds (`Retry-After` dates, token expiry) |
| `sleep` | The wait before a retry (async for `AsyncOscar`) |
| `jitter` | The backoff jitter, a number in `[0, 1)` |
| `uuid_factory` | The source of `Idempotency-Key` |

```python
import httpx
from oscaraccountant import Oscar


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={...})


oscar = Oscar(
    api_key='osk_test',
    http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    sleep=lambda seconds: None,
)
```

## Development

The resources and the models in `src/oscaraccountant/generated/` are generated
from the OpenAPI schema of the API. Do not edit them. The hand-written
runtime is the rest of `src/oscaraccountant/`.

The gates:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov      # coverage gate: 90 percent
uv build
```

`uv run pytest` also has two suites that need files of the source repository
of Oscar. In this repository they are skipped:

| Suite | What it does |
|---|---|
| `tests/test_conformance.py` | Plays every conformance case of the shared client rules with the sync and the async client, with `respx` as the HTTP mock. The runner is `tests/conformance.py` |
| `tests/integration/` (marker `integration`) | Starts the mock server of the API as a process on a free port and calls every operation over real HTTP: pagination, errors, retries, timeouts, idempotency, upload, download, and the login of the CLI with a fake `oscar` on `PATH` |

## Documentation

The guides and the reference of the API are at
<https://oscaraccountant.com/docs>.

## License

MIT. Copyright (c) 2026 OscarAI OÜ.
