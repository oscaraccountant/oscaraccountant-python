"""The runner of the conformance cases of clients/spec/conformance, with `respx` as the HTTP mock.

The format and the matching rules are normative in clients/spec/conformance/README.md. A case is one
call of the client, the HTTP exchanges the call must make, and the result or the error it must give.
The runner plays each case with the sync client (`Oscar`) and with the async client (`AsyncOscar`).
"""

import base64
import contextlib
import datetime as dt
import email
import email.policy
import hashlib
import inspect
import json
import os
import re
import typing
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import respx
from pydantic import BaseModel

from oscaraccountant import AsyncOscar, Oscar, OscarError
from oscaraccountant.generated import JsonBody, OscarModel, Request, RequestOptions

CASES_DIR = Path(__file__).resolve().parents[2] / 'spec' / 'conformance' / 'cases'

#: The name of this runner in the `skip` field of a case.
RUNNER = 'python'

DEFAULT_NOW = '2026-09-27T10:00:00Z'
SLEEP_TOLERANCE = 1e-6

Json = Any


class CaseMismatchError(AssertionError):
    """The client broke the case: a request that does not match, one request too many, an empty source."""


def load_cases(directory: Path = CASES_DIR) -> list[dict[str, Json]]:
    """Every case of every file, in the order of the file names and then of each file."""
    cases: list[dict[str, Json]] = []
    for path in sorted(directory.glob('*.json')):
        cases.extend(json.loads(path.read_text(encoding='utf-8'))['cases'])
    return cases


def case_params(directory: Path = CASES_DIR) -> list[Any]:
    """The cases as pytest parameters, with the case id and the skip of this runner."""
    if not directory.is_dir():
        return []
    params = []
    for case in load_cases(directory):
        marks = []
        reason = case.get('skip', {}).get(RUNNER)
        if reason:
            marks.append(pytest.mark.skip(reason=reason))
        params.append(pytest.param(case, id=case['id'], marks=marks))
    return params


# ---------------------------------------------------------------------------
# Injection: the clock, the sleep, the jitter, the UUID source, the token provider
# ---------------------------------------------------------------------------


def parse_time(value: str) -> float:
    """An RFC 3339 time as a UNIX time."""
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class Injected:
    """The hooks of a case. The sleep does not wait: it records its argument and moves the clock on."""

    def __init__(self, inject: dict[str, Json]) -> None:
        self.now = parse_time(inject.get('now', DEFAULT_NOW))
        self.jitter_value = float(inject.get('jitter', 0))
        self.uuids = list(inject.get('uuids', []))
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)

    def jitter(self) -> float:
        return self.jitter_value

    def uuid(self) -> str:
        if not self.uuids:
            raise CaseMismatchError('The client asked the UUID source for one key too many.')
        value: str = self.uuids.pop(0)
        return value


def token_provider(values: list[str]) -> Callable[[], str]:
    """A provider that returns the next value at each call, and fails when no value is left."""
    remaining = list(values)

    def provide() -> str:
        if not remaining:
            raise RuntimeError('the token provider has no token left')
        return remaining.pop(0)

    return provide


def client_options(case: dict[str, Json], injected: Injected, *, is_async: bool) -> dict[str, Any]:
    """The keyword arguments of the client of a case. An absent option is not passed."""
    client = case.get('client', {})
    options: dict[str, Any] = {
        'clock': injected.clock,
        'sleep': injected.asleep if is_async else injected.sleep,
        'jitter': injected.jitter,
        'uuid_factory': injected.uuid,
    }
    for name in ('base_url', 'api_key', 'max_retries'):
        if name in client:
            options[name] = client[name]
    if 'token_provider' in client:
        options['token_provider'] = token_provider(client['token_provider'])
    if 'timeout_ms' in client:
        options['timeout'] = client['timeout_ms'] / 1000
    return options


# ---------------------------------------------------------------------------
# The mock: each request is checked against the next exchange
# ---------------------------------------------------------------------------


def _object_mismatch(expected: dict[str, Json], actual: Json, where: str, *, subset: bool) -> str | None:
    if not isinstance(actual, dict):
        return f'{where}: expected an object, got {actual!r}'
    if not subset and set(expected) != set(actual):
        return f'{where}: expected the keys {sorted(expected)}, got {sorted(actual)}'
    for key, value in expected.items():
        if key not in actual:
            if subset and value is None:
                continue
            return f'{where}.{key}: missing'
        found = json_mismatch(value, actual[key], f'{where}.{key}', subset=subset)
        if found:
            return found
    return None


