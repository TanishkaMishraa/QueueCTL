import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def get_db_path() -> Path:
    override = os.environ.get("QUEUECTL_DB")
    path = Path(override) if override else Path.cwd() / "queuectl_data" / "queuectl.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _make_engine(db_path: Path = None):
    path = db_path or get_db_path()
    engine = create_engine(f"sqlite:///{path}", future=True)

    # pysqlite opens its own implicit transactions and disables SQLite's
    # native BEGIN, which makes it impossible to request BEGIN IMMEDIATE.
    # Disabling pysqlite's transaction handling and emitting BEGIN
    # IMMEDIATE ourselves on every "begin" restores the locking guarantee
    # queue_ops.claim_job relies on: two workers racing to claim a job are
    # serialized by SQLite's write lock instead of both seeing it as free.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _do_begin_immediate(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def init_db(db_path: Path = None) -> None:
    from queuectl import models  # noqa: F401  (registers Job/Config/Worker on Base)

    engine = _make_engine(db_path)
    Base.metadata.create_all(bind=engine)


def get_session(db_path: Path = None):
    from queuectl import models  # noqa: F401

    engine = _make_engine(db_path)
    Base.metadata.create_all(bind=engine)
    # expire_on_commit (default True) matters here: a worker process holds
    # one session for its whole lifetime, and must see stop_requested /
    # job-state changes committed by *other* processes, not a stale
    # in-memory copy from before its last commit.
    SessionLocal = sessionmaker(bind=engine, future=True)
    return SessionLocal()
