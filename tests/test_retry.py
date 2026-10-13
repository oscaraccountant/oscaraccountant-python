import email.utils

import pytest

from oscaraccountant._retry import backoff, is_retryable_status, retry_after, retry_delay

NOW = 1_000_000.0


@pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504])
def test_retryable_statuses(status):
    assert is_retryable_status(status, None)


@pytest.mark.parametrize(('status', 'code'), [(400, None), (401, None), (409, 'conflict'), (501, None), (404, None)])
def test_not_retryable(status, code):
    assert not is_retryable_status(status, code)


def test_idempotency_in_progress_is_retryable():
    assert is_retryable_status(409, 'idempotency_in_progress')


def test_backoff():
    assert [backoff(n, 0.0) for n in range(1, 7)] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    assert backoff(1, 0.5) == 0.5 * 0.875
    assert backoff(3, 0.999) == pytest.approx(2.0 * (1 - 0.25 * 0.999))


def test_retry_after_seconds():
    assert retry_after({'retry-after': '3'}, NOW) == 3.0
    assert retry_after({'retry-after': ' 0 '}, NOW) == 0.0


def test_retry_after_date():
    date = email.utils.formatdate(NOW + 7, usegmt=True)
    assert retry_after({'retry-after': date}, NOW) == pytest.approx(7.0)
    past = email.utils.formatdate(NOW - 100, usegmt=True)
    assert retry_after({'retry-after': past}, NOW) == 0.0


def test_retry_after_date_without_zone_is_utc():
    date = email.utils.formatdate(NOW + 5, usegmt=True).replace('GMT', '-0000')
    assert retry_after({'retry-after': date}, NOW) == pytest.approx(5.0)


@pytest.mark.parametrize('headers', [None, {}, {'retry-after': ''}, {'retry-after': 'soon'}, {'retry-after': '-1'}])
def test_retry_after_unusable(headers):
    assert retry_after(headers, NOW) is None


def test_retry_delay():
    assert retry_delay(2, None, now=NOW, jitter=0.0) == 1.0
    assert retry_delay(2, {'retry-after': '5'}, now=NOW, jitter=0.0) == 5.0
    assert retry_delay(1, {'retry-after': '60'}, now=NOW, jitter=0.0) == 60.0
    assert retry_delay(1, {'retry-after': '61'}, now=NOW, jitter=0.0) is None
