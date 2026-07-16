import pytest

from queuectl import database


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test_queuectl.db"
    monkeypatch.setenv("QUEUECTL_DB", str(path))
    return path


@pytest.fixture
def session(db_path):
    s = database.get_session(db_path)
    yield s
    s.close()
