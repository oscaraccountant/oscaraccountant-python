import json
import os
import subprocess
import sys

import pytest

from oscaraccountant import AuthenticationError, _cli_login
from oscaraccountant._cli_login import CliLogin, check_version, find_oscar, parse_token

from .conftest import BASE


def _executable(path):
    path.write_text('#!/bin/sh\n', encoding='utf-8')
    path.chmod(0o755)
    return path


def test_find_oscar_uses_absolute_path_entries_only(tmp_path, monkeypatch):
    here = tmp_path / 'cwd'
    here.mkdir()
    _executable(here / 'oscar')
    monkeypatch.chdir(here)
    found = tmp_path / 'bin'
    found.mkdir()
    _executable(found / 'oscar')
    path = os.pathsep.join(['', '.', 'relative', str(tmp_path / 'missing'), str(found)])
    assert find_oscar(path, windows=False) == found / 'oscar'
    assert find_oscar(os.pathsep.join(['', '.']), windows=False) is None
    assert find_oscar(None, windows=False) is None


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows has no execute bit: every file is executable')
def test_find_oscar_needs_an_executable_file_on_posix(tmp_path):
    (tmp_path / 'oscar').write_text('x', encoding='utf-8')
    assert find_oscar(str(tmp_path), windows=False) is None
    (tmp_path / 'dir').mkdir()
    (tmp_path / 'dir' / 'oscar').mkdir()
    assert find_oscar(str(tmp_path / 'dir'), windows=False) is None


def test_find_oscar_on_windows_follows_pathext(tmp_path):
    first = tmp_path / 'a'
    second = tmp_path / 'b'
    first.mkdir()
    second.mkdir()
    (first / 'oscar.cmd').write_text('x', encoding='utf-8')
    (first / 'oscar.bat').write_text('x', encoding='utf-8')
    (second / 'oscar.exe').write_text('x', encoding='utf-8')
    path = f'{first};{second}'
    assert find_oscar(path, windows=True, pathext='.EXE;.CMD', pathsep=';') == first / 'oscar.cmd'
    assert find_oscar(str(first), windows=True, pathext='.BAT', pathsep=';') is None
    assert find_oscar(str(second), windows=True, pathext=None, pathsep=';') == second / 'oscar.exe'
    assert find_oscar(f'{second};{first}', windows=True, pathext='.CMD;.EXE', pathsep=';') == second / 'oscar.exe'


@pytest.mark.parametrize(
    'answer',
    [
        'not json',
        '[]',
        json.dumps({'base_url': BASE}),
        json.dumps({'token': '', 'base_url': BASE}),
        json.dumps({'token': 't'}),
        json.dumps({'token': 't', 'base_url': BASE, 'expires_at': 5}),
        json.dumps({'token': 't', 'base_url': BASE, 'expires_at': 'tomorrow'}),
    ],
)
def test_parse_token_refuses_bad_answers(answer):
    with pytest.raises(AuthenticationError, match='cannot read') as info:
        parse_token(answer)
    assert 'secret' not in str(info.value)


def test_parse_token():
    answer = parse_token(
        json.dumps(
            {
                'token': 'secret',
                'expires_at': '2026-09-27T10:00:00Z',
                'kind': 'oauth',
                'base_url': BASE,
                'profile': 'default',
                'future': 1,
            }
        )
    )
    assert answer.token == 'secret'
    assert answer.expires_at == 1790503200.0
    assert answer.kind == 'oauth'
    assert answer.profile == 'default'


def test_parse_token_naive_date_is_utc_and_optional_fields():
    answer = parse_token(json.dumps({'token': 't', 'base_url': BASE, 'expires_at': '2026-09-27T10:00:00', 'kind': 1}))
    assert answer.expires_at == 1790503200.0
    assert answer.kind is None
    assert answer.profile is None


class Clock:
    def __init__(self) -> None:
        self.now = 1790503200.0 - 3600

    def __call__(self) -> float:
        return self.now


def test_cli_login_runs_the_cli_and_caches(fake_cli):
    fake_cli.answer('cli-token', expires_at='2026-09-27T10:00:00Z')
    clock = Clock()
    login = CliLogin(BASE, None, clock=clock)
    assert login.token() == 'cli-token'
    assert login.token() == 'cli-token'
    assert fake_cli.calls() == [['--version'], ['auth', 'token', '--json', '--base-url', BASE]]
    clock.now = 1790503200.0 - 61
    login.token()
    assert len(fake_cli.calls()) == 2
    clock.now = 1790503200.0 - 60
    login.token()
    assert len(fake_cli.calls()) == 3
    assert fake_cli.calls()[-1][0] == 'auth'


