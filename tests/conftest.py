import pytest

from queuectl import db as db_mod


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test_queuectl.db"
    monkeypatch.setenv("QUEUECTL_DB", str(path))
    return path


@pytest.fixture
def conn(db_path):
    connection = db_mod.connect(db_path)
    yield connection
    connection.close()
