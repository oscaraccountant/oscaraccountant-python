"""The credential of a client, in the order of clients/spec/README.md, section 1.1.

1. An explicit `api_key` or `token_provider`.
2. `OSCAR_API_KEY`.
3. The stored login of the CLI, only with `credential='cli'` or `OSCAR_USE_CLI_LOGIN=1`.
4. Otherwise `AuthenticationError`, raised before the first request.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from ._cli_login import CliLogin
from ._constants import ENV_API_KEY, ENV_PROFILE, ENV_USE_CLI_LOGIN
from ._errors import AuthenticationError, ConfigurationError

#: A callback that returns an access token. The async client also accepts one that returns an awaitable.
TokenProvider = Callable[[], str]
AsyncTokenProvider = Callable[[], str | Awaitable[str]]

CredentialOption = Literal['cli']

MISSING_MESSAGE = (
    'No credential for the Oscar API. Pass api_key or token_provider, set OSCAR_API_KEY, '
    "or run `oscar login` and pass credential='cli' (or set OSCAR_USE_CLI_LOGIN=1)."
)


class Credential:
    """The source of the `Authorization: Bearer` value. It is asked before each attempt."""

    #: A source that can fetch a new value after a 401 (the CLI login).
    refreshable = False

    def token(self) -> str:
        raise NotImplementedError

    async def atoken(self) -> str:
        return self.token()

    def invalidate(self) -> None:
        """Forget a cached value. Only a refreshable source has one."""


class StaticCredential(Credential):
    """An API key, given in code or by `OSCAR_API_KEY`."""

    def __init__(self, value: str) -> None:
        self._value = value

    def token(self) -> str:
        return self._value


class MissingCredential(Credential):
    """No credential: every call raises `AuthenticationError` and sends nothing."""

    def token(self) -> str:
        raise AuthenticationError.no_credential(MISSING_MESSAGE)


def _checked(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AuthenticationError.no_credential('The token provider did not return a token.')
    return value


class ProviderCredential(Credential):
    """A token provider, called before each attempt. It may cache the token itself."""

    def __init__(self, provider: AsyncTokenProvider) -> None:
        self._provider = provider

    def _call(self) -> object:
        try:
            return self._provider()
        except Exception as exc:
            raise AuthenticationError.no_credential(f'The token provider failed: {exc}') from exc

    def token(self) -> str:
        value = self._call()
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise AuthenticationError.no_credential('The token provider of the sync client must not be async.')
        return _checked(value)

    async def atoken(self) -> str:
        value = self._call()
        if inspect.isawaitable(value):
            try:
                value = await value
            except Exception as exc:
                raise AuthenticationError.no_credential(f'The token provider failed: {exc}') from exc
        return _checked(value)


class CliCredential(Credential):
    """The stored login of the CLI (step 3)."""

    refreshable = True

    def __init__(self, login: CliLogin) -> None:
        self._login = login

    def token(self) -> str:
        return self._login.token()

    async def atoken(self) -> str:
        # The CLI is a process: run it on a thread, so the event loop keeps running.
        return await asyncio.to_thread(self._login.token)

    def invalidate(self) -> None:
        self._login.invalidate()


def resolve_credential(
    *,
    api_key: str | None,
    token_provider: AsyncTokenProvider | None,
    credential: CredentialOption | None,
    profile: str | None,
    base_url: str,
    environ: Mapping[str, str],
    clock: Callable[[], float],
) -> Credential:
    """The first source of the order that is set. An empty string counts as not set."""
    if api_key and token_provider is not None:
        raise ConfigurationError('Pass api_key or token_provider, not both.')
    if credential not in (None, 'cli'):
        raise ConfigurationError(f"credential must be 'cli' or None, not {credential!r}.")
    if api_key:
        return StaticCredential(api_key)
    if token_provider is not None:
        return ProviderCredential(token_provider)
    env_key = environ.get(ENV_API_KEY)
    if env_key:
        return StaticCredential(env_key)
    if credential == 'cli' or environ.get(ENV_USE_CLI_LOGIN) == '1':
        login = CliLogin(base_url, profile or environ.get(ENV_PROFILE) or None, clock=clock)
        return CliCredential(login)
    return MissingCredential()
