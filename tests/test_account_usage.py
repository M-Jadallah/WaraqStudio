from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from waraq.account_usage import AccountUsageMonitor, safe_usage_result
from waraq.config import Config
from waraq.opencode_accounts import OpenCodeAccounts
from waraq.scheduler import Scheduler
from waraq.services import ServiceError
from waraq.store import Store


def success(now=1000, used=40):
    return {"status": "available", "reason_code": "usage_available", "source": "legacy_codex_usage",
            "observed_at": now,
            "five_hour": {"used_percent": used, "remaining_percent": 100 - used,
                          "window_minutes": 300, "resets_at": now + 1200},
            "weekly": {"used_percent": 90, "remaining_percent": 10,
                       "window_minutes": 10080, "resets_at": now + 6000}}


class ManualExecutor:
    def __init__(self):
        self.tasks = []
        self.closed = False

    def submit(self, function, *args):
        self.tasks.append((function, args))

    def run(self):
        function, args = self.tasks.pop(0)
        function(*args)

    def shutdown(self, **kwargs):
        self.closed = True
        self.tasks.clear()


@pytest.fixture
def service(tmp_path):
    store, config = Store(tmp_path / "queue.db"), Config(tmp_path)
    config.data["chatgpt_accounts_enabled"] = True
    runtime = SimpleNamespace(read_usage=lambda *args: success(), close=lambda: None)
    scheduler = Scheduler(store, config, None, account_runtime=runtime)
    scheduler.account_usage.stop()
    clock = [1000.0]
    scheduler.accounts.clock = lambda: clock[0]
    executor = ManualExecutor()
    monitor = AccountUsageMonitor(scheduler, clock=lambda: clock[0], executor=executor)
    scheduler.account_usage = monitor
    row = scheduler.accounts.create("one")
    scheduler.accounts.record_login_result(row["id"], {"authenticated": True, "identity": "one"})
    payloads = []
    monitor.set_callback(payloads.append)
    yield SimpleNamespace(store=store, config=config, scheduler=scheduler, accounts=scheduler.accounts,
                          runtime=runtime, monitor=monitor, clock=clock, executor=executor,
                          ident=row["id"], payloads=payloads)
    scheduler.shutdown()
    store.db.close()


def complete(s):
    s.executor.run()
    return s.monitor.apply(s.payloads.pop(0))


