"""The `User-Agent` of the SDK (clients/spec/README.md, section 3.9)."""

import platform

from ._version import __version__

_SYSTEMS = {'linux': 'linux', 'darwin': 'macos', 'windows': 'windows'}
_MACHINES = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'arm64': 'arm64', 'aarch64': 'arm64'}


def user_agent(
    *,
    system: str | None = None,
    machine: str | None = None,
    python_version: str | None = None,
) -> str:
    """`oscaraccountant-python/<version> (<os>; <arch>; python/<version>)`. The arguments are for the tests."""
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    python_version = platform.python_version() if python_version is None else python_version
    os_name = _SYSTEMS.get(system.lower(), 'unknown')
    arch = _MACHINES.get(machine.lower(), machine.lower() or 'unknown')
    return f'oscaraccountant-python/{__version__} ({os_name}; {arch}; python/{python_version})'