def _array_mismatch(expected: list[Json], actual: Json, where: str, *, subset: bool) -> str | None:
    if not isinstance(actual, list) or len(actual) != len(expected):
        return f'{where}: expected {expected!r}, got {actual!r}'
    for index, (want, got) in enumerate(zip(expected, actual, strict=True)):
        found = json_mismatch(want, got, f'{where}[{index}]', subset=subset)
        if found:
            return found
    return None


def json_mismatch(expected: Json, actual: Json, where: str = '$', *, subset: bool = False) -> str | None:
    """Where `actual` differs from `expected`, or `None`.

    Numbers compare by value (`1` equals `1.0`), a boolean never equals a number. With `subset`, the actual
    object may have more keys, and an expected null may be absent (the match rule of a result).
    """
    if isinstance(expected, dict):
        return _object_mismatch(expected, actual, where, subset=subset)
    if isinstance(expected, list):
        return _array_mismatch(expected, actual, where, subset=subset)
    if isinstance(expected, bool) or isinstance(actual, bool):
        same = type(expected) is type(actual) and expected == actual
    elif isinstance(expected, int | float):
        same = isinstance(actual, int | float) and expected == actual
    else:
        same = expected == actual and type(expected) is type(actual)
    return None if same else f'{where}: expected {expected!r}, got {actual!r}'


def multipart_parts(request: httpx.Request) -> list[dict[str, Json]]:
    """The parts of a `multipart/form-data` body: name, and the value or the file name, type and hash."""
    content_type = request.headers.get('content-type', '')
    raw = b'Content-Type: ' + content_type.encode('latin-1') + b'\r\n\r\n' + request.content
    message = email.message_from_bytes(raw, policy=email.policy.HTTP)
    if not message.is_multipart():
        raise CaseMismatchError(f'The body is not multipart/form-data: {content_type!r}.')
    parts = []
    for part in message.iter_parts():
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b''
        entry: dict[str, Json] = {'name': part.get_param('name', header='content-disposition')}
        filename = part.get_filename()
        if filename is None:
            entry['value'] = data.decode('utf-8')
        else:
            entry['filename'] = filename
            entry['sha256'] = hashlib.sha256(data).hexdigest()
            entry['content_type'] = part.get_content_type() if part.get('content-type') else None
        parts.append(entry)
    return parts


def body_mismatch(expected: dict[str, Json], request: httpx.Request) -> str | None:
    content = request.content
    kind = expected['kind']
    if kind == 'none':
        return None if not content else f'expected no body, got {content[:200]!r}'
    if kind == 'json':
        try:
            actual = json.loads(content)
        except ValueError:
            return f'expected a JSON body, got {content[:200]!r}'
        return json_mismatch(expected['value'], actual, 'body')
    actual_parts = {part['name']: part for part in multipart_parts(request)}
    wanted = {part['name']: part for part in expected['parts']}
    if set(actual_parts) != set(wanted):
        return f'expected the parts {sorted(wanted)}, got {sorted(actual_parts)}'
    for name, want in wanted.items():
        got = actual_parts[name]
        for key in ('value', 'filename', 'sha256', 'content_type'):
            if key in want and want[key] != got.get(key):
                return f'part {name}: expected {key} {want[key]!r}, got {got.get(key)!r}'
    return None


def header_mismatch(expected: dict[str, Json], headers: httpx.Headers) -> str | None:
    for name, rule in expected.items():
        actual = headers.get(name)
        if rule is None:
            if actual is not None:
                return f'header {name}: expected none, got {actual!r}'
        elif isinstance(rule, dict):
            if actual is None or re.search(rule['pattern'], actual) is None:
                return f'header {name}: expected to match {rule["pattern"]!r}, got {actual!r}'
        elif actual != rule:
            return f'header {name}: expected {rule!r}, got {actual!r}'
    return None


def _url_mismatch(origin: str, expected: dict[str, Json], request: httpx.Request) -> str | None:
    url = request.url
    actual_origin = f'{url.scheme}://{url.netloc.decode("ascii")}'
    if actual_origin != origin:
        return f'origin: expected {origin!r}, got {actual_origin!r}'
    if request.method != expected['method']:
        return f'method: expected {expected["method"]}, got {request.method}'
    path = url.raw_path.decode('ascii').split('?', 1)[0]
    if path != expected['path']:
        return f'path: expected {expected["path"]!r}, got {path!r}'
    query = sorted(parse_qsl(url.query.decode('ascii'), keep_blank_values=True))
    wanted_query = sorted((name, value) for name, value in expected['query'])
    if query != wanted_query:
        return f'query: expected {wanted_query!r}, got {query!r}'
    return None


