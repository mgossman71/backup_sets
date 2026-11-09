"""Shared fixtures. Helpers live in helpers.py (importable from every test module)."""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from helpers import BackupEnv  # noqa: E402


@pytest.fixture
def bk(tmp_path):
    return BackupEnv(tmp_path)
