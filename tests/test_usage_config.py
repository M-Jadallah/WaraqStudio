"""Embedded account settings have one-way migration and bounded polling."""
import json

import pytest

from waraq.config import Config


def test_old_settings_migrate_without_an_old_runtime_toggle(tmp_path):
    config = Config(tmp_path)
    saved = config.snapshot()
    for key in ("accounts_enabled", "accounts_max_concurrency", "accounts_auto_refresh", "accounts_refresh_seconds"):
        saved.pop(key)
    saved.update(chatgpt_accounts_enabled=False, chatgpt_account_concurrency=3,
                 chatgpt_usage_auto_refresh=False, chatgpt_usage_refresh_seconds=600)
    config.path.write_text(json.dumps(saved), encoding="utf-8")
    restored = Config(tmp_path).snapshot()
    assert restored["accounts_enabled"] is True
    assert restored["accounts_max_concurrency"] == 3
    assert restored["accounts_auto_refresh"] is False
    assert restored["accounts_refresh_seconds"] == 600
    assert not any(key.startswith("chatgpt_") for key in restored)
    assert json.loads(config.path.read_text(encoding="utf-8")) == restored


@pytest.mark.parametrize("value", [True, False, 59, 1801, 60.5, "300", None, float("nan"), float("inf")])
def test_usage_interval_rejects_unsafe_or_noninteger_values(tmp_path, value):
    config = Config(tmp_path)
    with pytest.raises(ValueError):
        config.save({"accounts_refresh_seconds": value})


@pytest.mark.parametrize("value", [60, 300, 1800])
def test_usage_interval_accepts_supported_range(tmp_path, value):
    config = Config(tmp_path)
    config.save({"accounts_refresh_seconds": value, "accounts_auto_refresh": False})
    assert Config(tmp_path).snapshot()["accounts_refresh_seconds"] == value
    assert Config(tmp_path).snapshot()["accounts_auto_refresh"] is False


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_usage_auto_refresh_requires_boolean(tmp_path, value):
    config = Config(tmp_path)
    with pytest.raises(ValueError):
        config.save({"accounts_auto_refresh": value})
