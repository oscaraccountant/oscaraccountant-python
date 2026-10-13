import pytest

from oscaraccountant import DEFAULT_BASE_URL, ConfigurationError
from oscaraccountant._base_url import normalize_base_url, resolve_base_url


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('https://API.Example.com/', 'https://api.example.com'),
        ('HTTPS://api.example.com/prefix/', 'https://api.example.com/prefix'),
        ('https://api.example.com:8443', 'https://api.example.com:8443'),
        ('http://localhost:3500', 'http://localhost:3500'),
        ('http://127.0.0.1:3500/', 'http://127.0.0.1:3500'),
        ('http://[::1]:3500', 'http://[::1]:3500'),
        ('http://omarchy.personal:3500', 'http://omarchy.personal:3500'),
        ('  https://api.example.com  ', 'https://api.example.com'),
    ],
)
def test_normalize(value, expected):
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize(
    'value',
    [
        'http://api.example.com',
        'ftp://api.example.com',
        'api.example.com',
        'https://',
        'https://user:pass@api.example.com',
        'https://api.example.com/?a=1',
        'https://api.example.com/#x',
        'https://api.example.com:99999',
        'http://[::1',
    ],
)
def test_refused(value):
    with pytest.raises(ConfigurationError):
        normalize_base_url(value)


def test_resolve_order():
    assert resolve_base_url('https://a.example', {'OSCAR_BASE_URL': 'https://b.example'}) == 'https://a.example'
    assert resolve_base_url(None, {'OSCAR_BASE_URL': 'https://b.example/'}) == 'https://b.example'
    assert resolve_base_url('', {'OSCAR_BASE_URL': ''}) == DEFAULT_BASE_URL
    assert resolve_base_url(None, {}) == DEFAULT_BASE_URL
