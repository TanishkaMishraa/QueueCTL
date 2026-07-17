"""Tests for the 3-file logging split: error.log should receive
ERROR-level records from both the app and worker loggers via Python's
logging propagation, without a second explicit log call anywhere -- and
INFO/WARNING records should stay out of it.
"""

import logging

import pytest

from queuectl import app_logging


@pytest.fixture(autouse=True)
def _reset_logging_handlers():
    # logging.getLogger(name) returns the same cached object across
    # tests, so handlers from a previous test's (already-deleted) tmp_path
    # would otherwise keep accumulating. Clear them after each test so the
    # next one starts fresh and re-attaches against its own tmp_path.
    yield
    for name in ("queuectl", "queuectl.app", "queuectl.worker"):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def _read(path):
    return path.read_text() if path.exists() else ""


def test_app_logger_writes_to_queuectl_log(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUECTL_LOG_DIR", str(tmp_path))
    app_logging.get_app_logger().info("hello from app")

    assert "hello from app" in _read(tmp_path / "queuectl.log")
    assert "hello from app" not in _read(tmp_path / "worker.log")
    assert "hello from app" not in _read(tmp_path / "error.log")


def test_worker_logger_writes_to_worker_log(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUECTL_LOG_DIR", str(tmp_path))
    app_logging.get_worker_logger().info("worker did a thing")

    assert "worker did a thing" in _read(tmp_path / "worker.log")
    assert "worker did a thing" not in _read(tmp_path / "queuectl.log")
    assert "worker did a thing" not in _read(tmp_path / "error.log")


def test_error_level_propagates_to_error_log_from_worker_logger(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUECTL_LOG_DIR", str(tmp_path))
    app_logging.get_worker_logger().error("job exceeded retries")

    assert "job exceeded retries" in _read(tmp_path / "worker.log")
    assert "job exceeded retries" in _read(tmp_path / "error.log")


def test_error_level_propagates_to_error_log_from_app_logger(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUECTL_LOG_DIR", str(tmp_path))
    app_logging.get_app_logger().error("something broke")

    assert "something broke" in _read(tmp_path / "queuectl.log")
    assert "something broke" in _read(tmp_path / "error.log")


def test_warning_level_does_not_reach_error_log(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUECTL_LOG_DIR", str(tmp_path))
    app_logging.get_worker_logger().warning("retrying soon")

    assert "retrying soon" in _read(tmp_path / "worker.log")
    assert "retrying soon" not in _read(tmp_path / "error.log")