def test_usage_table_migrates_existing_database_and_auth_epoch(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE chatgpt_accounts(id TEXT PRIMARY KEY, metadata TEXT, updated REAL)")
    db.execute("INSERT INTO chatgpt_accounts VALUES(?,?,?)", ("old", json.dumps({"id": "old"}), 1))
    db.commit()
    db.close()
    store = Store(path)
    accounts = OpenCodeAccounts(store, Config(tmp_path))
    assert accounts.usage_binding("old")["auth_epoch"] == 0
    assert json.loads(store.db.execute("SELECT metadata FROM chatgpt_accounts").fetchone()[0])["auth_epoch"] == 0
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(chatgpt_usage)")}
    assert columns == {"account_id", "auth_epoch", "request_id", "snapshot", "last_success_at",
                       "last_attempt_at", "next_attempt_at", "status", "reason_code"}
    store.db.close()


def test_callback_result_is_not_committed_until_owner_apply(service):
    s = service
    assert s.monitor.request(s.ident)
    s.executor.run()
    assert s.accounts.usage_record(s.ident)["last_success_at"] == 0
    assert s.monitor.state(s.ident)["refreshing"]
    assert s.monitor.apply(s.payloads[0])
    assert not s.monitor.state(s.ident)["refreshing"]
    public = s.monitor.public_snapshot(s.ident)
    assert public["fiveHour"]["remainingPercent"] == 60
    assert public["weekly"]["remainingPercent"] == 10
    assert not public["stale"]


def test_unknown_and_zero_are_distinct_and_overrun_is_zero(service):
    s = service
    s.runtime.read_usage = lambda *args: {**success(used=400), "weekly": None, "status": "partial"}
    assert s.monitor.request(s.ident)
    assert complete(s)
    public = s.monitor.public_snapshot(s.ident)
    assert public["fiveHour"]["remainingPercent"] == 0
    assert public["weekly"] == {"status": "unavailable", "remainingPercent": None,
                                "resetsAt": None, "windowMinutes": None}
    assert public["status"] == "partial"


def test_failures_retain_stale_success_without_mutating_routing(service):
    s = service
    before = s.accounts._get(s.ident).copy()
    assert s.monitor.request(s.ident) and complete(s)
    s.clock[0] += 31
    s.runtime.read_usage = lambda *args: {"status": "auth_unavailable", "reason_code": "unauthorized"}
    assert s.monitor.request(s.ident, manual=True) and complete(s)
    public = s.monitor.public_snapshot(s.ident)
    assert public["stale"] and public["fiveHour"]["remainingPercent"] == 60
    assert public["messageCode"] == "unauthorized"
    assert s.accounts._get(s.ident) == before
    assert not s.accounts.leases


def test_old_callbacks_cannot_clear_new_spinner_or_restore_deleted_account(service):
    s = service
    assert s.monitor.request(s.ident)
    s.executor.run()
    old = s.payloads.pop()
    s.monitor.invalidate(s.ident)
    s.accounts.set_login_state(s.ident, "authorizing")
    s.accounts.record_login_result(s.ident, {"authenticated": True, "identity": "two"})
    assert s.monitor.request(s.ident)
    assert not s.monitor.apply(old)
    assert s.monitor.state(s.ident)["refreshing"]
    s.executor.run()
    newer = s.payloads.pop()
    s.accounts.remove(s.ident)
    assert not s.monitor.apply(newer)
    assert s.accounts.usage_record(s.ident) is None
    assert not s.accounts.list()


def test_out_of_order_registry_requests_rejected_without_snapshot_overwrite(service):
    s = service
    binding = s.accounts.usage_binding(s.ident)
    assert s.accounts.begin_usage_request(s.ident, binding, "old")
    assert s.accounts.begin_usage_request(s.ident, binding, "new")
    assert s.accounts.record_usage(s.ident, binding, "new", success(used=99))
    assert not s.accounts.record_usage(s.ident, binding, "old", success(used=1))
    assert s.accounts.usage_record(s.ident)["snapshot"]["five_hour"]["remaining_percent"] == 1


def test_routing_generation_does_not_invalidate_usage_identity(service):
    s = service
    binding = s.accounts.usage_binding(s.ident)
    assert s.monitor.request(s.ident)
    s.accounts.recheck(s.ident)
    assert s.accounts.usage_binding(s.ident) == binding
    lease, _ = s.accounts.acquire("job", "worker", "openai/model")
    error = ServiceError("quota", "rate_limited")
    error.confirmed, error.reset_at, error.scope = True, 2000, "account"
    assert s.accounts.failure(lease, error)
    assert s.accounts.usage_binding(s.ident) == binding
    assert complete(s)
    s.accounts.release(lease)
    assert s.monitor.public_snapshot(s.ident)["fiveHour"]["remainingPercent"] == 60


def test_remove_cleans_usage_atomically_even_if_preference_cleanup_fails(service, monkeypatch):
    s = service
    assert s.monitor.request(s.ident) and complete(s)
    s.accounts.prefer(s.ident)
    monkeypatch.setattr(s.store, "state_clear", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk")))
    with pytest.raises(sqlite3.OperationalError):
        s.accounts.remove(s.ident)
    assert s.accounts.usage_record(s.ident) is not None
    assert s.accounts.usage_binding(s.ident)


def test_manual_floor_coalescing_interval_and_auto_disabled(service):
    s = service
    assert s.monitor.schedule_due() == 1
    assert not s.monitor.request(s.ident, manual=True)
    assert complete(s)
    s.clock[0] = 1029
    assert not s.monitor.request(s.ident, manual=True)
    s.clock[0] = 1030
    assert s.monitor.request(s.ident, manual=True) and complete(s)
    s.clock[0] = 1300
    assert s.monitor.schedule_due() == 0
    s.clock[0] = 1330
    s.config.data["chatgpt_usage_auto_refresh"] = False
    assert s.monitor.schedule_due() == 0
    assert s.monitor.request_all() == 1


def test_retry_backoff_and_retry_after_survive_manual_refresh_and_restart(service):
    s = service
    s.runtime.read_usage = lambda *args: {"status": "unavailable", "reason_code": "throttled", "retry_after": 500}
    assert s.monitor.request(s.ident) and complete(s)
    assert s.monitor.state(s.ident)["nextAttemptAt"] == 1500
    s.clock[0] = 1499
    assert not s.monitor.request(s.ident, manual=True)
    fresh = AccountUsageMonitor(s.scheduler, clock=lambda: s.clock[0], executor=ManualExecutor())
    fresh.set_callback(s.payloads.append)
    assert not fresh.request(s.ident, manual=True)
    fresh.stop()
    s.clock[0] = 1500
    s.runtime.read_usage = lambda *args: {"status": "unavailable", "reason_code": "timeout"}
    for delay in (120, 300, 600, 900, 900):
        assert s.monitor.request(s.ident) and complete(s)
        assert s.monitor.state(s.ident)["nextAttemptAt"] == s.clock[0] + delay
        s.clock[0] += delay


def test_disabled_for_jobs_is_eligible_but_auth_mutating_and_unsupported_stop_auto(service):
    s = service
    s.accounts.update(s.ident, enabled=False)
    s.runtime.read_usage = lambda *args: {"status": "unsupported", "reason_code": "token_sharing_unsupported"}
    assert s.monitor.request(s.ident) and complete(s)
    s.clock[0] += 1000
    assert not s.monitor.request(s.ident)
    assert not s.monitor.request(s.ident, manual=True)
    assert s.monitor.public_snapshot(s.ident)["refreshBlockReason"] == "usage_unsupported"
    s.config.data["opencode_executable"] = "different-opencode"
    assert s.monitor.request(s.ident, manual=True) and complete(s)
    s.accounts.set_login_state(s.ident, "checking")
    s.clock[0] += 1000
    assert not s.monitor.request(s.ident, manual=True)


def test_cached_snapshot_stale_on_startup_ttl_and_reset_without_faking_renewal(service):
    s = service
    assert s.monitor.request(s.ident) and complete(s)
    fresh = AccountUsageMonitor(s.scheduler, clock=lambda: s.clock[0], executor=ManualExecutor())
    assert fresh.public_snapshot(s.ident)["stale"]
    fresh.stop()
    assert s.monitor.public_snapshot(s.ident, now=1600)["stale"]
    s.config.data["chatgpt_usage_refresh_seconds"] = 1800
    public = s.monitor.public_snapshot(s.ident, now=2200)
    assert public["stale"] and public["fiveHour"]["remainingPercent"] == 60


def test_manual_success_clears_previous_unsupported_and_restores_auto(service):
    s = service
    s.runtime.read_usage = lambda *args: {"status": "unsupported", "reason_code": "endpoint_unavailable"}
    assert s.monitor.request(s.ident) and complete(s)
    assert not s.monitor.request(s.ident)
    s.clock[0] += 60
    assert s.monitor.public_snapshot(s.ident)["canRefresh"]
    s.runtime.read_usage = lambda *args: success(s.clock[0])
    assert s.monitor.request(s.ident, manual=True) and complete(s)
    assert s.ident not in s.monitor.unsupported
    s.clock[0] += 300
    assert s.monitor.schedule_due() == 1


@pytest.mark.parametrize("reason", ["token_sharing_unsupported", "method_unsupported", "native_version_unverified", "usage_runtime_unsupported"])
def test_unsupported_capability_blocks_manual_until_runtime_version_changes(service, reason):
    s = service
    capability = ["2.0.27"]
    s.runtime.usage_capability_key = lambda ident: tuple(capability)
    s.runtime.read_usage = lambda *args: {"status": "unsupported", "reason_code": reason}
    binding = s.accounts.usage_binding(s.ident)
    assert s.monitor.request(s.ident) and complete(s)
    s.clock[0] += 1000
    assert not s.monitor.request(s.ident, manual=True)
    public = s.monitor.public_snapshot(s.ident)
    assert not public["canRefresh"] and public["refreshWaitSeconds"] == 0
    assert public["refreshBlockReason"] == "usage_unsupported"
    capability[0] = "2.0.28"
    public = s.monitor.public_snapshot(s.ident)
    assert public["canRefresh"] and public["refreshBlockReason"] == ""
    assert s.accounts.usage_binding(s.ident) == binding
    s.runtime.read_usage = lambda *args: success(s.clock[0])
    assert s.monitor.request(s.ident, manual=True) and complete(s)
    s.clock[0] += 300
    assert s.monitor.request(s.ident)


def test_capability_change_keeps_floor_and_retry_after_and_snapshot_launches_no_work(service):
    s = service
    s.runtime.read_usage = lambda *args: {"status": "unsupported", "reason_code": "native_version_unverified", "retry_after": 120}
    assert s.monitor.request(s.ident) and complete(s)
    s.config.data["opencode_executable"] = "new-opencode"
    public = s.monitor.public_snapshot(s.ident)
    assert not public["canRefresh"] and public["refreshWaitSeconds"] == 120
    assert public["refreshBlockReason"] == "refresh_wait"
    assert not s.executor.tasks
    assert not s.monitor.request(s.ident, manual=True)
    s.clock[0] += 119.25
    assert s.monitor.public_snapshot(s.ident)["refreshWaitSeconds"] == 1
    s.clock[0] += .75
    assert s.monitor.public_snapshot(s.ident)["canRefresh"]
    assert not s.executor.tasks


def test_cold_runtime_creation_does_not_invalidate_unsupported_cache(service):
    s = service
    capability = [None]
    s.runtime.usage_capability_key = lambda ident: capability[0]
    def read(*args):
        capability[0] = "runtime-version-2.0.27"
        return {"status": "unsupported", "reason_code": "token_sharing_unsupported"}
    s.runtime.read_usage = read
    assert s.monitor.request(s.ident) and complete(s)
    s.clock[0] += 1000
    assert not s.monitor.public_snapshot(s.ident)["canRefresh"]
    assert not s.monitor.request(s.ident)


def test_capability_read_failure_is_not_actionable_or_a_changed_unsupported_key(service):
    s = service
    capability, failed = ["known-version"], [False]
    def key(ident):
        if failed[0]:
            raise ValueError("PRIVATE_LOCAL_FAILURE")
        return capability[0]
    s.runtime.usage_capability_key = key
    s.runtime.read_usage = lambda *args: {"status": "unsupported", "reason_code": "token_sharing_unsupported"}
    assert s.monitor.request(s.ident) and complete(s)
    before = s.monitor.unsupported[s.ident].copy()
    s.clock[0] += 1000
    failed[0] = True
    public = s.monitor.public_snapshot(s.ident)
    assert not public["canRefresh"] and public["refreshBlockReason"] == "usage_unavailable"
    assert not s.monitor.request(s.ident, manual=True)
    assert s.monitor.unsupported[s.ident] == before and not s.executor.tasks
    assert "PRIVATE_LOCAL_FAILURE" not in json.dumps(public)
    failed[0] = False
    assert s.monitor.public_snapshot(s.ident)["refreshBlockReason"] == "usage_unsupported"
    assert not s.monitor.request(s.ident, manual=True)
    capability[0] = "known-changed-version"
    assert s.monitor.public_snapshot(s.ident)["canRefresh"]
    assert s.monitor.request(s.ident, manual=True)


@pytest.mark.parametrize("status", ["available", "unsupported"])
def test_capability_read_failure_after_usage_clears_pending_and_preserves_start_key(service, status):
    s = service
    failed = [False]
    def key(ident):
        if failed[0]:
            raise OSError("PRIVATE_LOCAL_FAILURE")
        return "stable-capability"
    def read(*args):
        failed[0] = True
        return success() if status == "available" else {"status": "unsupported", "reason_code": "token_sharing_unsupported"}
    s.runtime.usage_capability_key, s.runtime.read_usage = key, read
    assert s.monitor.request(s.ident)
    assert complete(s)
    assert not s.monitor.state(s.ident)["refreshing"]
    public = s.monitor.public_snapshot(s.ident)
    assert public["status"] == status and not public["canRefresh"]
    assert public["refreshBlockReason"] == "usage_unavailable"
    assert "PRIVATE_LOCAL_FAILURE" not in json.dumps(s.accounts.usage_record(s.ident))
    failed[0] = False
    s.clock[0] += 1000
    if status == "available":
        assert s.monitor.public_snapshot(s.ident)["canRefresh"]
    else:
        assert s.monitor.public_snapshot(s.ident)["refreshBlockReason"] == "usage_unsupported"
        assert not s.monitor.request(s.ident, manual=True)


def test_refresh_projection_matches_requests_without_jobs_or_background_activity(service):
    s = service
    assert not s.store.jobs()
    public = s.monitor.public_snapshot(s.ident)
    assert public["canRefresh"] and public["refreshBlockReason"] == "" and public["refreshWaitSeconds"] == 0
    assert not s.executor.tasks
    assert s.monitor.request(s.ident, manual=True)
    public = s.monitor.public_snapshot(s.ident)
    assert not public["canRefresh"] and public["refreshBlockReason"] == "refreshing"
    assert complete(s)
    public = s.monitor.public_snapshot(s.ident)
    assert not public["canRefresh"] and public["refreshWaitSeconds"] == 30
    s.clock[0] += 30
    assert s.monitor.public_snapshot(s.ident)["canRefresh"]
    assert not s.monitor.request(s.ident)  # Ordinary interval still applies.
    assert s.monitor.request(s.ident, manual=True)


@pytest.mark.parametrize("change,reason", [
    ("managed_disabled", "managed_accounts_disabled"), ("callback_missing", "usage_unavailable"),
    ("auth_missing", "account_not_ready"), ("uncertain", "account_not_ready"),
    ("stopped", "usage_closed"), ("missing_account", "account_not_ready")])
def test_refresh_projection_reasons_match_nonactionable_request(service, change, reason):
    s = service
    if change == "managed_disabled":
        s.config.data["chatgpt_accounts_enabled"] = False
    elif change == "callback_missing":
        s.monitor.set_callback(None)
    elif change == "auth_missing":
        s.accounts.set_login_state(s.ident, "signed_out")
    elif change == "uncertain":
        row = s.accounts._get(s.ident)
        row["health"] = "execution_uncertain"
        s.accounts._put(row)
    elif change == "stopped":
        s.monitor.stop()
    else:
        s.accounts.remove(s.ident)
    public = s.monitor.public_snapshot(s.ident)
    assert not public["canRefresh"] and public["refreshBlockReason"] == reason
    assert public["refreshWaitSeconds"] == 0
    assert not s.monitor.request(s.ident, manual=True)
    assert not s.executor.tasks


def test_auto_disabled_and_account_disabled_for_jobs_do_not_block_manual_refresh(service):
    s = service
    s.config.data["chatgpt_usage_auto_refresh"] = False
    s.accounts.update(s.ident, enabled=False)
    assert s.monitor.public_snapshot(s.ident)["canRefresh"]
    assert not s.monitor.request(s.ident)
    assert s.monitor.request(s.ident, manual=True)


def test_refresh_projection_uses_monotonic_countdown_and_persisted_backoff(service):
    s = service
    monotonic = [10.0]
    s.monitor.monotonic = lambda: monotonic[0]
    s.runtime.read_usage = lambda *args: {"status": "unavailable", "reason_code": "timeout"}
    assert s.monitor.request(s.ident) and complete(s)
    assert s.monitor.public_snapshot(s.ident)["refreshWaitSeconds"] == 60
    s.clock[0] -= 86400
    monotonic[0] += 59.5
    assert s.monitor.public_snapshot(s.ident)["refreshWaitSeconds"] == 1
    monotonic[0] += .5
    assert s.monitor.public_snapshot(s.ident)["canRefresh"]
    fresh = AccountUsageMonitor(s.scheduler, clock=lambda: s.clock[0], executor=ManualExecutor(),
                                monotonic=lambda: monotonic[0])
    fresh.set_callback(s.payloads.append)
    try:
        public = fresh.public_snapshot(s.ident)
        assert not public["canRefresh"] and public["refreshWaitSeconds"] == 60
        assert not fresh.request(s.ident, manual=True)
    finally:
        fresh.stop()


def test_no_callback_no_network_and_shutdown_discards_queued_delivery(service):
    s = service
    s.monitor.set_callback(None)
    assert not s.monitor.request(s.ident, manual=True)
    assert s.monitor.schedule_due() == 0
    s.monitor.set_callback(s.payloads.append)
    assert s.monitor.request(s.ident)
    s.executor.run()
    payload = s.payloads.pop()
    s.scheduler.shutdown()
    assert not s.monitor.apply(payload)
    assert not s.monitor.request(s.ident, manual=True)


def test_quota_lease_published_before_session_prevents_native_restart(service):
    s = service
    lease, _ = s.accounts.acquire("queued", "worker", "openai/model")
    assert "session_id" not in lease
    s.runtime.read_usage = lambda *args: {"status": "deferred", "reason_code": "expired_pending_idle"}
    s.runtime.restart_idle_for_usage = lambda *args: pytest.fail("queued lease restarted")
    result = s.scheduler.refresh_account_usage(s.ident, s.accounts.usage_binding(s.ident), threading.Event())
    assert result["status"] == "deferred"
    assert not s.accounts.usage_maintenance
    s.accounts.release(lease)


@pytest.mark.parametrize("initial_reason", ["expired_pending_idle", "runtime_pending_idle"])
def test_maintenance_preserves_sticky_route_and_releases_after_failure(service, initial_reason):
    s = service
    second = s.accounts.create("second")
    s.accounts.record_login_result(second["id"], {"authenticated": True, "identity": "second"})
    s.accounts.prefer(s.ident)
    s.runtime.read_usage = lambda *args: {"status": "deferred", "reason_code": initial_reason}

    def restart(ident, reservation, cancel):
        assert reservation["is_valid"]()
        assert s.accounts.acquire("pending", "w", "openai/model")[0] is None
        raise RuntimeError("SECRET")

    s.runtime.restart_idle_for_usage = restart
    assert s.monitor.request(s.ident) and complete(s)
    assert not s.accounts.usage_maintenance
    assert s.accounts.acquire("job", "worker", "openai/model")[0]["account_id"] == s.ident
    assert "SECRET" not in json.dumps(s.accounts.usage_record(s.ident))
    for lease in list(s.accounts.leases.values()):
        s.accounts.release(lease)


def test_shutdown_during_maintenance_invalidates_reservation_and_never_rereads(service):
    s = service
    reads = []

    def read(*args):
        reads.append(True)
        return {"status": "deferred", "reason_code": "expired_pending_idle"}

    def restart(ident, reservation, cancel):
        s.scheduler.shutdown()
        assert not reservation["is_valid"]()
        return {"status": "partial", "reason_code": "native_refreshed"}

    s.runtime.read_usage, s.runtime.restart_idle_for_usage = read, restart
    result = s.scheduler.refresh_account_usage(s.ident, s.accounts.usage_binding(s.ident), threading.Event())
    assert result["status"] == "cancelled" and len(reads) == 1
    assert not s.accounts.usage_maintenance


def test_valid_token_reads_are_allowed_during_jobs_without_native_restart(service):
    s = service
    lease, _ = s.accounts.acquire("job", "worker", "openai/model")
    s.runtime.restart_idle_for_usage = lambda *args: pytest.fail("valid token restarted")
    assert s.monitor.request(s.ident) and complete(s)
    assert s.accounts.leases[lease["id"]] == lease
    s.accounts.release(lease)


def test_maintenance_state_prevents_auth_mutation_without_discarding_cache(service):
    s = service
    assert s.monitor.request(s.ident) and complete(s)
    binding = s.accounts.usage_binding(s.ident)
    before = s.accounts.usage_record(s.ident)
    reservation = s.accounts.reserve_usage_maintenance(s.ident, binding)
    assert s.monitor.state(s.ident)["maintenance"]
    try:
        with pytest.raises(ValueError):
            s.accounts.set_login_state(s.ident, "authorizing")
        assert s.accounts.usage_binding(s.ident) == binding
        assert s.accounts.usage_record(s.ident) == before
    finally:
        s.accounts.release_usage_maintenance(reservation)
    assert not s.monitor.state(s.ident)["maintenance"]
    s.accounts.set_login_state(s.ident, "authorizing")
    assert s.accounts.usage_record(s.ident) is None


def test_native_refresh_preserves_epoch_but_signout_and_identity_change_invalidate(service):
    s = service
    assert s.monitor.request(s.ident) and complete(s)
    binding = s.accounts.usage_binding(s.ident)
    s.accounts.record_login_result(s.ident, {"authenticated": True, "identity": "one"}, reset_cooldown=False)
    assert s.accounts.usage_binding(s.ident) == binding
    assert s.accounts.usage_record(s.ident)["last_success_at"] == 1000
    s.accounts.set_login_state(s.ident, "signed_out")
    assert s.accounts.usage_binding(s.ident)["auth_epoch"] > binding["auth_epoch"]
    assert s.accounts.usage_record(s.ident) is None
    s.accounts.record_login_result(s.ident, {"authenticated": True, "identity": "replacement"})
    assert s.monitor.public_snapshot(s.ident)["fiveHour"]["remainingPercent"] is None


def test_public_projection_rejects_epoch_mismatch_and_marks_unauthenticated_cache_stale(service):
    s = service
    assert s.monitor.request(s.ident) and complete(s)
    s.accounts.set_login_state(s.ident, "checking")
    assert s.monitor.public_snapshot(s.ident)["stale"]
    row = s.accounts._get(s.ident)
    row["auth_epoch"] += 1
    s.accounts._put(row)
    assert s.monitor.public_snapshot(s.ident)["fiveHour"]["remainingPercent"] is None


def test_poll_cadence_uses_monotonic_deadlines_when_wallclock_moves_backwards(service):
    s = service
    monotonic = [10.0]
    s.monitor.monotonic = lambda: monotonic[0]
    assert s.monitor.request(s.ident) and complete(s)
    s.clock[0] -= 86400
    monotonic[0] += 299
    assert not s.monitor.request(s.ident)
    monotonic[0] += 1
    assert s.monitor.request(s.ident)
    assert complete(s)


def test_restart_after_wallclock_rollback_retains_delay_without_waiting_days(service):
    s = service
    s.runtime.read_usage = lambda *args: {"status": "unavailable", "reason_code": "throttled", "retry_after": 500}
    assert s.monitor.request(s.ident) and complete(s)
    s.clock[0] -= 86400
    monotonic = [10.0]
    fresh = AccountUsageMonitor(s.scheduler, clock=lambda: s.clock[0], executor=ManualExecutor(),
                                monotonic=lambda: monotonic[0])
    fresh.set_callback(s.payloads.append)
    assert not fresh.request(s.ident, manual=True)
    monotonic[0] += 499
    assert not fresh.request(s.ident, manual=True)
    monotonic[0] += 1
    assert fresh.request(s.ident, manual=True)
    fresh.stop()


def test_sanitization_closed_reasons_windows_and_bounded_buckets():
    raw = success(used=400)
    raw.update(reason_code="sensitive_private_token", access_token="SECRET", source="https://secret.test",
               buckets=[{**success(), "label": "SECRET"}] * 30)
    result = safe_usage_result(raw)
    assert result["reason_code"] == "usage_unavailable" and result["source"] == ""
    assert len(result["buckets"]) == 8
    assert result["five_hour"]["remaining_percent"] == 0
    assert "SECRET" not in json.dumps(result) and "sensitive_private_token" not in json.dumps(result)
    assert safe_usage_result({**success(), "five_hour": {"used_percent": True}})["five_hour"] is None
    assert safe_usage_result({"status": []})["status"] == "unavailable"


def test_executor_has_two_workers_and_coalesces_accounts(service):
    s = service
    s.monitor.stop()
    monitor = AccountUsageMonitor(s.scheduler, clock=lambda: s.clock[0])
    s.scheduler.account_usage = monitor
    gate, two_started = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls, concurrent, maximum = [], [0], [0]
    delivered = threading.Condition()
    payloads = []

    def read(ident, binding, cancel):
        with lock:
            calls.append(ident)
            concurrent[0] += 1
            maximum[0] = max(maximum[0], concurrent[0])
            if len(calls) == 2:
                two_started.set()
        assert gate.wait(3)
        with lock:
            concurrent[0] -= 1
        return success()

    def callback(payload):
        with delivered:
            payloads.append(payload)
            delivered.notify_all()

    s.runtime.read_usage = read
    monitor.set_callback(callback)
    ids = [s.ident]
    for name in ("two", "three"):
        row = s.accounts.create(name)
        s.accounts.record_login_result(row["id"], {"authenticated": True, "identity": name})
        ids.append(row["id"])
    try:
        assert monitor.request_all() == 3
        assert two_started.wait(3)
        assert len(calls) == 2 and maximum[0] == 2
        assert not monitor.request(ids[0], manual=True)
        gate.set()
        with delivered:
            assert delivered.wait_for(lambda: len(payloads) == 3, 3)
        assert maximum[0] == 2
        assert all(monitor.apply(payload) for payload in payloads)
    finally:
        gate.set()
        monitor.stop()
