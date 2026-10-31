"""Client boundary tests: no credentials, provider HTTP, or GUI are involved."""
import json
import threading
from types import SimpleNamespace

import pytest

from waraq.embedded_accounts import EmbeddedAccountService, AccountEngineError, AccountUnavailableError
from waraq.embedded_runtime import EmbeddedRuntimeError


def row(**extra):
    return {"accountRef": "account_1", "alias": "One", "enabled": True, "priority": 1, "preferred": False,
            "email": "test@example.test", "plan": "plus", "loginState": "ready", "health": "ready", "bindingVersion": "binding_1", **extra}


class Runtime:
    instance_id = "runtime_1"
    def __init__(self):
        self.calls = []
        self.rows = [row()]
        self.fail = None
        self.response = None

    def rpc(self, method, params, directory=None):
        self.calls.append((method, params, directory))
        if self.fail:
            raise self.fail
        if self.response:
            return self.response(method, params)
        if method == "accounts.list":
            return {"ok": True, "data": {"accounts": self.rows}}
        if method == "health":
            return {"ok": True, "data": {"apiVersion": 1, "authMethods": [{"id": "browser", "label": "Browser"}, {"id": "device", "label": "Device"}, {"id": "token", "label": "Token"}]}}
        if method == "attempt.begin":
            return {"ok": True, "data": {"attemptId": params["attemptId"], "accountRef": "account_1", "alias": "One"}}
        return {"ok": True, "data": {"attemptId": params.get("attemptId"), "accountRef": "account_1", "state": "closed", "events": []}}


