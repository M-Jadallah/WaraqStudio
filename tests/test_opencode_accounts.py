from __future__ import annotations

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from waraq.config import Config
from waraq.opencode_accounts import OpenCodeAccounts
from waraq.scheduler import Scheduler
from waraq.services import ServiceError
from waraq.store import Store


def error(kind="rate_limited", reset_at=None, scope="account", confirmed=True):
    exc = ServiceError("اختبار", kind)
    exc.reset_at, exc.scope, exc.confirmed = reset_at, scope, confirmed
    return exc


@pytest.fixture
def registry(tmp_path):
    store, config = Store(tmp_path / "queue.db"), Config(tmp_path)
    config.data["chatgpt_account_concurrency"] = 5
    clock = [1000.0]
    accounts = OpenCodeAccounts(store, config, clock=lambda: clock[0])
    yield accounts, store, config, clock
    store.db.close()


def authenticated(accounts, name):
    row = accounts.create(name)
    return accounts.record_login_result(row["id"], {"authenticated": True, "identity": name,
                                                  "identity_verified": True, "email": name + "@example.test"})


def test_metadata_whitelist_and_duplicate_identity(registry):
    accounts, store, _, _ = registry
    row = authenticated(accounts, "one")
    other = accounts.create("two")
    with pytest.raises(ValueError, match="مسجل"):
        accounts.record_login_result(other["id"], {"authenticated": True, "identity": "one", "access_token": "SECRET"})
    serialized = json.dumps(accounts.list())
    assert "identity" not in serialized and "SECRET" not in serialized
    stored = " ".join(r[0] for r in store.db.execute("SELECT metadata FROM chatgpt_accounts"))
    assert "access_token" not in stored and "SECRET" not in stored
    assert row["health"] == "healthy"


def test_unknown_identity_never_counts_as_distinct_account(registry):
    accounts, _, _, _ = registry
    row = accounts.create()
    with pytest.raises(ValueError):
        accounts.record_login_result(row["id"], {"authenticated": True, "identity": "", "identity_verified": False})
    assert accounts.list()[0]["login_state"] == "needsverify"
    assert accounts.acquire("job", "worker", "openai/model")[0] is None


def test_sticky_priority_capacity_and_explicit_preference(registry):
    accounts, _, config, _ = registry
    one, two = authenticated(accounts, "one"), authenticated(accounts, "two")
    config.data["chatgpt_account_concurrency"] = 2
    first, _ = accounts.acquire("j1", "w1", "openai/model")
    second, _ = accounts.acquire("j2", "w2", "openai/model")
    assert first["account_id"] == second["account_id"] == one["id"]
    assert accounts.acquire("j3", "w3", "openai/model")[0] is None
    accounts.prefer(two["id"])
    assert accounts.acquire("j3", "w3", "openai/model")[0]["account_id"] == two["id"]


def test_quota_failover_remains_sticky_when_old_account_recovers(registry):
    accounts, _, _, clock = registry
    one, two = authenticated(accounts, "one"), authenticated(accounts, "two")
    accounts.prefer(one["id"])
    lease, _ = accounts.acquire("j", "w", "openai/model")
    assert accounts.failure(lease, error(reset_at=1100))
    accounts.release(lease)
    alternate, _ = accounts.acquire("j", "w", "openai/model")
    assert alternate["account_id"] == two["id"]
    accounts.release(alternate)
    clock[0] = 1200
    assert accounts.acquire("j", "w", "openai/model")[0]["account_id"] == two["id"]


def test_all_cooled_accounts_preserve_reset_across_restart(registry):
    accounts, store, config, clock = registry
    for name, reset in (("one", 1600), ("two", 1400), ("three", 1800)):
        authenticated(accounts, name)
        lease, _ = accounts.acquire(name, name, "openai/model")
        accounts.failure(lease, error(reset_at=reset))
        accounts.release(lease)
    restored = OpenCodeAccounts(store, config, clock=lambda: clock[0])
    assert restored.acquire("pending", "w", "openai/model") == (None, 1400)
    assert [r["reset_at"] for r in restored.list()] == [1600, 1400, 1800]


def test_unknown_reset_has_one_central_probe_and_bounded_backoff(registry):
    accounts, _, _, clock = registry
    authenticated(accounts, "one")
    lease, _ = accounts.acquire("j", "w", "openai/model")
    accounts.failure(lease, error())
    accounts.release(lease)
    assert accounts.acquire("j", "w", "openai/model") == (None, 1300)
    clock[0] = 1301
    probe, _ = accounts.acquire("j", "w", "openai/model")
    assert probe["probe"]
    assert accounts.acquire("j2", "w2", "openai/model")[0] is None
    accounts.failure(probe, error())
    accounts.release(probe)
    assert accounts.list()[0]["reset_at"] == 1901
    clock[0] = 1902
    probe, _ = accounts.acquire("j", "w", "openai/model")
    accounts.success(probe)
    accounts.release(probe)
    assert accounts.list()[0]["health"] == "healthy"