def test_cli_login_with_profile_and_no_expiry(fake_cli):
    fake_cli.answer('osk_key', expires_at=None, kind='api_key')
    login = CliLogin(BASE, 'work', clock=Clock())
    assert login.token() == 'osk_key'
    login.token()
    assert fake_cli.calls() == [
        ['--version'],
        ['auth', 'token', '--json', '--base-url', BASE, '--profile', 'work'],
    ]
    login.invalidate()
    login.token()
    assert len(fake_cli.calls()) == 3


def test_version_is_checked_once_per_process(fake_cli):
    fake_cli.answer()
    CliLogin(BASE, None, clock=Clock()).token()
    CliLogin(BASE, None, clock=Clock()).token()
    assert [call for call in fake_cli.calls() if call == ['--version']] == [['--version']]


@pytest.mark.parametrize(
    'config',
    [
        {'version': 'something-else 1.0.0'},
        {'version': 'oscar-cli 1.0'},
        {'version': 'oscar-cli 1.0.0', 'version_exit': 1},
    ],
)
def test_wrong_version_is_refused(fake_cli, config):
    fake_cli.configure(**config)
    with pytest.raises(AuthenticationError, match='is not the oscar CLI'):
        check_version(fake_cli.path)
    assert str(fake_cli.path) not in _cli_login._checked


def test_prerelease_version_is_accepted(fake_cli):
    fake_cli.configure(version='oscar-cli 0.2.0-beta.1\nmore')
    check_version(fake_cli.path)
    assert str(fake_cli.path) in _cli_login._checked


def test_missing_cli(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(AuthenticationError, match='not installed'):
        CliLogin(BASE, None, clock=Clock()).token()


def test_exit_code_three_says_log_in(fake_cli):
    fake_cli.configure(exit=3, stderr='no login for this base URL\nsecond line\n')
    with pytest.raises(AuthenticationError) as info:
        CliLogin(BASE, None, clock=Clock()).token()
    assert str(info.value) == (
        '`oscar auth token` failed with exit code 3: no login for this base URL. Run `oscar login` first.'
    )


def test_other_exit_code_without_message(fake_cli):
    fake_cli.configure(exit=1)
    with pytest.raises(AuthenticationError, match=r'exit code 1: no message\.$'):
        CliLogin(BASE, None, clock=Clock()).token()


def test_answer_for_another_base_url_is_refused(fake_cli):
    fake_cli.answer('secret', base_url='https://other.example.test')
    with pytest.raises(AuthenticationError) as info:
        CliLogin(BASE, None, clock=Clock()).token()
    assert 'https://other.example.test' in str(info.value)
    assert 'secret' not in str(info.value)


def test_answer_base_url_is_normalized(fake_cli):
    fake_cli.answer('t', base_url=BASE.upper() + '/')
    assert CliLogin(BASE, None, clock=Clock()).token() == 't'


def test_answer_with_an_invalid_base_url(fake_cli):
    fake_cli.answer('t', base_url='ftp://nowhere')
    with pytest.raises(AuthenticationError, match='ftp://nowhere'):
        CliLogin(BASE, None, clock=Clock()).token()


def test_timeout(fake_cli, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='oscar', timeout=30)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(AuthenticationError, match='did not answer within 30 seconds'):
        CliLogin(BASE, None, clock=Clock()).token()


def test_os_error(fake_cli, monkeypatch):
    def run(*args, **kwargs):
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(AuthenticationError, match='could not be run: Permission denied'):
        CliLogin(BASE, None, clock=Clock()).token()


def test_the_cli_is_run_without_a_shell(fake_cli, monkeypatch):
    seen = {}
    real = subprocess.run

    def run(arguments, **kwargs):
        seen.setdefault('calls', []).append((arguments, kwargs))
        return real(arguments, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    fake_cli.answer()
    CliLogin(BASE, None, clock=Clock()).token()
    for arguments, kwargs in seen['calls']:
        assert arguments[0] == str(fake_cli.path)
        assert kwargs['shell'] is False
        assert kwargs['timeout'] == 30.0
        assert 'env' not in kwargs