@pytest.fixture
def service(tmp_path, monkeypatch):
    runtime = Runtime()
    manager = SimpleNamespace(ensure=lambda *args, **kwargs: runtime, close=lambda: None)
    config = SimpleNamespace(home=tmp_path, snapshot=lambda: {"accounts_max_concurrency": 3})
    # Permission behavior is covered separately in runtime tests.
    monkeypatch.setattr("waraq.embedded_accounts.private_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    client = EmbeddedAccountService(config, manager, clock=lambda: 1000)
    client.fake_runtime = runtime
    return client


def test_list_and_readiness_are_cached_and_strip_all_unapproved_fields(service):
    service.fake_runtime.rows = [row(access="SECRET", refresh="SECRET", quota={"status": "available", "fiveHour": {"usedPercent": 25, "resetsAt": 1100, "windowMinutes": 300}})]
    assert service.list() == []
    assert not service.readiness()["ready"]
    assert not service.fake_runtime.calls
    service.refresh()
    public = service.list()
    assert "SECRET" not in json.dumps(public)
    assert public[0]["usage"]["fiveHour"]["remainingPercent"] == 75
    public[0]["alias"] = "modified"
    calls = len(service.fake_runtime.calls)
    assert service.list()[0]["alias"] == "One"
    assert service.readiness()["ready"]
    assert len(service.fake_runtime.calls) == calls


def test_transport_failure_preserves_success_and_never_becomes_auth_failure(service):
    service.fake_runtime.rows = [row(quota={"fiveHour": {"remainingPercent": 40}, "lastSuccessAt": 900})]
    service.refresh()
    service.fake_runtime.fail = EmbeddedRuntimeError("engine_connection_failed")
    with pytest.raises(AccountEngineError) as error:
        service.refresh()
    assert error.value.kind == "runtime_unavailable"
    assert service.status["state"] == "unavailable"
    assert service.list()[0]["login_state"] == "authenticated"
    assert service.list()[0]["usage"]["fiveHour"]["remainingPercent"] == 40
    assert service.list()[0]["usage"]["stale"]
    assert len(service.fake_runtime.calls) == 3  # exactly one bounded read retry


def test_usage_transient_retention_requires_exact_binding(service):
    service.fake_runtime.rows = [row(quota={"fiveHour": {"remainingPercent": 40}, "lastSuccessAt": 900})]
    service.refresh()
    service.fake_runtime.rows = [row(quota={"status": "unavailable"})]
    service.refresh()
    assert service.list()[0]["usage"]["fiveHour"]["remainingPercent"] == 40
    service.fake_runtime.rows = [row(bindingVersion="binding_2", quota={"status": "unavailable"})]
    service.refresh()
    assert service.list()[0]["usage"]["fiveHour"] is None


def test_begin_attempt_routes_exact_directory_and_configured_limit(service, tmp_path):
    service.refresh()
    lease = service.begin_attempt(12, "oc1", "openai/gpt-6-astra", tmp_path / "attempt")
    method, params, directory = service.fake_runtime.calls[-1]
    assert method == "attempt.begin" and params["maxConcurrency"] == 3
    assert params["jobId"] == "12" and directory == str((tmp_path / "attempt").resolve())
    assert lease["account_id"] == "account_1" and lease["runtime_instance_id"] == "runtime_1"
    assert lease["attempt_id"] == params["attemptId"]
    assert service.list()[0]["in_flight"] == 1


@pytest.mark.parametrize("code,kind", [("NO_ELIGIBLE_ACCOUNT", "no_available_account"), ("CONCURRENCY_LIMIT", "accounts_busy"), ("LOCATION_BUSY", "accounts_busy")])
def test_selection_errors_are_typed_and_not_retried(service, tmp_path, code, kind):
    service.fake_runtime.response = lambda method, params: {"ok": False, "error": {"code": code, "retryAt": 1100, "message": "SECRET"}}
    with pytest.raises(AccountUnavailableError) as error:
        service.begin_attempt(1, "oc1", "model", tmp_path)
    assert error.value.kind == kind and error.value.retry_at == 1100
    assert "SECRET" not in str(error.value)
    assert len(service.fake_runtime.calls) == 1


def test_auth_methods_reject_manual_token_entry(service):
    assert [item["id"] for item in service.auth_methods("account_1")] == ["browser", "device"]


def test_transfer_forwards_exact_password_and_exposes_only_allowlisted_summary(service, tmp_path):
    password = "  كلمة أمان عربية 🔒  "
    def response(method, params):
        if method == "accounts.list":
            return {"ok": True, "data": {"accounts": [row(canExport=True, refreshToken="SECRET")]}}
        assert params["password"] == password
        return {"ok": True, "data": {"exported": 1, "imported": 1, "skipped": 0, "total": 1, "password": password, "refreshToken": "SECRET"}}
    service.fake_runtime.response = response
    service.refresh()
    assert service.list()[0]["can_export"] is True
    assert service.export_accounts(["account_1"], tmp_path / "backup.waraq-accounts", password) == {"exported": 1}
    assert service.import_accounts(tmp_path / "backup.waraq-accounts", password) == {"imported": 1, "skipped": 0, "total": 1}
    assert not service._transfer_active
    assert "SECRET" not in json.dumps(service.list())
    assert password not in json.dumps(service.status, ensure_ascii=False)


@pytest.mark.parametrize("password", ["", "short", "a" * 257, "a" * 15 + "\ud800"])
def test_invalid_transfer_password_never_reaches_engine(service, tmp_path, password):
    with pytest.raises(AccountEngineError, match="TRANSFER_PASSWORD_INVALID"):
        service.import_accounts(tmp_path / "backup.waraq-accounts", password)
    assert not service.fake_runtime.calls


@pytest.mark.parametrize("ids", [[], ["account_1", "account_1"], [["nested"]], ["bad/reference"]])
def test_invalid_export_selection_never_reaches_engine(service, tmp_path, ids):
    with pytest.raises(AccountEngineError):
        service.export_accounts(ids, tmp_path / "backup.waraq-accounts", "password-12345")
    assert not service.fake_runtime.calls


def test_transfer_busy_and_failure_release_reservations_without_retries(service, tmp_path):
    path = tmp_path / "backup.waraq-accounts"
    for collection in (service._auth, service._attempts):
        collection["existing"] = "busy"
        with pytest.raises(AccountEngineError, match="TRANSFER_BUSY"):
            service.import_accounts(path, "password-12345")
        collection.clear()
    service._usage_lock.acquire()
    try:
        with pytest.raises(AccountEngineError, match="TRANSFER_BUSY"):
            service.import_accounts(path, "password-12345")
    finally:
        service._usage_lock.release()
    assert not service.fake_runtime.calls
    service.fake_runtime.response = lambda *_: {"ok": False, "error": {"code": "TRANSFER_DECRYPT_FAILED", "message": "SECRET"}}
    with pytest.raises(AccountEngineError, match="TRANSFER_DECRYPT_FAILED") as failure:
        service.import_accounts(path, "password-12345")
    assert "SECRET" not in str(failure.value)
    assert len(service.fake_runtime.calls) == 1 and not service._transfer_active
    assert service._usage_lock.acquire(blocking=False)
    service._usage_lock.release()


def test_transfer_blocks_execution_and_quota_until_it_finishes(service, tmp_path):
    entered, release = threading.Event(), threading.Event()
    def response(method, params):
        entered.set()
        assert release.wait(3)
        return {"ok": True, "data": {"exported": 1}}
    service.fake_runtime.response = response
    errors = []
    def run():
        try:
            service.export_accounts(["account_1"], tmp_path / "backup.waraq-accounts", "password-12345")
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2)
        assert service.readiness()["reason"] == "accounts_busy"
        assert not service.request_refresh(force=True)
        with pytest.raises(AccountUnavailableError):
            service.begin_attempt(1, "oc1", "model", tmp_path / "task")
        with pytest.raises(AccountEngineError, match="TRANSFER_BUSY"):
            service._call("usage.refresh")
    finally:
        release.set()
        worker.join(3)
    assert not errors and not worker.is_alive() and not service._transfer_active


