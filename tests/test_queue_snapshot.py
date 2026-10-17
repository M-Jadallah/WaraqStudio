from __future__ import annotations

import json

import pytest
from qt_test_support import dispose_bridge

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


@pytest.fixture
def snapshot_env(tmp_path, scheduler_harness, qt_app):
    app = qt_app
    config, store = Config(tmp_path), Store(tmp_path / "queue.db")
    scheduler = Scheduler(store, config, None)
    bridge = Bridge(store, config, None, scheduler)
    bridge.timer.stop()
    jobs_changed, changed = [], []
    bridge.jobsChanged.connect(lambda: jobs_changed.append(bridge.jobsJson))
    bridge.changed.connect(lambda: changed.append(True))
    try:
        yield bridge, store, config, scheduler, jobs_changed, changed
    finally:
        bridge.closeAccounts()
        scheduler_harness.shutdown_and_join()
        dispose_bridge(bridge)
        with store.lock:
            store.db.close()


def add_job(store, config):
    return store.add([{"video_id": "one", "title": "درس عربي", "position": 1}], config.snapshot())[0]


def test_queue_snapshot_preserves_unicode_and_existing_jobs_model(snapshot_env):
    bridge, store, config, _, jobs_changed, _ = snapshot_env
    assert bridge.jobsJson == "[]"
    ident = add_job(store, config)
    bridge.refresh()
    snapshot = json.loads(bridge.jobsJson)
    assert snapshot == bridge.jobs
    assert snapshot[0]["title"] == "درس عربي"
    assert "درس عربي" in bridge.jobsJson
    assert snapshot[0]["id"] == ident
    assert bridge.jobsModel.rowCount() == 1
    assert len(jobs_changed) == 1
    assert jobs_changed[0] == bridge.jobsJson


def test_snapshot_getter_is_cached_and_idle_refresh_emits_nothing(snapshot_env, monkeypatch):
    bridge, store, config, _, jobs_changed, changed = snapshot_env
    add_job(store, config)
    bridge.refresh()
    previous = bridge.jobsJson
    jobs_changed.clear()
    changed.clear()
    def no_serialization(*args, **kwargs):
        raise AssertionError("Unchanged snapshot was serialized again")
    monkeypatch.setattr("waraq.bridge.json.dumps", no_serialization)
    for _ in range(20):
        assert bridge.jobsJson is previous
    bridge.refresh()
    assert not jobs_changed and not changed


def test_worker_elapsed_and_log_changes_do_not_invalidate_queue_snapshot(snapshot_env, monkeypatch):
    bridge, store, config, scheduler, jobs_changed, changed = snapshot_env
    add_job(store, config)
    bridge.refresh()
    previous = bridge.jobsJson
    jobs_changed.clear()
    changed.clear()
    workers = scheduler.workers()
    workers[0]["elapsed"] = 99
    monkeypatch.setattr(scheduler, "workers", lambda: workers)
    bridge.refresh()
    assert changed
    assert not jobs_changed
    assert bridge.jobsJson is previous
    changed.clear()
    store.event("", "حدث في السجل فقط")
    bridge.refresh()
    assert changed
    assert not jobs_changed
    assert bridge.jobsJson is previous


def test_job_progress_and_deletion_each_publish_one_consistent_snapshot(snapshot_env):
    bridge, store, config, scheduler, jobs_changed, _ = snapshot_env
    ident = add_job(store, config)
    bridge.refresh()
    jobs_changed.clear()
    store.progress(ident, "download", 42)
    bridge.refresh()
    assert len(jobs_changed) == 1
    assert json.loads(jobs_changed[0])[0]["progress"] == 42
    jobs_changed.clear()
    assert scheduler.delete_jobs([ident])["deleted"] == [ident]
    bridge.refresh()
    assert jobs_changed == ["[]"]
    assert not bridge.jobs
    assert bridge.jobsModel.rowCount() == 0
