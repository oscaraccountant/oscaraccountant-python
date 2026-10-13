"""The base URL of the API (clients/spec/README.md, section 3.1)."""

from collections.abc import Mapping
from urllib.parse import urlsplit

from ._constants import DEFAULT_BASE_URL, ENV_BASE_URL
from ._errors import ConfigurationError

#: The hosts that may use `http`: the loopback hosts. A host that ends in `.personal` may too.
_LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})
_OVERLAY_SUFFIX = '.personal'


def normalize_base_url(value: str) -> str:
    """The base URL without a trailing `/`, with the scheme and the host in lower case.

    Raises `ConfigurationError` for a URL that is not `https`, or `http` on a loopback or overlay host.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError(f'The base URL {value!r} is not a valid URL.') from exc
    scheme = parts.scheme.lower()
    host = (parts.hostname or '').lower()
    if not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ConfigurationError(f'The base URL {value!r} must be a scheme, a host, a port and a path only.')
    local = host in _LOOPBACK_HOSTS or host.endswith(_OVERLAY_SUFFIX)
    if scheme != 'https' and not (scheme == 'http' and local):
        raise ConfigurationError(
            f'The base URL {value!r} must use https (http only for localhost, 127.0.0.1, [::1] or *.personal).'
        )
    netloc = f'[{host}]' if ':' in host else host
    if port is not None:
        netloc = f'{netloc}:{port}'
    return f'{scheme}://{netloc}{parts.path.rstrip("/")}'


def resolve_base_url(option: str | None, environ: Mapping[str, str]) -> str:
    """The option, else `OSCAR_BASE_URL`, else the default, normalized. An empty string counts as not set."""
    value = option or environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    return normalize_base_url(value)
