"""Every conformance case of clients/spec/conformance, played with the sync and the async client."""

import copy

import pytest

from .conformance import case_params, json_mismatch, play_async, play_sync

CASES = case_params()

pytestmark = pytest.mark.skipif(not CASES, reason='no conformance cases: outside the source repository')


@pytest.mark.parametrize('case', CASES)
def test_sync(case, monkeypatch):
    play_sync(case, monkeypatch)


@pytest.mark.anyio
@pytest.mark.parametrize('case', CASES)
async def test_async(case, monkeypatch):
    await play_async(case, monkeypatch)


# The runner itself: a broken case must fail, or a passing run proves nothing.


def _case(case_id):
    for param in CASES:
        if param.id == case_id:
            return copy.deepcopy(param.values[0])
    raise LookupError(case_id)


def _broken(case_id, change):
    case = _case(case_id)
    change(case)
    return case


def _set(path, value):
    def change(case):
        target = case
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return change


BROKEN = [
    ('operations.invoices.list', _set(['exchanges', 0, 'request', 'path'], '/api/companies/cmp0000001/invoice/')),
    ('operations.invoices.list', _set(['exchanges', 0, 'request', 'query'], [['limit', '50']])),
    ('operations.invoices.list', _set(['exchanges', 0, 'request', 'method'], 'POST')),
    ('operations.invoices.list', _set(['origin'], 'https://other.example.test')),
    ('operations.me.retrieve', _set(['exchanges', 0, 'request', 'headers', 'authorization'], 'Bearer nope')),
    ('operations.me.retrieve', _set(['exchanges', 0, 'request', 'headers', 'authorization'], None)),
    ('operations.me.retrieve', _set(['exchanges', 0, 'request', 'headers', 'accept'], {'pattern': '^text/'})),
    ('operations.me.retrieve', _set(['expect', 'result', 'value', 'has_totp'], True)),
    ('operations.me.retrieve', _set(['expect', 'result', 'value', 'missing'], 1)),
    ('operations.me.retrieve', _set(['expect', 'result', 'kind'], 'none')),
    ('operations.me.retrieve', _set(['sleeps'], [0.5])),
    ('operations.me.retrieve', lambda case: case['exchanges'].append(copy.deepcopy(case['exchanges'][0]))),
    ('retries.get-503-then-ok', lambda case: case['exchanges'].pop()),
    ('retries.get-503-then-ok', _set(['sleeps'], [0.6])),
    ('operations.invoices.cancel', _set(['exchanges', 0, 'request', 'body', 'value'], {'reason': 'other'})),
    ('operations.invoices.cancel', _set(['exchanges', 0, 'request', 'body'], {'kind': 'none'})),
    ('operations.invoices.cancel', _set(['inject', 'uuids'], [])),
    ('operations.files.upload', _set(['exchanges', 0, 'request', 'body', 'parts', 0, 'sha256'], '0' * 64)),
    ('operations.files.upload', lambda case: case['exchanges'][0]['request']['body']['parts'].pop()),
    ('operations.files.download', _set(['expect', 'result', 'size'], 1)),
    ('errors.status-404', _set(['expect', 'error', 'type'], 'ConflictError')),
    ('errors.status-404', _set(['expect', 'error', 'code'], 'other')),
    ('errors.status-404', _set(['expect', 'error', 'message'], 'other')),
    ('errors.status-404', _set(['expect'], {'result': {'kind': 'none'}})),
    ('operations.me.retrieve', _set(['expect'], {'error': {'type': 'NotFoundError'}})),
    ('timeouts.call-option', _set(['exchanges', 0, 'request', 'timeout_ms'], 1000)),
]


@pytest.mark.parametrize(('case_id', 'change'), BROKEN)
def test_broken_case_fails(case_id, change, monkeypatch):
    with pytest.raises(AssertionError):
        play_sync(_broken(case_id, change), monkeypatch)


@pytest.mark.anyio
@pytest.mark.parametrize(('case_id', 'change'), BROKEN[:6])
async def test_broken_case_fails_async(case_id, change, monkeypatch):
    with pytest.raises(AssertionError):
        await play_async(_broken(case_id, change), monkeypatch)


def test_json_mismatch_rules():
    assert json_mismatch(1, 1.0) is None
    assert json_mismatch(True, 1) is not None
    assert json_mismatch(1, True) is not None
    assert json_mismatch({'a': None}, {}, subset=True) is None
    assert json_mismatch({'a': None}, {}) is not None
    assert json_mismatch({'a': 1}, {'a': 1, 'b': 2}, subset=True) is None
    assert json_mismatch({'a': 1}, {'a': 1, 'b': 2}) is not None
    assert json_mismatch([1], [1, 2]) is not None
    assert json_mismatch({'a': 1}, [1]) is not None
    assert json_mismatch('1', 1) is not None
