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
    assert values["backoff_base"] == "3.0"  # normalized through float(), per its validator
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


def test_set_config_rejects_unknown_key(session):
    with pytest.raises(InvalidConfiguration):
        config.set_config(session, "not_a_real_key", "abc")


@pytest.mark.parametrize(
    "key, bad_value",
    [
        ("max_retries", "-1"),
        ("max_retries", "abc"),
        ("backoff_base", "1"),  # must be > 1, not >=
        ("backoff_base", "0.5"),
        ("poll_interval", "0"),
        ("poll_interval", "-1"),
        ("heartbeat_interval", "0"),
        ("timeout", "0"),
        ("timeout", "-5"),
        ("max_workers", "0"),
        ("max_workers", "-2"),
        ("default_priority", "-1"),
    ],
)
def test_set_config_rejects_invalid_values(session, key, bad_value):
    with pytest.raises(InvalidConfiguration):
        config.set_config(session, key, bad_value)


def test_exists(session):
    assert config.exists(session, "max_retries") is True
    assert config.exists(session, "not_a_real_key") is False


def test_delete_is_equivalent_to_reset_single_key(session):
    config.set_config(session, "max_retries", "9")
    config.delete(session, "max_retries")
    assert config.get_config(session, "max_retries") == "3"


def test_export_config_writes_json_with_numeric_values(session, tmp_path):
    config.set_config(session, "max_retries", "5")
    out_path = tmp_path / "config.json"

    exported = config.export_config(session, out_path)
    assert exported["max_retries"] == 5  # int, not the string "5"
    assert exported["backoff_base"] == 2.0 or exported["backoff_base"] == 2

    import json

    with open(out_path) as f:
        on_disk = json.load(f)
    assert on_disk["max_retries"] == 5


def test_import_config_applies_values(session, tmp_path):
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"max_retries": 8, "backoff_base": 3}))

    config.import_config(session, path)
    assert config.get_int(session, "max_retries") == 8
    assert config.get_float(session, "backoff_base") == 3.0


def test_import_config_rejects_invalid_values(session, tmp_path):
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"max_retries": -5}))

    with pytest.raises(InvalidConfiguration):
        config.import_config(session, path)


def test_import_config_rejects_malformed_json(session, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("not json")

    with pytest.raises(InvalidConfiguration):
        config.import_config(session, path)
