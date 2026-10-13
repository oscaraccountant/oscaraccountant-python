"""When a request is sent again, and after how long (clients/spec/README.md, section 3.4)."""

import datetime as dt
import email.utils
from collections.abc import Mapping

#: The statuses that are retried. A 409 is retried only with the code `idempotency_in_progress`.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

#: A `Retry-After` above this ends the retries.
MAX_RETRY_AFTER = 60.0

#: The first delay, and the longest one, before the jitter.
BASE_DELAY = 0.5
MAX_DELAY = 8.0


def is_retryable_status(status: int, code: str | None) -> bool:
    """A status that is worth a retry of a request that may be retried."""
    return status in RETRY_STATUSES or (status == 409 and code == 'idempotency_in_progress')


def backoff(retry: int, jitter: float) -> float:
    """The delay before retry `retry` (1, 2, ...), with `jitter` in `[0, 1)`."""
    return min(MAX_DELAY, BASE_DELAY * 2.0 ** (retry - 1)) * (1 - 0.25 * jitter)


def retry_after(headers: Mapping[str, str] | None, now: float) -> float | None:
    """The delay that `Retry-After` asks for, in seconds, or `None` when there is no usable header.

    Whole seconds, or an HTTP date against `now` (a UNIX time). A date in the past is 0.
    """
    if headers is None:
        return None
    value = (headers.get('retry-after') or '').strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, when.timestamp() - now)


def retry_delay(retry: int, headers: Mapping[str, str] | None, *, now: float, jitter: float) -> float | None:
    """The delay before retry `retry`, or `None` when `Retry-After` asks for more than 60 seconds."""
    asked = retry_after(headers, now)
    if asked is None:
        return backoff(retry, jitter)
    if asked > MAX_RETRY_AFTER:
        return None
    return asked