def _timeout_mismatch(expected: dict[str, Json], request: httpx.Request) -> str | None:
    if 'timeout_ms' not in expected:
        return None
    timeout = request.extensions.get('timeout', {})
    wanted = expected['timeout_ms'] / 1000
    if any(abs((timeout.get(phase) or 0) - wanted) > SLEEP_TOLERANCE for phase in ('connect', 'read', 'write')):
        return f'timeout: expected {wanted} s, got {timeout!r}'
    return None


def request_mismatch(origin: str, expected: dict[str, Json], request: httpx.Request) -> str | None:
    """How a request differs from the `request` of an exchange, or `None`."""
    return (
        _url_mismatch(origin, expected, request)
        or header_mismatch(expected.get('headers', {}), request.headers)
        or body_mismatch(expected['body'], request)
        or _timeout_mismatch(expected, request)
    )


def build_response(answer: dict[str, Json]) -> httpx.Response:
    """The answer of an exchange, with the content type the conformance README sets when it has none."""
    status = answer['status']
    headers = dict(answer.get('headers', {}))
    body = answer.get('body', {'kind': 'empty'})
    kind = body['kind']
    content = b''
    default_type = None
    if kind == 'json':
        content = json.dumps(body['value']).encode()
        default_type = 'application/problem+json' if status >= 400 else 'application/json'
    elif kind == 'text':
        content = body['value'].encode()
        default_type = 'text/plain; charset=utf-8'
    elif kind == 'base64':
        content = base64.b64decode(body['value'])
        default_type = 'application/octet-stream'
    if default_type is not None and 'content-type' not in headers:
        headers['content-type'] = default_type
    return httpx.Response(status, headers=headers, content=content)


@dataclass
class Player:
    """Answers the n-th request with the n-th exchange, after it checks the request."""

    case: dict[str, Json]
    used: int = 0
    failures: list[str] = field(default_factory=list)

    def fail(self, message: str) -> CaseMismatchError:
        self.failures.append(message)
        return CaseMismatchError(message)

    def handle(self, request: httpx.Request) -> httpx.Response:
        exchanges = self.case['exchanges']
        if self.used >= len(exchanges):
            raise self.fail(f'Request {self.used + 1} ({request.method} {request.url}) has no exchange.')
        exchange = exchanges[self.used]
        self.used += 1
        request.read()
        found = request_mismatch(self.case['origin'], exchange['request'], request)
        if found:
            raise self.fail(f'Request {self.used}: {found}')
        fault = exchange.get('fault')
        if fault == 'connection_error':
            raise httpx.ConnectError('conformance: connection error', request=request)
        if fault == 'timeout':
            raise httpx.ReadTimeout('conformance: timeout', request=request)
        return build_response(exchange['response'])


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    """A call ready to run: the function, its arguments, and how to read what it gives."""

    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    #: `json`, `page`, `items`, `none` or `binary`.
    reads: str


def call_options(options: dict[str, Json] | None) -> RequestOptions | None:
    if options is None:
        return None
    timeout_ms = options.get('timeout_ms')
    return RequestOptions(
        idempotency_key=options.get('idempotency_key'),
        timeout=timeout_ms / 1000 if timeout_ms is not None else None,
        max_retries=options.get('max_retries'),
        extra_headers=options.get('headers'),
    )


def _python_name(name: str, parameters: typing.Mapping[str, inspect.Parameter]) -> str:
    """The Python name of a wire name: the same, or with a `_` suffix when it is a keyword or a builtin."""
    if name in parameters:
        return name
    if f'{name}_' in parameters:
        return f'{name}_'
    raise CaseMismatchError(f'The method has no parameter for {name!r}.')


def _body_model(function: Callable[..., Any]) -> type[BaseModel]:
    hint = typing.get_type_hints(function)['body']
    for candidate in (hint, *typing.get_args(hint)):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    raise CaseMismatchError(f'The body of {function.__qualname__} is not a model.')


