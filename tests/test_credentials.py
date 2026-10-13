import pytest

from oscaraccountant import AuthenticationError, ConfigurationError
from oscaraccountant._credentials import (
    CliCredential,
    Credential,
    MissingCredential,
    ProviderCredential,
    StaticCredential,
    resolve_credential,
)

from .conftest import BASE


def resolve(environ=None, **options):
    values = {'api_key': None, 'token_provider': None, 'credential': None, 'profile': None}
    values.update(options)
    return resolve_credential(base_url=BASE, environ=environ or {}, clock=lambda: 0.0, **values)


def test_explicit_api_key_wins():
    source = resolve({'OSCAR_API_KEY': 'env', 'OSCAR_USE_CLI_LOGIN': '1'}, api_key='explicit', credential='cli')
    assert isinstance(source, StaticCredential)
    assert source.token() == 'explicit'
    assert not source.refreshable


def test_token_provider_wins_over_the_environment():
    source = resolve({'OSCAR_API_KEY': 'env'}, token_provider=lambda: 'provided')
    assert isinstance(source, ProviderCredential)
    assert source.token() == 'provided'


def test_environment_key_wins_over_the_cli():
    source = resolve({'OSCAR_API_KEY': 'env'}, credential='cli')
    assert source.token() == 'env'


@pytest.mark.parametrize(
    ('environ', 'options'),
    [({}, {'credential': 'cli'}), ({'OSCAR_USE_CLI_LOGIN': '1'}, {}), ({'OSCAR_API_KEY': ''}, {'credential': 'cli'})],
)
def test_cli_only_when_asked(environ, options):
    assert isinstance(resolve(environ, **options), CliCredential)


def test_cli_profile_from_option_or_environment():
    source = resolve({'OSCAR_PROFILE': 'env'}, credential='cli')
    assert isinstance(source, CliCredential)
    assert source._login._profile == 'env'
    source = resolve({'OSCAR_PROFILE': 'env'}, credential='cli', profile='mine')
    assert source._login._profile == 'mine'
    assert resolve({'OSCAR_PROFILE': ''}, credential='cli')._login._profile is None


@pytest.mark.parametrize('environ', [{}, {'OSCAR_USE_CLI_LOGIN': '0'}, {'OSCAR_USE_CLI_LOGIN': 'true'}])
def test_missing(environ):
    source = resolve(environ, api_key='')
    assert isinstance(source, MissingCredential)
    with pytest.raises(AuthenticationError, match='OSCAR_API_KEY') as info:
        source.token()
    assert info.value.status is None


def test_both_explicit_credentials_refused():
    with pytest.raises(ConfigurationError, match='not both'):
        resolve(api_key='k', token_provider=lambda: 't')


def test_unknown_credential_option_refused():
    with pytest.raises(ConfigurationError, match="'cli' or None"):
        resolve(credential='keychain')


def test_base_credential():
    with pytest.raises(NotImplementedError):
        Credential().token()
    Credential().invalidate()


@pytest.mark.anyio
async def test_static_atoken():
    assert await StaticCredential('k').atoken() == 'k'


def test_provider_errors():
    def fails():
        raise RuntimeError('vault down')

    with pytest.raises(AuthenticationError, match='The token provider failed: vault down'):
        ProviderCredential(fails).token()
    with pytest.raises(AuthenticationError, match='did not return a token'):
        ProviderCredential(lambda: '').token()
    with pytest.raises(AuthenticationError, match='did not return a token'):
        ProviderCredential(lambda: None).token()


def test_sync_provider_must_not_be_async():
    async def provider():
        return 't'

    with pytest.raises(AuthenticationError, match='must not be async'):
        ProviderCredential(provider).token()


def test_sync_provider_awaitable_that_is_not_a_coroutine():
    class Awaitable:
        def __await__(self):
            yield from ()
            return 't'

    with pytest.raises(AuthenticationError, match='must not be async'):
        ProviderCredential(Awaitable).token()


@pytest.mark.anyio
async def test_async_provider():
    calls = []

    async def provider():
        calls.append(1)
        return 'async-token'

    source = ProviderCredential(provider)
    assert await source.atoken() == 'async-token'
    assert await source.atoken() == 'async-token'
    assert len(calls) == 2
    assert await ProviderCredential(lambda: 'sync').atoken() == 'sync'


@pytest.mark.anyio
async def test_async_provider_failure():
    async def provider():
        raise RuntimeError('boom')

    with pytest.raises(AuthenticationError, match='failed: boom'):
        await ProviderCredential(provider).atoken()


@pytest.mark.anyio
async def test_cli_credential(fake_cli):
    fake_cli.answer('cli-token')
    source = resolve(credential='cli')
    assert source.refreshable
    assert await source.atoken() == 'cli-token'
    assert source.token() == 'cli-token'
    source.invalidate()
    assert source.token() == 'cli-token'
    assert len(fake_cli.calls()) == 3
