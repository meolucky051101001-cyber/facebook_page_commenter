"""Prove test isolation using synthetic audit events, without opening secrets."""
from pathlib import Path
import sys

import pytest

import config
import database as db


@pytest.mark.parametrize("name", [".env", "fb_commenter.db", "fb_commenter.db-wal", "fb_commenter.db.bak"])
def test_operator_files_are_rejected_before_open(name):
    with pytest.raises(AssertionError, match="operator credentials"):
        sys.audit("open", str(Path(__file__).parent / name), "r", 0)


def test_sqlite_operator_path_is_rejected_before_connect():
    with pytest.raises(AssertionError, match="production database"):
        sys.audit("sqlite3.connect", str(Path(__file__).parent / "fb_commenter.db"))


def test_dotenv_disabled_and_database_is_temporary(tmp_path):
    assert config.DOTENV_DISABLED
    assert db.DB_PATH.parent == tmp_path
    assert db.DB_PATH.name != "fb_commenter.db"
