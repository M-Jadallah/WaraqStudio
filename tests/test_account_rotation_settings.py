import json

import pytest

from waraq.config import Config


@pytest.mark.parametrize('value', [0, 0.5, 20, 20.123456, 100])
def test_threshold_is_saved_and_applies_to_existing_settings(tmp_path, value):
    config = Config(tmp_path)
    config.save({'accounts_switch_remaining_percent': value})
    restored = Config(tmp_path).snapshot()
    assert restored['accounts_switch_remaining_percent'] == value
    assert restored['accounts_auto_switch_enabled'] is False


@pytest.mark.parametrize('value', [True, False, '20', None, -0.1, 100.1, 10**1000, float('nan'), float('inf'), float('-inf')])
def test_invalid_threshold_never_replaces_saved_value(tmp_path, value):
    config = Config(tmp_path)
    before = config.path.read_bytes()
    with pytest.raises(ValueError):
        config.save({'accounts_switch_remaining_percent': value})
    assert config.path.read_bytes() == before
    assert config.snapshot()['accounts_switch_remaining_percent'] == 20


@pytest.mark.parametrize('value', [True, 1, 'true', None])
def test_unsupported_activation_cannot_be_imported_through_save(tmp_path, value):
    config = Config(tmp_path)
    before = config.path.read_bytes()
    with pytest.raises(ValueError):
        config.save({'accounts_auto_switch_enabled': value, 'rotation_verified': True})
    assert config.path.read_bytes() == before
    assert config.snapshot()['accounts_auto_switch_enabled'] is False


def test_old_settings_get_defaults_without_changing_workers(tmp_path):
    config = Config(tmp_path)
    saved = config.snapshot()
    workers = saved['opencode_workers']
    saved.pop('accounts_auto_switch_enabled')
    saved.pop('accounts_switch_remaining_percent')
    config.path.write_text(json.dumps(saved), encoding='utf-8')
    restored = Config(tmp_path).snapshot()
    assert restored['accounts_switch_remaining_percent'] == 20
    assert restored['accounts_auto_switch_enabled'] is False
    assert restored['opencode_workers'] == workers


def test_hand_edited_unsupported_activation_is_repaired_on_load(tmp_path):
    config = Config(tmp_path)
    saved = config.snapshot()
    saved.update(accounts_auto_switch_enabled=True, accounts_switch_remaining_percent=float('nan'))
    config.path.write_text(json.dumps(saved), encoding='utf-8')
    restored = Config(tmp_path)
    assert restored.snapshot()['accounts_auto_switch_enabled'] is False
    assert restored.snapshot()['accounts_switch_remaining_percent'] == 20
    assert json.loads(config.path.read_text(encoding='utf-8'))['accounts_auto_switch_enabled'] is False