def test_committed_import_is_acknowledged_when_followup_refresh_fails(service, tmp_path):
    def response(method, params):
        if method == "accounts.import":
            return {"ok": True, "data": {"imported": 1, "skipped": 1, "total": 2}}
        raise EmbeddedRuntimeError("engine_connection_failed")
    service.fake_runtime.response = response
    assert service.import_accounts(tmp_path / "backup.waraq-accounts", "password-12345") == {"imported": 1, "skipped": 1, "total": 2}
    assert [call[0] for call in service.fake_runtime.calls].count("accounts.import") == 1


def test_provider_window_order_does_not_mislabel_weekly_limit(service):
    service.fake_runtime.rows = [row(quota={"observedAt": 1000000, "primary": {
        "usedPercent": 100, "resetAtMs": 2000000000000, "windowMinutes": 10080}})]
    service.refresh()
    usage = service.list()[0]["usage"]
    assert usage["fiveHour"] is None
    assert usage["weekly"]["remainingPercent"] == 0
    assert usage["weekly"]["resetsAt"] == 2000000000


def test_ready_second_account_wakes_before_first_accounts_long_reset(service):
    service.fake_runtime.rows = [row(health="cooldown",cooldownUntil=100000),
                                 row(accountRef="account_2",health="ready",cooldownUntil=0)]
    service.refresh()
    assert service.readiness()["ready"]
    assert service.next_available_at()==service.clock()
    with pytest.raises(AccountEngineError, match="invalid_auth_method"):
        service.begin_login("account_1", "token")
    with pytest.raises(AccountEngineError, match="manual_auth_not_supported"):
        service.finish_login("account_1", "secret-oauth-token")


def test_cancelled_or_replaced_auth_result_cannot_bind_a_new_login(service):
    entered, release = threading.Event(), threading.Event()
    def response(method, params):
        if method == "auth.begin":
            return {"ok": True, "data": {"attemptId": "challenge_1", "accountRef": "account_1", "url": "https://auth.openai.com/login"}}
        entered.set()
        assert release.wait(2)
        return {"ok": True, "data": {"attemptId": "challenge_1", "accountRef": "account_1", "state": "success"}}
    service.fake_runtime.response = response
    service.begin_login("account_1")
    errors = []
    def finish():
        try:
            service.finish_login("account_1")
        except AccountEngineError as error:
            errors.append(error.reason)
    thread = threading.Thread(target=finish)
    thread.start()
    assert entered.wait(1)
    service._auth["account_1"] = "challenge_2"
    release.set()
    thread.join(2)
    assert errors == ["stale_auth_binding"]
    assert service._auth["account_1"] == "challenge_2"


def test_background_refresh_is_single_flight_and_close_prevents_restart(service):
    entered, release = threading.Event(), threading.Event()
    def response(method, params):
        entered.set()
        assert release.wait(2)
        return {"ok": True, "data": {"accounts": [row()]}}
    service.fake_runtime.response = response
    assert service.request_refresh()
    assert entered.wait(1)
    assert not service.request_refresh(force=True)
    service.close()
    release.set()
    service._refresh_thread.join(2)
    assert service.status["state"] == "closed"
    assert service.list() == []
    assert not service.request_refresh(force=True)


def test_migration_acknowledgement_persists_only_ui_flag(service):
    assert service.status["migration_required"]
    service.acknowledge_migration()
    saved = json.loads((service.root / "ui-state.json").read_text())
    assert saved == {"migration_required": False}
    assert not service.status["migration_required"]


