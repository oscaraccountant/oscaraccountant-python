import tomllib
from pathlib import Path

import pytest

import oscaraccountant
from oscaraccountant import types

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_the_project():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert project['project']['version'] == oscaraccountant.__version__


def test_version_matches_clients_version():
    version_file = ROOT.parent / 'VERSION'
    if not version_file.exists():
        pytest.skip('outside the source repository')
    assert version_file.read_text(encoding='utf-8').strip() == oscaraccountant.__version__


def test_exports():
    for name in oscaraccountant.__all__:
        assert hasattr(oscaraccountant, name)
    assert types.BooksInvoice.__name__ == 'BooksInvoice'
    assert (ROOT / 'src' / 'oscaraccountant' / 'py.typed').exists()
