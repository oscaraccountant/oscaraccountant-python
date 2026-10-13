import json

import httpx
import pytest

from oscaraccountant import _errors
from oscaraccountant._errors import (
    ApiError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    OscarError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
    api_error,
    error_class,
)


@pytest.mark.parametrize(
    ('status', 'kind'),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, UnprocessableEntityError),
        (429, RateLimitError),
        (500, InternalServerError),
        (503, InternalServerError),
        (418, ApiError),
    ],
)
def test_error_class(status, kind):
    assert error_class(status) is kind


def _problem(**fields):
    return json.dumps({'type': 'about:blank', 'title': 'Invalid', 'status': 400, **fields}).encode()


def test_problem_is_parsed():
    body = _problem(code='validation_error', detail='The number is taken.', errors=[{'field': 'n'}, 'junk'])
    error = api_error(400, httpx.Headers({'X-Request-Id': 'req-1'}), body, 'Bad Request')
    assert isinstance(error, BadRequestError)
    assert isinstance(error, OscarError)
    assert error.status == 400
    assert error.code == 'validation_error'
    assert error.title == 'Invalid'
    assert error.detail == 'The number is taken.'
    assert error.errors == [{'field': 'n'}]
    assert error.request_id == 'req-1'
    assert str(error) == '400 validation_error: The number is taken. (request req-1)'


def test_problem_without_detail_uses_title_and_no_request_id():
    error = api_error(404, httpx.Headers(), _problem(code='not_found', errors='nope'), 'Not Found')
    assert str(error) == '404 not_found: Invalid'
    assert error.request_id is None
    assert error.errors == []


def test_problem_with_no_text():
    body = json.dumps({'code': 'gone', 'title': 5}).encode()
    error = api_error(410, httpx.Headers(), body, 'Gone')
    assert type(error) is ApiError
    assert error.title is None
    assert str(error) == '410 gone'


@pytest.mark.parametrize('body', [b'<html>oops</html>', b'[1, 2]', b'{"title": "no code"}'])
def test_body_that_is_not_a_problem(body):
    error = api_error(502, httpx.Headers(), body, 'Bad Gateway')
    assert isinstance(error, InternalServerError)
    assert error.code == 'unknown'
    assert error.title == 'Bad Gateway'
    assert error.detail == body.decode()
    assert str(error) == f'502 unknown: {body.decode()}'


def test_long_body_is_cut():
    error = api_error(500, httpx.Headers(), b'x' * 5000, 'Internal Server Error')
    assert error.detail == 'x' * _errors.MAX_DETAIL


def test_empty_body():
    error = api_error(503, httpx.Headers(), b'', '')
    assert error.detail is None
    assert error.title is None
    assert str(error) == '503 unknown'


def test_no_credential():
    error = AuthenticationError.no_credential('log in')
    assert error.status is None
    assert error.code == 'no_credential'
    assert str(error) == 'log in'


def test_connection_errors_are_not_the_builtins():
    assert not issubclass(_errors.ConnectionError, ConnectionError)
    assert not issubclass(_errors.TimeoutError, TimeoutError)
    assert issubclass(_errors.ConnectionError, OscarError)
    assert issubclass(_errors.TimeoutError, OscarError)