def test_quota_ms_timestamps_and_failure_projection_are_explicit(service):
    service.fake_runtime.rows = [row(cooldownUntil=1_800_000_000_000, quota={"observedAt": 1_799_999_000_000,
        "primary": {"usedPercent": 10, "resetAtMs": 1_800_000_000_000, "windowMinutes": 300},
        "secondary": {"usedPercent": 25, "resetAtMs": 1_800_006_000_000, "windowMinutes": 10080}})]
    account = service.refresh()[0]
    assert account["cooldown_until"] == 1_800_000_000
    assert account["usage"]["lastSuccessAt"] == 1_799_999_000
    assert account["usage"]["fiveHour"]["resetsAt"] == 1_800_000_000
    projected = service._attempt_projection({"attemptId": "attempt_1", "failure": {"kind": "quota", "confirmed": True, "retryAtMs": 1_800_000_000_000,
        "authorization": "SECRET"}, "events": [{"type": "response", "status": 429, "at": 1_800_000_000_000}, {"type": "SECRET"}]}, "attempt_1")
    assert projected["failure"] == {"kind": "quota", "confirmed": True, "retry_at": 1_800_000_000}
    assert projected["events"][0]["status"] == 429
    assert "SECRET" not in json.dumps(projected)


def test_successful_mutation_is_not_repeated_after_cache_refresh_fails(service):
    def response(method, params):
        if method == "account.create":
            return {"ok": True, "data": row()}
        raise EmbeddedRuntimeError("engine_connection_failed")
    service.fake_runtime.response = response
    created = service.create("One")
    assert created["id"] == "account_1"
    assert service.list()[0]["id"] == "account_1"
    assert sum(call[0] == "account.create" for call in service.fake_runtime.calls) == 1
    assert service.status["state"] == "unavailable"


def test_automatic_refresh_honors_disabled_retry_and_closed_even_when_forced(service):
    cfg = {"accounts_enabled": False}
    service.config.snapshot = lambda: cfg
    assert not service.request_refresh(force=True)
    assert not service.fake_runtime.calls
    cfg["accounts_enabled"] = True
    service._health["next_retry_at"] = 1002
    assert not service.request_refresh(force=True)
    service.clock = lambda: 1002
    assert service.request_refresh(force=True)
    service._refresh_thread.join(2)
    service.close()
    assert not service.request_refresh(force=True)


def test_transient_failures_use_bounded_exponential_backoff(service, monkeypatch):
    monkeypatch.setattr("waraq.embedded_accounts.time.sleep", lambda _: None)
    service.fake_runtime.fail = EmbeddedRuntimeError("engine_connection_failed")
    now = [1000]
    service.clock = lambda: now[0]
    for delay in (2, 4, 8, 16, 32, 60, 60):
        before = len(service.fake_runtime.calls)
        assert service.request_refresh(force=True)
        service._refresh_thread.join(2)
        assert len(service.fake_runtime.calls) == before + 2
        assert service.status["next_retry_at"] == now[0] + delay
        assert not service.status["requires_manual_recovery"]
        assert not service.request_refresh(force=True)
        now[0] += delay
    service.fake_runtime.fail = None
    assert service.request_refresh(force=True)
    service._refresh_thread.join(2)
    assert service.status["next_retry_at"] == 0


@pytest.mark.parametrize("reason", ["private_path_is_link", "private_permissions_failed", "private_owner_unknown",
    "engine_payload_missing", "engine_integrity_failed", "engine_manifest_invalid", "engine_version_mismatch",
    "engine_protocol_mismatch", "engine_owner_unverified", "engine_shutdown_unverified", "engine_already_owned"])
def test_fixed_errors_latch_once_until_explicit_recheck(service, reason):
    attempts = []
    def fail():
        attempts.append(reason)
        raise EmbeddedRuntimeError(reason)
    service.supervisor.ensure = fail
    assert service.request_refresh()
    service._refresh_thread.join(2)
    assert attempts == [reason]  # never the immediate idempotent retry
    assert service.status["requires_manual_recovery"]
    assert service.status["next_retry_at"] == 0
    service.clock = lambda: 10_000
    assert not service.request_refresh(force=True)
    with pytest.raises(AccountEngineError, match=reason):
        service.recheck("")
    assert attempts == [reason, reason]
    assert service.status["requires_manual_recovery"]
    service.supervisor.ensure = lambda: service.fake_runtime
    service.recheck("")
    assert service.status["state"] == "ready"
    assert not service.status["requires_manual_recovery"]


def test_mutation_transport_failure_is_never_repeated(service):
    service.fake_runtime.fail = EmbeddedRuntimeError("engine_connection_failed")
    with pytest.raises(AccountEngineError):
        service.create("Synthetic")
    assert [call[0] for call in service.fake_runtime.calls] == ["account.create"]


