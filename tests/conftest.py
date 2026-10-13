"""The shared fixtures: a clean environment, the injected hooks, and a fake `oscar` CLI on PATH."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
import respx

from oscaraccountant import _cli_login

BASE = 'https://api.example.test'
KEY = 'osk_test_key'

FAKE_OSCAR = """#!{python}
import json, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
config = json.loads((here / 'config.json').read_text())
with (here / 'calls.log').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:] == ['--version']:
    print(config.get('version', 'oscar-cli 0.1.0'))
    sys.exit(config.get('version_exit', 0))
sys.stderr.write(config.get('stderr', ''))
sys.stdout.write(config.get('stdout', ''))
sys.exit(config.get('exit', 0))
"""


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ('OSCAR_API_KEY', 'OSCAR_BASE_URL', 'OSCAR_PROFILE', 'OSCAR_USE_CLI_LOGIN'):
        monkeypatch.delenv(name, raising=False)
    _cli_login._checked.clear()
    yield
    _cli_login._checked.clear()


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


class Hooks:
    """The injected clock, sleep, jitter and UUID source."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []
        self.jitter_value = 0.0
        self.uuids = iter(f'00000000-0000-4000-8000-{n:012d}' for n in range(1, 1000))

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def asleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def jitter(self) -> float:
        return self.jitter_value

    def uuid(self) -> str:
        return next(self.uuids)

    def sync_options(self) -> dict[str, object]:
        return {'clock': self.clock, 'sleep': self.sleep, 'jitter': self.jitter, 'uuid_factory': self.uuid}

    def async_options(self) -> dict[str, object]:
        return {'clock': self.clock, 'sleep': self.asleep, 'jitter': self.jitter, 'uuid_factory': self.uuid}


@pytest.fixture
def hooks():
    return Hooks()


class FakeCli:
    """A fake `oscar` on PATH, run as a real process. `config.json` sets its answers; `calls.log` records."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / 'oscar'
        self.path.write_text(FAKE_OSCAR.format(python=sys.executable), encoding='utf-8')
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.configure()

    def configure(self, **config: object) -> None:
        (self.directory / 'config.json').write_text(json.dumps(config), encoding='utf-8')

    def answer(self, token: str = 'cli-token', *, expires_at: str | None = None, base_url: str = BASE, **extra):
        body = {'token': token, 'expires_at': expires_at, 'kind': 'oauth', 'base_url': base_url, 'profile': 'default'}
        body.update(extra)
        self.configure(stdout=json.dumps(body))

    def calls(self) -> list[list[str]]:
        log = self.directory / 'calls.log'
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    if os.name == 'nt':
        pytest.skip('the fake CLI is a POSIX script')
    directory = tmp_path / 'bin'
    directory.mkdir()
    cli = FakeCli(directory)
    monkeypatch.setenv('PATH', str(directory))
    return cli
