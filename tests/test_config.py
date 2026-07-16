import pytest

from queuectl import config
from queuectl.exceptions import InvalidConfiguration


def test_get_config_returns_defaults_when_table_empty(session):
    assert config.get_config(session, "max_retries") == "3"
    assert config.get_config(session, "backoff_base") == "2"


def test_load_defaults_seeds_all_known_keys(session):
    config.load_defaults(session)
    all_values = config.get_all(session)
    for key in config.DEFAULTS:
        assert key in all_values


def test_set_config_overrides_default(session):
    config.set_config(session, "max_retries", "5")
    assert config.get_config(session, "max_retries") == "5"


def test_set_config_then_get_all_reflects_override(session):
    config.set_config(session, "backoff_base", "3")
    values = config.get_all(session)
    assert values["backoff_base"] == "3"
    assert values["max_retries"] == "3"  # untouched key still shows its default


def test_get_config_unknown_key_raises(session):
    with pytest.raises(InvalidConfiguration):
        config.get_config(session, "not_a_real_key")


def test_reset_config_single_key_restores_default(session):
    config.set_config(session, "max_retries", "9")
    config.reset_config(session, "max_retries")
    assert config.get_config(session, "max_retries") == "3"


def test_reset_config_unknown_key_raises(session):
    with pytest.raises(InvalidConfiguration):
        config.reset_config(session, "not_a_real_key")


def test_reset_config_all_restores_every_default(session):
    config.set_config(session, "max_retries", "9")
    config.set_config(session, "backoff_base", "9")
    config.reset_config(session)
    values = config.get_all(session)
    assert values["max_retries"] == "3"
    assert values["backoff_base"] == "2"


def test_get_int_and_get_float(session):
    config.set_config(session, "max_retries", "7")
    config.set_config(session, "backoff_base", "1.5")
    assert config.get_int(session, "max_retries") == 7
    assert config.get_float(session, "backoff_base") == 1.5