def test_identical_public_cache_is_not_written_again(service, monkeypatch):
    writes = []
    monkeypatch.setattr("waraq.embedded_accounts._atomic_json", lambda path, value: writes.append(json.dumps(value, sort_keys=True)))
    service.refresh()
    service.refresh()
    assert len(writes) == 1
    service.fake_runtime.rows[0]["alias"] = "Changed"
    service.refresh()
    assert len(writes) == 2


def test_healthy_metadata_and_quota_keep_existing_intervals(service):
    now = [1000]
    service.clock = lambda: now[0]
    service.config.snapshot = lambda: {"accounts_enabled": True, "accounts_auto_refresh": True,
                                       "accounts_refresh_seconds": 120}
    for tick, expected_lists, expected_usage in ((1000, 2, 1), (1004, 2, 1), (1005, 3, 1), (1119, 4, 1), (1120, 4, 1), (1124, 6, 2)):
        now[0] = tick
        if service.request_refresh():
            service._refresh_thread.join(2)
        methods = [call[0] for call in service.fake_runtime.calls]
        assert methods.count("accounts.list") == expected_lists
        assert methods.count("usage.refresh") == expected_usage


def test_scheduler_refresh_declines_while_synchronous_manual_work_is_active(service):
    entered, release = threading.Event(), threading.Event()
    def response(method, params):
        entered.set()
        assert release.wait(2)
        return {"ok": True, "data": row()}
    service.fake_runtime.response = response
    thread = threading.Thread(target=lambda: service._call("account.update", {"accountRef": "account_1"}))
    thread.start()
    assert entered.wait(1)
    assert service.refresh_in_flight
    assert not service.request_refresh(force=True)
    release.set()
    thread.join(2)
    assert not service.refresh_in_flight


def test_attempt_begin_cannot_double_count_in_flight_with_automatic_refresh(service, tmp_path):
    service.refresh()
    selected, release = threading.Event(), threading.Event()
    def response(method, params):
        if method == "attempt.begin":
            service.fake_runtime.rows = [row(inFlight=1)]
            return {"ok": True, "data": {"attemptId": params["attemptId"], "accountRef": "account_1"}}
        return {"ok": True, "data": {"accounts": service.fake_runtime.rows}}
    def ensure():
        if service.fake_runtime.calls[-1][0] == "attempt.begin":
            selected.set()  # native selection succeeded; local increment is pending
            assert release.wait(3)
        return service.fake_runtime
    service.fake_runtime.response = response
    service.supervisor.ensure = ensure
    leases = []
    thread = threading.Thread(target=lambda: leases.append(service.begin_attempt(1, "oc1", "model", tmp_path)))
    thread.start()
    assert selected.wait(1)
    assert service.refresh_in_flight
    assert not service.request_refresh(force=True)
    release.set()
    thread.join(2)
    assert len(leases) == 1 and service.list()[0]["in_flight"] == 1
    assert service.request_refresh(force=True)
    service._refresh_thread.join(2)
    assert service.list()[0]["in_flight"] == 1


def test_quota_network_wait_does_not_block_execution_control(service, tmp_path):
    service.refresh()
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def response(method, params):
        if method == "usage.refresh":
            entered.set()
            assert release.wait(3)
            return {"ok": True, "data": {}}
        if method == "accounts.list":
            return {"ok": True, "data": {"accounts": [row()]}}
        return {"ok": True, "data": {"attemptId": params["attemptId"], "accountRef": "account_1",
            "state": "closed" if method == "attempt.close" else "active", "events": []}}
    service.fake_runtime.response = response
    def quota():
        try:
            service.refresh_usage()
        except Exception as exc:
            errors.append(exc)
    def execution():
        try:
            lease = service.begin_attempt(1, "oc1", "model", tmp_path)
            service.resolve_attempt(lease["id"])
            service.close_attempt(lease["id"])
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()
    quota_thread = threading.Thread(target=quota)
    execution_thread = threading.Thread(target=execution)
    quota_thread.start()
    assert entered.wait(1)
    execution_thread.start()
    try:
        assert completed.wait(.5), "execution control blocked behind provider quota network wait"
        assert not errors
        assert [call[0] for call in service.fake_runtime.calls][-3:] == ["attempt.begin", "attempt.resolve", "attempt.close"]
        assert not release.is_set()
    finally:
        release.set()
        quota_thread.join(2)
        execution_thread.join(2)
    assert not errors
