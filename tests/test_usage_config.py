"""Quota polling settings remain bounded and load with older saved settings."""
import json

import pytest

from waraq.config import Config


def test_usage_defaults_load_when_old_settings_omit_new_keys(tmp_path):
    config = Config(tmp_path)
    saved = config.snapshot()
    saved.pop("chatgpt_usage_auto_refresh")
    saved.pop("chatgpt_usage_refresh_seconds")
    config.path.write_text(json.dumps(saved), encoding="utf-8")
    restored = Config(tmp_path)
    assert restored.snapshot()["chatgpt_usage_auto_refresh"] is True
    assert restored.snapshot()["chatgpt_usage_refresh_seconds"] == 300


@pytest.mark.parametrize("value", [True, False, 59, 1801, 60.5, "300", None, float("nan"), float("inf")])
def test_usage_interval_rejects_unsafe_or_noninteger_values(tmp_path, value):
    config = Config(tmp_path)
    with pytest.raises(ValueError):
        config.save({"chatgpt_usage_refresh_seconds": value})


@pytest.mark.parametrize("value", [60, 300, 1800])
def test_usage_interval_accepts_supported_range(tmp_path, value):
    config = Config(tmp_path)
    config.save({"chatgpt_usage_refresh_seconds": value, "chatgpt_usage_auto_refresh": False})
    assert Config(tmp_path).snapshot()["chatgpt_usage_refresh_seconds"] == value
    assert Config(tmp_path).snapshot()["chatgpt_usage_auto_refresh"] is False


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_usage_auto_refresh_requires_boolean(tmp_path, value):
    config = Config(tmp_path)
    with pytest.raises(ValueError):
        config.save({"chatgpt_usage_auto_refresh": value})