def _file_input(file: dict[str, Json]) -> Any:
    content = base64.b64decode(file['content_base64'])
    filename = file.get('filename')
    content_type = file.get('content_type')
    if filename is None:
        if content_type is not None:
            raise CaseMismatchError('The Python SDK takes a content type only with a file name.')
        return content
    if content_type is None:
        return (filename, content)
    return (filename, content, content_type)


def spec_invocation(client: Oscar | AsyncOscar, call: dict[str, Json]) -> Invocation:
    """A request spec handed to the runtime directly."""
    spec = call['spec']
    body = spec.get('body')
    request = Request(
        operation_id=spec['operation_id'],
        method=spec['method'],
        path=spec['path'],
        path_params=spec['path_params'],
        query=spec['query'],
        body=None if body is None else JsonBody(OscarModel.model_validate(body)),
        idempotent=spec['idempotent'],
        response=spec['response'],
    )
    kwargs: dict[str, Any] = {'options': call_options(call.get('options'))}
    if spec['response'] == 'json':
        kwargs['response_type'] = OscarModel
        return Invocation(client.request, (request,), kwargs, 'json')
    if spec['response'] == 'binary':
        return Invocation(client.download, (request,), kwargs, 'binary')
    return Invocation(client.request_no_content, (request,), kwargs, 'none')