@pytest.mark.parametrize("reset", [float("inf"), float("nan"), -(10 ** 1000), 10 ** 1000, "invalid", 10 ** 20])
def test_invalid_reset_is_bounded_probe_instead_of_unusable_ui(registry, reset):
    accounts, _, _, _ = registry
    authenticated(accounts, "one")
    lease, _ = accounts.acquire("j", "w", "openai/model")
    accounts.failure(lease, error(reset_at=reset))
    accounts.release(lease)
    assert accounts.list()[0]["reset_at"] == 1300


def test_model_exclusion_normalizes_reasoning_suffix(registry):
    accounts, _, _, _ = registry
    one, two = authenticated(accounts, "one"), authenticated(accounts, "two")
    lease, _ = accounts.acquire("j", "w", "openai/model#high")
    accounts.failure(lease, error(scope="model", reset_at=2000))
    accounts.release(lease)
    assert accounts.list()[0]["health"] == "model_limited"
    assert accounts.list()[0]["reset_at"] == 2000
    assert accounts.acquire("j", "w", "openai/model#low")[0]["account_id"] == two["id"]
    accounts.prefer(one["id"])
    assert accounts.acquire("j2", "w2", "openai/other")[0]["account_id"] == one["id"]


def test_transport_error_does_not_poison_account_and_throttle_does_not_rotate(registry):
    accounts, _, _, _ = registry
    one = authenticated(accounts, "one")
    authenticated(accounts, "two")
    lease, _ = accounts.acquire("j", "w", "openai/model")
    assert not accounts.failure(lease, error("failed"))
    assert not accounts.failure(lease, error(confirmed=False))
    assert accounts.failure(lease, error("throttled"))
    accounts.release(lease)
    assert accounts.acquire("j", "w", "openai/model") == (None, 1030)
    assert accounts.list()[0]["id"] == one["id"]
    assert accounts.list()[0]["health"] == "healthy"


def test_late_old_generation_success_or_failure_cannot_clear_newer_quota(registry):
    accounts, _, _, _ = registry
    authenticated(accounts, "one")
    first, _ = accounts.acquire("j1", "w1", "openai/model")
    second, _ = accounts.acquire("j2", "w2", "openai/model")
    accounts.failure(first, error(reset_at=2000))
    second["probe"] = True
    accounts.success(second)
    accounts.failure(second, error(reset_at=3000))
    assert accounts.list()[0]["reset_at"] == 2000


@pytest.mark.parametrize("state", ["checking", "authorizing", "removing"])
def test_auth_mutations_exclude_dispatch_and_reject_live_lease(registry, state):
    accounts, _, _, _ = registry
    row = authenticated(accounts, "one")
    lease, _ = accounts.acquire("j", "w", "openai/model")
    with pytest.raises(ValueError):
        accounts.set_login_state(row["id"], state)
    with pytest.raises(ValueError):
        accounts.remove(row["id"])
    accounts.release(lease)
    accounts.set_login_state(row["id"], state)
    assert accounts.acquire("j", "w", "openai/model")[0] is None


def test_metadata_refresh_preserves_observed_quota(registry):
    accounts, _, _, _ = registry
    row = authenticated(accounts, "one")
    lease, _ = accounts.acquire("j", "w", "openai/model")
    accounts.failure(lease, error(reset_at=4000))
    accounts.release(lease)
    accounts.set_login_state(row["id"], "checking")
    accounts.record_login_result(row["id"], {"authenticated": True, "identity": "one"}, reset_cooldown=False)
    assert accounts.list()[0]["reset_at"] == 4000
    assert accounts.acquire("j", "w", "openai/model")[0] is None


class FakeRuntime:
    def __init__(self):
        self.stopped = False

    def close(self):
        pass

    def confirm_stopped(self, **kwargs):
        return self.stopped


@pytest.fixture
def scheduled(tmp_path, scheduler_harness):
    store, config = Store(tmp_path / "queue.db"), Config(tmp_path)
    config.data.update(chatgpt_accounts_enabled=True, chatgpt_account_concurrency=5, library_auto=False)
    runtime = FakeRuntime()
    scheduler = Scheduler(store, config, None, account_runtime=runtime, opencode=SimpleNamespace())
    text = tmp_path / "source.txt"
    text.write_text("نص", encoding="utf-8")

    def add(count=1):
        return store.add([{"video_id": str(i), "title": str(i), "position": i} for i in range(count)],
                         config.snapshot(), input_path=str(text))

    yield scheduler, store, config, runtime, add
    scheduler_harness.shutdown_and_join()
    store.db.close()


