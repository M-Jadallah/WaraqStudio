from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


@pytest.fixture
def queue(tmp_path, scheduler_harness):
    config = Config(tmp_path)
    config.data["library_auto"] = False
    store = Store(tmp_path / "tasks.db")
    try:
        yield store, config
    finally:
        scheduler_harness.shutdown_and_join()
        with store.lock:
            store.db.close()


def add_jobs(store, config, count=1):
    return store.add([{"video_id": str(i), "title": f"درس {i}", "position": i}
                      for i in range(1, count + 1)], config.snapshot())


def add_transcript(store, config, tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("التفريغ الأصلي", encoding="utf-8-sig")
    ident = store.add([{"video_id": "txt", "title": "درس", "position": 1}],
                      config.snapshot(), input_path=str(source))[0]
    return ident, source


def wait_idle(scheduler, timeout=3):
    deadline = time.monotonic() + timeout
    while not scheduler.idle() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert scheduler.idle(), "Worker did not finish"


def test_delete_multiple_classifies_deduplicates_and_preserves_order(queue):
    store, config = queue
    first, busy, third, fourth = add_jobs(store, config, 4)
    assert store.claim(busy, "download", "test")
    scheduler = Scheduler(store, config, None)
    assert scheduler.delete_jobs([third, busy, "missing", first, third, "missing"]) == {
        "deleted": [third, first], "busy": [busy], "missing": ["missing"]}
    assert [job["id"] for job in store.jobs()] == [busy, fourth]
    assert [job["position"] for job in store.jobs()] == [2, 4]
    assert [job["seq"] for job in store.jobs()] == [2, 4]
    assert store.db.execute("SELECT COUNT(*) FROM stages WHERE job_id IN (?,?)", (first, third)).fetchone()[0] == 0
    assert scheduler.delete_jobs([first]) == {"deleted": [], "busy": [], "missing": [first]}
    assert scheduler.delete_jobs([]) == {"deleted": [], "busy": [], "missing": []}


def test_delete_preserves_source_outputs_library_and_survives_reopen(queue, tmp_path):
    store, config = queue
    ident, source = add_transcript(store, config, tmp_path)
    job = store.get(ident)
    output = Path(job["folder"]) / "result.docx"
    output.write_bytes(b"original output bytes")
    store.artifact(ident, "document", output)
    store.set_stage(ident, "opencode", "completed")
    scheduler = Scheduler(store, config, None)
    scheduler.library.publish_current(store, ident, config.snapshot())
    library_files = {Path(item["path"]): Path(item["path"]).read_bytes()
                     for item in scheduler.library.scan(config.snapshot())}
    assert library_files
    files = [source, Path(job["transcript"]), output]
    originals = {path: path.read_bytes() for path in files}
    assert scheduler.delete_jobs([ident])["deleted"] == [ident]
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert all(path.read_bytes() == data for path, data in library_files.items())
    reloaded = Store(store.path)
    try:
        assert reloaded.get(ident) is None
        assert reloaded.db.execute("SELECT COUNT(*) FROM stages WHERE job_id=?", (ident,)).fetchone()[0] == 0
    finally:
        reloaded.db.close()


def test_store_delete_is_atomic_and_rolls_back_stages_on_database_failure(queue):
    store, config = queue
    first, second = add_jobs(store, config, 2)
    store.db.execute(f"CREATE TRIGGER prevent_second_delete BEFORE DELETE ON jobs WHEN OLD.id='{second}' BEGIN SELECT RAISE(ABORT,'cannot delete'); END")
    with pytest.raises(sqlite3.IntegrityError, match="cannot delete"):
        store.delete_jobs([first, second])
    assert [job["id"] for job in store.jobs()] == [first, second]
    assert store.db.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 6


def test_store_claim_races_deletion_without_orphan_or_deleted_claim(queue):
    store, config = queue
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(12):
            ident = add_jobs(store, config)[0]
            barrier = threading.Barrier(2)
            def claim():
                barrier.wait(timeout=3)
                return store.claim(ident, "download", "test")
            def delete():
                barrier.wait(timeout=3)
                return store.delete_jobs([ident])
            claimed_future, deleted_future = pool.submit(claim), pool.submit(delete)
            claimed, result = claimed_future.result(timeout=3), deleted_future.result(timeout=3)
            if claimed:
                assert result["busy"] == [ident]
                assert store.get(ident)["stages"]["download"]["status"] == "running"
                store.set_stage(ident, "download", "completed")
                assert store.delete_jobs([ident])["deleted"] == [ident]
            else:
                assert result["deleted"] == [ident]
                assert store.get(ident) is None
            assert store.db.execute("SELECT COUNT(*) FROM stages WHERE job_id=?", (ident,)).fetchone()[0] == 0


def test_scheduler_delete_blocks_claim_registration_gap(queue, tmp_path, monkeypatch):
    store, config = queue
    ident, _ = add_transcript(store, config, tmp_path)
    claimed, register, finish = threading.Event(), threading.Event(), threading.Event()
    original_claim = store.claim
    def held_claim(*args):
        result = original_claim(*args)
        if result:
            claimed.set()
            assert register.wait(3)
        return result
    monkeypatch.setattr(store, "claim", held_claim)
    class HeldOC:
        def process(self, job, *args):
            assert finish.wait(3)
            return Path(job["transcript"])
    scheduler = Scheduler(store, config, None, opencode=HeldOC())
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(scheduler.tick)
        assert claimed.wait(3)
        deletion = pool.submit(scheduler.delete_jobs, [ident])
        assert not deletion.done()
        register.set()
        dispatch.result(timeout=3)
        assert deletion.result(timeout=3) == {"deleted": [], "busy": [ident], "missing": []}
        finish.set()
    wait_idle(scheduler)
    assert scheduler.delete_jobs([ident])["deleted"] == [ident]


def test_deleted_snapshot_is_never_dispatched(queue, tmp_path, monkeypatch):
    store, config = queue
    ident, _ = add_transcript(store, config, tmp_path)
    ready, release = threading.Event(), threading.Event()
    calls = []
    class FakeOC:
        def process(self, *args):
            calls.append(True)
            raise AssertionError("deleted job dispatched")
    scheduler = Scheduler(store, config, None, opencode=FakeOC())
    original_ready = scheduler.ready
    def held_ready(*args):
        ready.set()
        assert release.wait(3)
        return original_ready(*args)
    monkeypatch.setattr(scheduler, "ready", held_ready)
    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatch = pool.submit(scheduler.tick)
        assert ready.wait(3)
        assert scheduler.delete_jobs([ident])["deleted"] == [ident]
        release.set()
        dispatch.result(timeout=3)
    assert not calls
    assert scheduler.idle()
    assert not store.jobs()


def test_completed_job_stays_busy_until_library_publication_finishes(queue, tmp_path):
    store, config = queue
    config.data["library_auto"] = True
    ident, _ = add_transcript(store, config, tmp_path)
    publishing, release = threading.Event(), threading.Event()
    class FakeOC:
        def process(self, job, *args):
            return Path(job["transcript"])
    class HeldLibrary:
        def publish_current(self, store, job_id, cfg):
            publishing.set()
            assert release.wait(3)
            assert store.get(job_id)
            return []
    scheduler = Scheduler(store, config, None, opencode=FakeOC(), library=HeldLibrary())
    scheduler.tick()
    assert publishing.wait(3)
    assert store.get(ident)["stages"]["opencode"]["status"] == "completed"
    try:
        assert scheduler.delete_jobs([ident]) == {"deleted": [], "busy": [ident], "missing": []}
        with pytest.raises(ValueError, match="نشر نتائجه"):
            scheduler.retry_job(ident, "opencode", rerun=True)
        assert store.get(ident)["stages"]["opencode"]["status"] == "completed"
        assert "revision_opencode" not in store.get(ident)["config"]
    finally:
        release.set()
    wait_idle(scheduler)
    scheduler.retry_job(ident, "opencode", rerun=True)
    assert store.get(ident)["stages"]["opencode"]["status"] == "pending"
    assert store.get(ident)["config"]["revision_opencode"]
    assert scheduler.delete_jobs([ident])["deleted"] == [ident]


def test_deleted_failed_predecessor_allows_strict_queue_to_continue(queue, tmp_path):
    store, config = queue
    config.data["strict_order"] = True
    source = tmp_path / "transcript.txt"
    source.write_text("نص", encoding="utf-8")
    first, second = store.add([{"video_id": str(i), "title": str(i), "position": i} for i in (1, 2)],
                              config.snapshot(), input_path=str(source))
    store.set_stage(first, "opencode", "failed")
    calls = []
    class FakeOC:
        def process(self, job, *args):
            calls.append(job["id"])
            return Path(job["transcript"])
    scheduler = Scheduler(store, config, None, opencode=FakeOC())
    scheduler.tick()
    assert not calls
    assert scheduler.delete_jobs([first])["deleted"] == [first]
    scheduler.tick()
    wait_idle(scheduler)
    assert calls == [second]
    assert store.get(second)["position"] == 2
    assert store.get(second)["queue_order"] == 2
    assert store.get(second)["stages"]["opencode"]["status"] == "completed"


@pytest.mark.parametrize("failure", ["construct", "start"])
def test_thread_failure_does_not_leave_undeletable_active_job(queue, tmp_path, monkeypatch, failure):
    import waraq.scheduler as scheduler_module
    store, config = queue
    ident, _ = add_transcript(store, config, tmp_path)
    scheduler = Scheduler(store, config, None, opencode=object())
    if failure == "construct":
        def broken_thread(*args, **kwargs):
            raise RuntimeError("thread construction failed")
        monkeypatch.setattr(scheduler_module.threading, "Thread", broken_thread)
    else:
        original_thread = scheduler_module.threading.Thread
        def broken_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            def fail_start():
                raise RuntimeError("thread start failed")
            thread.start = fail_start
            return thread
        monkeypatch.setattr(scheduler_module.threading, "Thread", broken_thread)
    scheduler.tick()
    assert scheduler.idle()
    assert store.get(ident)["stages"]["opencode"]["status"] == "failed"
    assert "thread" in store.get(ident)["stages"]["opencode"]["error"]
    assert scheduler.delete_jobs([ident])["deleted"] == [ident]


def test_worker_preparation_failure_releases_active_job_and_redacts_key(queue, tmp_path, monkeypatch):
    store, config = queue
    ident, _ = add_transcript(store, config, tmp_path)
    scheduler = Scheduler(store, config, None)
    def broken_prepare(worker):
        raise RuntimeError("preparation failed with secret-test-key")
    monkeypatch.setattr(scheduler, "effective_worker", broken_prepare)
    assert store.claim(ident, "opencode", "oc1")
    cancel = threading.Event()
    scheduler.active["oc1"] = {"job_id": ident, "stage": "opencode", "cancel": cancel}
    scheduler.execute(store.get(ident), "opencode", config.data["opencode_workers"][0], "secret-test-key", cancel)
    assert scheduler.idle()
    stage = store.get(ident)["stages"]["opencode"]
    assert stage["status"] == "failed"
    assert "secret-test-key" not in stage["error"]
    assert "[محجوب]" in stage["error"]
    assert scheduler.delete_jobs([ident])["deleted"] == [ident]


def test_cancel_and_dispatch_do_not_execute_a_cancelled_pending_job(queue, tmp_path, monkeypatch):
    store, config = queue
    ident, _ = add_transcript(store, config, tmp_path)
    snapshot, release = threading.Event(), threading.Event()
    original_get = store.get
    def held_get(job_id):
        job = original_get(job_id)
        if threading.current_thread().name.startswith("ThreadPoolExecutor"):
            snapshot.set()
            assert release.wait(3)
        return job
    monkeypatch.setattr(store, "get", held_get)
    calls = []
    class FakeOC:
        def process(self, *args):
            calls.append(True)
            raise AssertionError("cancelled pending job was dispatched")
    scheduler = Scheduler(store, config, None, opencode=FakeOC())
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation = pool.submit(scheduler.cancel_job, ident)
        assert snapshot.wait(3)
        try:
            # At this exact gap dispatch must be excluded. Checking the lock
            # while the cancellation snapshot is held makes the race test
            # independent of which submitted thread the OS happens to run.
            acquired = scheduler.lock.acquire(blocking=False)
            if acquired:
                scheduler.lock.release()
            assert not acquired, "Dispatch can claim between cancellation's snapshot and update"
            dispatch = pool.submit(scheduler.tick)
        finally:
            release.set()
        cancellation.result(timeout=3)
        dispatch.result(timeout=3)
    assert store.get(ident)["stages"]["opencode"]["status"] == "cancelled"
    assert not calls
    assert scheduler.idle()


@pytest.mark.parametrize("action", ["pause", "shutdown"])
def test_late_background_dispatch_does_not_claim_after_pause_or_shutdown(queue, tmp_path, monkeypatch, action):
    store, config = queue
    ident, _ = add_transcript(store, config, tmp_path)
    ready, release = threading.Event(), threading.Event()
    calls = []
    class FakeOC:
        def process(self, *args):
            calls.append(True)
            raise AssertionError("dispatch after pause/shutdown")
    scheduler = Scheduler(store, config, None, opencode=FakeOC())
    scheduler.running = True
    original_ready = scheduler.ready
    def held_ready(*args):
        ready.set()
        assert release.wait(3)
        return original_ready(*args)
    monkeypatch.setattr(scheduler, "ready", held_ready)
    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatch = pool.submit(scheduler.tick, require_running=True)
        assert ready.wait(3)
        getattr(scheduler, action)()
        release.set()
        dispatch.result(timeout=3)
    assert not calls
    assert scheduler.idle()
    assert store.get(ident)["stages"]["opencode"]["status"] == "pending"
