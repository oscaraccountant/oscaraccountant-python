"""The Python SDK of the Oscar accounting API.

```python
from oscaraccountant import Oscar

oscar = Oscar()  # OSCAR_API_KEY, or api_key=..., or credential='cli'
for invoice in oscar.invoices.list(company_id, state='issued'):
    print(invoice.number)
```
"""

from ._async_client import AsyncOscar
from ._client import Oscar
from ._constants import DEFAULT_BASE_URL, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from ._errors import (
    ApiError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    ConnectionError,
    InternalServerError,
    NotFoundError,
    OscarError,
    PermissionDeniedError,
    RateLimitError,
    ResponseValidationError,
    TimeoutError,
    UnprocessableEntityError,
    UsageError,
)
from ._files import AsyncDownload, Download
from ._pagination import AsyncPage, SyncPage
from ._version import __version__
from .generated import FileInput, OpenStrEnum, OscarModel, RequestOptions

__all__ = [
    'DEFAULT_BASE_URL',
    'DEFAULT_MAX_RETRIES',
    'DEFAULT_TIMEOUT',
    'ApiError',
    'AsyncDownload',
    'AsyncOscar',
    'AsyncPage',
    'AuthenticationError',
    'BadRequestError',
    'ConfigurationError',
    'ConflictError',
    'ConnectionError',
    'Download',
    'FileInput',
    'InternalServerError',
    'NotFoundError',
    'OpenStrEnum',
    'Oscar',
    'OscarError',
    'OscarModel',
    'PermissionDeniedError',
    'RateLimitError',
    'RequestOptions',
    'ResponseValidationError',
    'SyncPage',
    'TimeoutError',
    'UnprocessableEntityError',
    'UsageError',
    '__version__',
]