def drain(scheduler, timeout=3):
    deadline = time.monotonic() + timeout
    while not scheduler.idle() and time.monotonic() < deadline:
        time.sleep(.005)
    assert scheduler.idle()


def test_multiworker_three_account_failover_and_persistent_all_cooldown(scheduled):
    scheduler, store, _, _, add = scheduled
    rows = [authenticated(scheduler.accounts, str(i)) for i in range(3)]
    calls = []

    def process(job, worker, cfg, secret, cancel, log):
        calls.append(cfg["_chatgpt_account_id"])
        raise error(reset_at=time.time() + 600)

    scheduler.opencode.process = process
    ids = add(9)
    for _ in range(8):
        scheduler.tick()
        drain(scheduler)
    assert set(calls) == {row["id"] for row in rows}
    assert len(calls) <= 15
    assert all(store.get(ident)["stages"]["opencode"]["status"] == "pending" for ident in ids)
    assert all(store.get(ident)["stages"]["opencode"]["next_retry"] > time.time() for ident in ids)
    assert all(store.get(ident)["stages"]["opencode"]["retries"] == 0 for ident in ids)
    assert all(row["health"] == "cooldown" for row in OpenCodeAccounts(store, scheduler.config).list())


def test_uncertain_execution_blocks_manual_retry_delete_login_until_proven_stopped(scheduled):
    scheduler, store, _, runtime, add = scheduled
    row = authenticated(scheduler.accounts, "one")

    def process(job, worker, cfg, secret, cancel, log):
        cfg["_chatgpt_session_started"]("session", "instance", "C:/work")
        raise error("execution_uncertain")

    scheduler.opencode.process = process
    ident = add()[0]
    scheduler.tick()
    drain(scheduler)
    assert store.get(ident)["stages"]["opencode"]["status"] == "uncertain"
    with pytest.raises(ValueError):
        scheduler.retry_job(ident, "opencode")
    assert scheduler.delete_jobs([ident])["busy"] == [ident]
    with pytest.raises(ValueError):
        scheduler.accounts.set_login_state(row["id"], "authorizing")
    runtime.stopped = True
    scheduler.recheck_account(row["id"])
    scheduler.retry_job(ident, "opencode")
    assert store.get(ident)["stages"]["opencode"]["status"] == "pending"
    assert not store.state_get("chatgpt_session_holds")
    attempt = dict(store.db.execute("SELECT * FROM chatgpt_attempts").fetchone())
    assert attempt["session_id"] == "session" and attempt["outcome"] == "execution_uncertain"


def test_crash_recovery_preserves_native_session_hold(registry):
    accounts, store, config, _ = registry
    row = authenticated(accounts, "one")
    lease, _ = accounts.acquire("job", "worker", "openai/model")
    accounts.session_started(lease, "session", "instance", "C:/work")
    store.recover()
    restored = OpenCodeAccounts(store, config)
    assert restored.list()[0]["health"] == "execution_uncertain"
    assert store.state_get("chatgpt_session_holds")["job"]["session_id"] == "session"
    assert store.db.execute("SELECT outcome FROM chatgpt_attempts").fetchone()[0] == "interrupted"
    with pytest.raises(ValueError):
        restored.recheck(row["id"])


@pytest.mark.parametrize("managed_mode", [False, True])
def test_durable_replay_hold_blocks_store_and_dispatch_after_mode_change(scheduled, managed_mode):
    scheduler, store, config, _, add = scheduled
    held = authenticated(scheduler.accounts, "held")
    authenticated(scheduler.accounts, "available")
    ident = add()[0]
    lease, _ = scheduler.accounts.acquire(ident, "worker", "openai/model")
    scheduler.accounts.hold(lease, ident)
    scheduler.accounts.release(lease)
    with pytest.raises(ValueError):
        store.retry(ident, "opencode")
    with pytest.raises(ValueError):
        store.retry(ident, "deepgram", rerun=True)
    # Even an old caller restoring a pending status cannot bypass the hold.
    store.set_stage(ident, "opencode", "pending")
    assert not store.claim(ident, "opencode", "worker")
    config.data["chatgpt_accounts_enabled"] = managed_mode
    scheduler.opencode.process = lambda *args: pytest.fail("held job replayed")
    scheduler.tick()
    assert scheduler.idle() and not scheduler.accounts.leases
    assert store.get(ident)["stages"]["opencode"]["attempts"] == 0
    assert store.state_get("chatgpt_session_holds")[ident]["account_id"] == held["id"]


