from sqlalchemy import inspect

from queuectl import database


def test_get_db_path_respects_env_var(tmp_path, monkeypatch):
    override = tmp_path / "custom" / "queuectl.db"
    monkeypatch.setenv("QUEUECTL_DB", str(override))
    path = database.get_db_path()
    assert path == override
    assert path.parent.exists()  # parent dir is created eagerly


def test_get_db_path_defaults_under_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("QUEUECTL_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    path = database.get_db_path()
    assert path == tmp_path / "queuectl_data" / "queuectl.db"


def test_get_session_creates_all_tables(db_path):
    session = database.get_session(db_path)
    try:
        table_names = set(inspect(session.get_bind()).get_table_names())
        assert {"jobs", "config", "workers", "job_logs"} <= table_names
    finally:
        session.close()


def test_get_session_seeds_config_defaults(db_path):
    session = database.get_session(db_path)
    try:
        from queuectl.models import Config

        keys = {row.key for row in session.query(Config).all()}
        assert "max_retries" in keys
        assert "backoff_base" in keys
    finally:
        session.close()


def test_init_db_is_idempotent(db_path):
    database.init_db(db_path)
    database.init_db(db_path)  # must not raise / must not duplicate anything

    session = database.get_session(db_path)
    try:
        from queuectl.models import Config

        rows = session.query(Config).filter(Config.key == "max_retries").all()
        assert len(rows) == 1
    finally:
        session.close()


def test_get_session_isolation_level_disabled_for_manual_begin_immediate(db_path):
    """Sanity check on the mechanism claim_job's locking depends on: the
    raw DBAPI connection must have isolation_level=None so database.py's
    'begin' event hook is the only thing issuing BEGIN, letting it use
    BEGIN IMMEDIATE instead of pysqlite's own implicit transaction."""
    session = database.get_session(db_path)
    try:
        raw_conn = session.connection().connection.dbapi_connection
        assert raw_conn.isolation_level is None
    finally:
        session.close()
