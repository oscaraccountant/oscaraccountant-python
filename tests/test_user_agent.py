import platform

import pytest

from oscaraccountant import __version__
from oscaraccountant._user_agent import user_agent


@pytest.mark.parametrize(
    ('system', 'machine', 'expected'),
    [
        ('Linux', 'x86_64', 'linux; x86_64'),
        ('Darwin', 'arm64', 'macos; arm64'),
        ('Windows', 'AMD64', 'windows; x86_64'),
        ('Linux', 'aarch64', 'linux; arm64'),
        ('FreeBSD', 'riscv64', 'unknown; riscv64'),
        ('', '', 'unknown; unknown'),
    ],
)
def test_user_agent(system, machine, expected):
    value = user_agent(system=system, machine=machine, python_version='3.14.2')
    assert value == f'oscaraccountant-python/{__version__} ({expected}; python/3.14.2)'


def test_user_agent_of_this_machine():
    assert user_agent().endswith(f'python/{platform.python_version()})')