def test_queued_saved_model_controls_routing_not_live_worker(scheduled):
    scheduler, store, config, _, add = scheduled
    row = authenticated(scheduler.accounts, "one")
    ids = add()
    for worker in config.data["opencode_workers"]:
        worker.update(model="anthropic/changed", provider="openai")
    calls = []

    def process(job, worker, cfg, secret, cancel, log):
        calls.append((worker["model"], cfg.get("_chatgpt_account_id")))
        return job["folder"] + "/result.docx"

    scheduler.opencode.process = process
    scheduler.tick()
    drain(scheduler)
    assert calls == [("openai/gpt-6-astra", row["id"])]
    assert store.get(ids[0])["stages"]["opencode"]["status"] == "completed"


def test_no_account_dispatch_for_api_or_non_openai_or_legacy(scheduled):
    scheduler, _, config, _, _ = scheduled
    worker = config.snapshot()["opencode_workers"][0]
    assert scheduler.uses_accounts(worker, config.snapshot())
    assert not scheduler.uses_accounts({**worker, "provider": "openai"}, config.snapshot())
    assert not scheduler.uses_accounts({**worker, "model": "anthropic/model"}, config.snapshot())
    assert not scheduler.uses_accounts(worker, {**config.snapshot(), "chatgpt_accounts_enabled": False})


def test_runtime_shutdown_failure_is_visible_and_preserves_holds(scheduled):
    scheduler, store, _, runtime, _ = scheduled
    held = {"job": {"account_id": "account", "session_id": "session"}}
    store.state_put("chatgpt_session_holds", held)
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("token=SECRET")

    runtime.close = fail
    scheduler.shutdown()
    scheduler.shutdown()
    assert len(calls) == 2
    assert store.state_get("chatgpt_session_holds") == held
    events = store.events()
    assert events[0]["level"] == "error"
    assert "SECRET" not in json.dumps(events)


def test_recovery_preserves_known_quotas_and_checks_all_holds_before_publication(scheduled):
    scheduler, store, _, runtime, _ = scheduled
    row = authenticated(scheduler.accounts, "one")
    quota, _ = scheduler.accounts.acquire("quota", "w1", "openai/model")
    scheduler.accounts.failure(quota, error(reset_at=time.time() + 900))
    scheduler.accounts.release(quota)
    with store.lock:
        state = scheduler.accounts._get(row["id"])
        state["throttle_until"] = time.time() + 300
        state["models"] = {"openai/other": {"until": time.time() + 1200}}
        scheduler.accounts._put(state)
    expected = {key: state[key] for key in ("reset_at", "throttle_until", "models")}
    for job in ("j1", "j2"):
        scheduler.accounts.hold({"account_id": row["id"], "session_id": job}, job)
    checked = []

    def confirm(**held):
        checked.append(held["session_id"])
        assert len(store.state_get("chatgpt_session_holds")) == 2
        assert scheduler.accounts.list()[0]["health"] == "execution_uncertain"
        assert scheduler.accounts.acquire("new", "worker", "openai/model")[0] is None
        return True

    runtime.confirm_stopped = confirm
    recovered = scheduler.recheck_account(row["id"])
    assert checked == ["j1", "j2"]
    assert recovered["health"] == "cooldown"
    assert not store.state_get("chatgpt_session_holds")
    state = scheduler.accounts._get(row["id"])
    assert {key: state[key] for key in expected} == expected


def test_recovery_does_not_drop_any_hold_if_later_proof_fails(scheduled):
    scheduler, store, _, runtime, _ = scheduled
    row = authenticated(scheduler.accounts, "one")
    for job in ("j1", "j2"):
        scheduler.accounts.hold({"account_id": row["id"], "session_id": job}, job)
    runtime.confirm_stopped = lambda **held: held["session_id"] == "j1"
    with pytest.raises(ValueError):
        scheduler.recheck_account(row["id"])
    assert len(store.state_get("chatgpt_session_holds")) == 2
    assert scheduler.accounts.list()[0]["health"] == "execution_uncertain"


def test_acquire_database_failure_never_publishes_phantom_lease(registry, monkeypatch):
    accounts, store, _, _ = registry
    authenticated(accounts, "one")
    original = store.state_put

    def fail(key, value):
        if key == "chatgpt_current":
            raise sqlite3.OperationalError("disk full")
        original(key, value)

    monkeypatch.setattr(store, "state_put", fail)
    with pytest.raises(sqlite3.OperationalError):
        accounts.acquire("job", "worker", "openai/model")
    assert not accounts.leases
    assert store.db.execute("SELECT COUNT(*) FROM chatgpt_attempts").fetchone()[0] == 0


def test_claim_database_failure_releases_durable_account_lease(scheduled, monkeypatch):
    scheduler, store, _, _, add = scheduled
    authenticated(scheduler.accounts, "one")
    add()

    def fail(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "claim", fail)
    with pytest.raises(sqlite3.OperationalError):
        scheduler.tick()
    assert not scheduler.accounts.leases and not scheduler.active
    assert store.db.execute("SELECT outcome FROM chatgpt_attempts").fetchone()[0] == "claim_failed"
