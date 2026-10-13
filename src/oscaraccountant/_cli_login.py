"""Step 3 of the credential order: the stored login of the `oscar` CLI (clients/spec/README.md, section 1.3).

It runs only when the caller asks for it. The token is never logged, never put in an error message and
never written to disk.
"""

import datetime as dt
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._base_url import normalize_base_url
from ._errors import AuthenticationError, ConfigurationError

#: The first line of `oscar --version`.
VERSION_PATTERN = re.compile(r'^oscar-cli (\d+\.\d+\.\d+)(\S*)$')

#: The timeout of `oscar auth token`, which may refresh the token first, and of `oscar --version`.
TOKEN_TIMEOUT = 30.0

#: A token is fetched again this many seconds before it expires.
EXPIRY_MARGIN = 60.0

#: The extensions of `PATHEXT` that the SDK runs on Windows.
_WINDOWS_EXTENSIONS = ('.exe', '.cmd')
_DEFAULT_PATHEXT = '.COM;.EXE;.BAT;.CMD'

#: The exit code of the CLI for "authentication needed".
EXIT_AUTH = 3

#: The CLI executables whose version was checked in this process.
_checked: set[str] = set()
_checked_lock = threading.Lock()


def _candidates(windows: bool, pathext: str | None) -> list[str]:
    if not windows:
        return ['oscar']
    extensions = [ext.lower() for ext in (pathext or _DEFAULT_PATHEXT).split(';') if ext]
    return [f'oscar{ext}' for ext in extensions if ext in _WINDOWS_EXTENSIONS]


def find_oscar(
    path: str | None,
    *,
    windows: bool = os.name == 'nt',
    pathext: str | None = None,
    pathsep: str = os.pathsep,
) -> Path | None:
    """The absolute path of `oscar` in the directories of `PATH`, or `None`.

    Only absolute directories count: never the current directory, also not on Windows, and never a
    relative entry, which resolves against it. On Windows, `oscar.exe` or `oscar.cmd` in the order of
    `PATHEXT`.
    """
    names = _candidates(windows, pathext)
    for entry in (path or '').split(pathsep):
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _run(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the CLI with no shell and the environment of the caller."""
    try:
        return subprocess.run(  # noqa: S603 - an absolute path found on PATH, fixed arguments, no shell
            list(arguments),
            capture_output=True,
            text=True,
            timeout=TOKEN_TIMEOUT,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthenticationError.no_credential(
            f'`{arguments[0]}` did not answer within {TOKEN_TIMEOUT:g} seconds.'
        ) from exc
    except OSError as exc:
        raise AuthenticationError.no_credential(f'`{arguments[0]}` could not be run: {exc.strerror}.') from exc


def check_version(executable: Path) -> None:
    """Require `oscar-cli <semver>` on the first line of `oscar --version`, once per process and executable."""
    key = str(executable)
    with _checked_lock:
        if key in _checked:
            return
    result = _run([key, '--version'])
    first_line = result.stdout.splitlines()[0].strip() if result.stdout else ''
    if result.returncode != 0 or not VERSION_PATTERN.match(first_line):
        raise AuthenticationError.no_credential(f'`{executable}` is not the oscar CLI.')
    with _checked_lock:
        _checked.add(key)


@dataclass(frozen=True)
class CliToken:
    """The answer of `oscar auth token --json`."""

    token: str
    #: A UNIX time, or `None` for an API key, which does not expire.
    expires_at: float | None
    kind: str | None
    base_url: str
    profile: str | None


def _expiry(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError('expires_at is not a string')
    when = dt.datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when.timestamp()


def _decode(stdout: str) -> CliToken:
    answer = json.loads(stdout)
    if not isinstance(answer, dict):
        raise TypeError('not an object')
    token = answer.get('token')
    base_url = answer.get('base_url')
    if not isinstance(token, str) or not token or not isinstance(base_url, str):
        raise TypeError('token or base_url missing')
    kind = answer.get('kind')
    profile = answer.get('profile')
    return CliToken(
        token=token,
        expires_at=_expiry(answer.get('expires_at')),
        kind=kind if isinstance(kind, str) else None,
        base_url=base_url,
        profile=profile if isinstance(profile, str) else None,
    )


def parse_token(stdout: str) -> CliToken:
    """Read the JSON object of `oscar auth token --json`. The message of an error never holds the output."""
    try:
        return _decode(stdout)
    except (TypeError, ValueError) as exc:
        raise AuthenticationError.no_credential('The oscar CLI gave an answer the SDK cannot read.') from exc


class CliLogin:
    """The token of the stored login of the CLI, for one base URL and profile, cached in memory."""

    def __init__(
        self,
        base_url: str,
        profile: str | None,
        *,
        clock: Callable[[], float],
        path: Callable[[], str | None] = lambda: os.environ.get('PATH'),
    ) -> None:
        self._base_url = base_url
        self._profile = profile
        self._clock = clock
        self._path = path
        self._lock = threading.Lock()
        self._cached: CliToken | None = None

    def token(self) -> str:
        """A token for the base URL. The cache holds until 60 seconds before it expires."""
        with self._lock:
            cached = self._cached
            if cached is None or (cached.expires_at is not None and self._clock() >= cached.expires_at - EXPIRY_MARGIN):
                cached = self._fetch()
                self._cached = cached
            return cached.token

    def invalidate(self) -> None:
        """Forget the cached token, after a 401 of the API."""
        with self._lock:
            self._cached = None

    def _fetch(self) -> CliToken:
        executable = find_oscar(self._path())
        if executable is None:
            raise AuthenticationError.no_credential(
                'The oscar CLI is not installed: no `oscar` on PATH. Install it (npx @oscaraccountant/cli), '
                'or set OSCAR_API_KEY.'
            )
        check_version(executable)
        arguments = [str(executable), 'auth', 'token', '--json', '--base-url', self._base_url]
        if self._profile:
            arguments += ['--profile', self._profile]
        result = _run(arguments)
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            reason = lines[0] if lines else 'no message'
            hint = ' Run `oscar login` first.' if result.returncode == EXIT_AUTH else ''
            raise AuthenticationError.no_credential(
                f'`oscar auth token` failed with exit code {result.returncode}: {reason}.{hint}'
            )
        answer = parse_token(result.stdout)
        try:
            answered_for = normalize_base_url(answer.base_url)
        except ConfigurationError:
            answered_for = answer.base_url
        if answered_for != self._base_url:
            raise AuthenticationError.no_credential(
                f'The oscar CLI answered for {answered_for}, not for {self._base_url}. The token is not used.'
            )
        return answer
