"""The mock server of clients/mock-server, started once per test session as a real process.

The suite runs in the default test run when `uv` and clients/mock-server are there, and is skipped
otherwise (outside the source repository). Run it alone with `uv run pytest -m integration`.
"""

import contextlib
import json
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

MOCK_SERVER_DIR = Path(__file__).resolve().parents[3] / 'mock-server'
READY_PREFIX = 'OSCAR_MOCK_SERVER_READY '
#: The first start may create the environment of the mock server.
START_TIMEOUT = 180.0
#: The variables that point uv at the environment of this SDK; the mock server has its own.
PROJECT_VARIABLES = frozenset({'VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT'})


@dataclass(frozen=True)
class MockServer:
    """A running mock server: its ready information and its control API."""

    url: str
    api_key: str
    company_id: str
    organization_id: str
    resource: str

    def control(self, method: str, path: str, body: object = None) -> Any:
        response = httpx.request(method, f'{self.url}/_mock/{path}', json=body, timeout=10)
        response.raise_for_status()
        return response.json() if response.content else None

    def requests(self) -> list[dict[str, Any]]:
        """The request log: every request except the control API."""
        entries: list[dict[str, Any]] = self.control('GET', 'requests')['requests']
        return entries

    def script(self, *responses: dict[str, Any]) -> None:
        self.control('POST', 'responses', {'responses': list(responses)})


def _read_lines(stream: Any, lines: 'queue.Queue[str | None]') -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)


def _start(log: Path) -> tuple[subprocess.Popen[str], dict[str, str]]:
    uv = shutil.which('uv')
    if uv is None or not (MOCK_SERVER_DIR / 'pyproject.toml').is_file():
        pytest.skip('the mock server is not available: it needs uv and clients/mock-server')
    # The environment of this SDK is not the one of the mock server.
    environment = {name: value for name, value in os.environ.items() if name not in PROJECT_VARIABLES}
    with log.open('w', encoding='utf-8') as errors:
        process = subprocess.Popen(
            [uv, 'run', '--quiet', '--locked', '--project', str(MOCK_SERVER_DIR), 'oscar-mock-server', '--port', '0'],
            cwd=MOCK_SERVER_DIR,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding='utf-8',
        )
    lines: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_read_lines, args=(process.stdout, lines), daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=START_TIMEOUT)
        except queue.Empty:
            line = None
        if line is None:
            process.kill()
            process.wait()
            pytest.fail(f'The mock server did not start:\n{log.read_text(encoding="utf-8")}')
        if line.startswith(READY_PREFIX):
            return process, json.loads(line[len(READY_PREFIX) :])


@pytest.fixture(scope='session')
def mock_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MockServer]:
    process, info = _start(tmp_path_factory.mktemp('mock-server') / 'stderr.log')
    server = MockServer(
        url=info['url'],
        api_key=info['api_key'],
        company_id=info['company_id'],
        organization_id=info['organization_id'],
        resource=info['resource'],
    )
    try:
        yield server
    finally:
        with contextlib.suppress(httpx.HTTPError):
            server.control('POST', 'shutdown')
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture
def server(mock_server: MockServer) -> MockServer:
    """The mock server with a fresh state, and device codes that may be polled at once."""
    mock_server.control('POST', 'reset')
    mock_server.control('POST', 'config', {'device_interval': 0, 'device_enforce_interval': False})
    return mock_server