def invocation(client: Oscar | AsyncOscar, call: dict[str, Json]) -> Invocation:
    """The generated method of an operation call, with its arguments mapped from the wire names."""
    if 'spec' in call:
        return spec_invocation(client, call)
    resource, method = call['operation'].split('.')
    function = getattr(getattr(client, resource), method)
    parameters = inspect.signature(function).parameters
    path = call.get('path', {})
    positional = [
        name for name, parameter in parameters.items() if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    # The path parameters, in the order of the path (the order of the positional parameters).
    args = tuple(path[name] for name in positional if name in path)
    if len(args) != len(path):
        raise CaseMismatchError(f'{call["operation"]} does not take the path parameters {sorted(path)}.')
    kwargs: dict[str, Any] = {}
    for name, value in call.get('query', {}).items():
        kwargs[_python_name(name, parameters)] = value
    if 'body' in call:
        kwargs['body'] = _body_model(function).model_validate(call['body'])
    if 'multipart' in call:
        multipart = call['multipart']
        for name, value in multipart.get('fields', {}).items():
            kwargs[_python_name(name, parameters)] = value
        file = multipart['file']
        kwargs[_python_name(file['field'], parameters)] = _file_input(file)
    if 'options' in call:
        kwargs['options'] = call_options(call['options'])
    returns = str(inspect.signature(function).return_annotation)
    if 'Paginator' in returns:
        reads = 'items' if call.get('mode') == 'all' else 'page'
    elif 'BinaryResponse' in returns:
        reads = 'binary'
    elif returns == 'None':
        reads = 'none'
    else:
        reads = 'json'
    return Invocation(function, args, kwargs, reads)


def wire(model: BaseModel) -> Json:
    """A model turned back into wire JSON: the wire names and the model's own serialization."""
    return model.model_dump(mode='json', by_alias=True)


def binary_result(content: bytes, download: Any) -> dict[str, Json]:
    return {
        'sha256': hashlib.sha256(content).hexdigest(),
        'size': len(content),
        'content_type': download.content_type,
        'filename': download.filename,
    }


def run_sync(client: Oscar, call: dict[str, Json]) -> tuple[str, Json]:
    """Run the call with the sync client. The result as (kind, wire JSON)."""
    target = invocation(client, call)
    value = target.function(*target.args, **target.kwargs)
    if target.reads == 'page':
        return 'json', wire(value.first_page())
    if target.reads == 'items':
        return 'items', [wire(item) for item in value]
    if target.reads == 'binary':
        return 'binary', binary_result(value.read(), value)
    if target.reads == 'none':
        return 'none', value
    return 'json', wire(value)


async def run_async(client: AsyncOscar, call: dict[str, Json]) -> tuple[str, Json]:
    """Run the call with the async client: `await`, `first_page()`, `async for`, `aread()`."""
    target = invocation(client, call)
    if target.reads == 'page':
        return 'json', wire(await target.function(*target.args, **target.kwargs).first_page())
    if target.reads == 'items':
        return 'items', [wire(item) async for item in target.function(*target.args, **target.kwargs)]
    value = await target.function(*target.args, **target.kwargs)
    if target.reads == 'binary':
        return 'binary', binary_result(await value.aread(), value)
    if target.reads == 'none':
        return 'none', value
    return 'json', wire(value)


# ---------------------------------------------------------------------------
# The outcome
# ---------------------------------------------------------------------------

ERROR_FIELDS = ('status', 'code', 'title', 'detail', 'errors', 'request_id')


def check_error(expected: dict[str, Json], error: BaseException) -> None:
    assert isinstance(error, OscarError), f'expected {expected["type"]}, got {error!r}'
    assert type(error).__name__ == expected['type'], f'expected {expected["type"]}, got {error!r}'
    for name in ERROR_FIELDS:
        if name in expected:
            actual = getattr(error, name, None)
            found = json_mismatch(expected[name], actual, f'error.{name}')
            assert found is None, found
    if 'message' in expected:
        assert str(error) == expected['message']


def check_result(expected: dict[str, Json], kind: str, value: Json) -> None:
    wanted = expected['kind']
    assert kind == wanted, f'expected a {wanted} result, got {kind}'
    if wanted == 'none':
        assert value is None
    elif wanted == 'binary':
        assert value == {name: expected[name] for name in ('sha256', 'size', 'content_type', 'filename')}
    else:
        found = json_mismatch(expected['value'], value, 'result', subset=True)
        assert found is None, found


@dataclass
class Outcome:
    kind: str = ''
    value: Json = None
    error: BaseException | None = None


def check_outcome(case: dict[str, Json], player: Player, injected: Injected, outcome: Outcome) -> None:
    """Every rule of "The expected outcome", after the call."""
    assert not player.failures, player.failures[0]
    if isinstance(outcome.error, CaseMismatchError):
        raise outcome.error
    expect = case['expect']
    if 'error' in expect:
        if outcome.error is None:
            raise AssertionError(f'expected {expect["error"]["type"]}, got a {outcome.kind} result')
        check_error(expect['error'], outcome.error)
    else:
        if outcome.error is not None:
            raise AssertionError(f'expected a result, got {outcome.error!r}') from outcome.error
        check_result(expect['result'], outcome.kind, outcome.value)
    exchanges = case['exchanges']
    assert player.used == len(exchanges), f'{player.used} of {len(exchanges)} exchanges were used'
    wanted_sleeps = case.get('sleeps', [])
    assert len(injected.sleeps) == len(wanted_sleeps), f'sleeps: expected {wanted_sleeps}, got {injected.sleeps}'
    for want, got in zip(wanted_sleeps, injected.sleeps, strict=True):
        assert abs(want - got) <= SLEEP_TOLERANCE, f'sleeps: expected {wanted_sleeps}, got {injected.sleeps}'


def set_environment(monkeypatch: pytest.MonkeyPatch, case: dict[str, Json]) -> None:
    """Unset every `OSCAR_*` variable, then set the ones of the case. `monkeypatch` puts them back."""
    for name in list(os.environ):
        if name.startswith('OSCAR_'):
            monkeypatch.delenv(name)
    for name, value in case.get('env', {}).items():
        monkeypatch.setenv(name, value)


@contextlib.contextmanager
def mocked(player: Player) -> Iterator[respx.MockRouter]:
    """Every request of the block goes to `player`."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route().mock(side_effect=player.handle)
        yield router


def play_sync(case: dict[str, Json], monkeypatch: pytest.MonkeyPatch) -> None:
    """Play one case with `Oscar`."""
    set_environment(monkeypatch, case)
    injected = Injected(case.get('inject', {}))
    player = Player(case)
    outcome = Outcome()
    with mocked(player):
        try:
            with Oscar(**client_options(case, injected, is_async=False)) as client:
                outcome.kind, outcome.value = run_sync(client, case['call'])
        except Exception as exc:
            outcome.error = exc
    check_outcome(case, player, injected, outcome)


async def play_async(case: dict[str, Json], monkeypatch: pytest.MonkeyPatch) -> None:
    """Play one case with `AsyncOscar`."""
    set_environment(monkeypatch, case)
    injected = Injected(case.get('inject', {}))
    player = Player(case)
    outcome = Outcome()
    with mocked(player):
        try:
            async with AsyncOscar(**client_options(case, injected, is_async=True)) as client:
                outcome.kind, outcome.value = await run_async(client, case['call'])
        except Exception as exc:
            outcome.error = exc
    check_outcome(case, player, injected, outcome)
