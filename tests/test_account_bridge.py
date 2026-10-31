"""Embedded account bridge acceptance with cached synthetic data; no OAuth/network."""
from __future__ import annotations
import copy
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import pytest
from qt_test_support import dispose_bridge
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.store import Store


def empty_usage():
    window = {"status": "unknown", "remainingPercent": None, "resetsAt": None, "windowMinutes": None}
    return {"status": "unknown", "stale": False, "lastSuccessAt": None, "lastAttemptAt": None,
            "messageCode": "not_fetched", "source": "", "refreshing": False, "canRefresh": True,
            "refreshWaitSeconds": 0, "refreshBlockReason": "", "fiveHour": dict(window), "weekly": dict(window)}


class EmbeddedAccountsDouble:
    def __init__(self):
        self.rows, self.calls = [], []
        self.status = {"state": "ready", "migration_required": True}
        self.gate = None
        self.login_pending = False
        self.auth_url = "https://auth.openai.com/authorize?state=SYNTHETIC"

    def _called(self, action):
        self.calls.append((action, threading.get_ident()))
        if self.gate:
            assert self.gate.wait(5)

    def list(self):
        return copy.deepcopy(self.rows)

    def row(self, ident):
        return next(row for row in self.rows if row["id"] == ident)

    def create(self, alias):
        self._called("create")
        row = {"id": "native-" + str(len(self.rows)), "alias": alias, "priority": len(self.rows),
               "enabled": True, "preferred": False, "in_flight": 0, "login_state": "signed_out",
               "email": "PRIVATE_EMAIL", "subscription": "Plus", "usage": empty_usage(),
               "access_token": "NEVER_PUBLISH_THIS", "identity": "PRIVATE_NATIVE_ID"}
        self.rows.append(row)
        return row

    def update(self, ident, **fields):
        self._called("update")
        self.row(ident).update(fields)

    def prefer(self, ident):
        self._called("prefer")
        for row in self.rows:
            row["preferred"] = row["id"] == ident

    def auth_methods(self, ident):
        self._called("methods")
        return [{"id": "browser", "label": "دخول المتصفح", "type": "oauth", "access_token": "NEVER_PUBLISH_THIS"}]

    def begin_login(self, ident, method=None):
        self._called("begin")
        self.row(ident)["login_state"] = "authorizing"
        return {"url": self.auth_url, "mode": "auto", "status": "pending", "userCode": "DEMO-1234",
                "challenge_id": "PRIVATE_CHALLENGE", "refresh_token": "NEVER_PUBLISH_THIS"}

    def finish_login(self, ident, code=None):
        self._called("finish")
        if self.login_pending:
            return {"status": "pending"}
        self.row(ident)["login_state"] = "authenticated"
        return {"status": "complete", "authenticated": True, "access_token": "NEVER_PUBLISH_THIS"}

    def cancel_login(self, ident):
        self._called("cancel")
        self.row(ident)["login_state"] = "signed_out"

    def recheck(self, ident):
        self._called("recheck")
        return self.row(ident)

    def remove(self, ident):
        self._called("remove")
        self.rows = [row for row in self.rows if row["id"] != ident]

    def refresh(self):
        self._called("refresh")
        return self.list()

    def request_refresh(self, force=False):
        # Presentation/phone fixtures own their synthetic rows. Automatic
        # polling is exercised separately with the real account-service gate.
        return False

    def refresh_usage(self, account_id=None, manual=False):
        self._called("usage")
        return self.list()

    def acknowledge_migration(self):
        self._called("acknowledge")
        self.status["migration_required"] = False

    def export_accounts(self, ids, path, password):
        self._called("export")
        if getattr(self, "transfer_error", None):
            raise self.transfer_error
        return {"exported": len(ids), "password": password, "refreshToken": "NEVER_PUBLISH_THIS"}

    def import_accounts(self, path, password):
        self._called("import")
        if getattr(self, "transfer_error", None):
            raise self.transfer_error
        return {"imported": 1, "skipped": 1, "total": 2, "password": password, "refreshToken": "NEVER_PUBLISH_THIS"}

    def close(self):
        pass


def phone_usage_fixtures():
    """Shared deterministic presentation cases; contains no real account material."""
    empty = empty_usage()
    available = {**empty, "status": "available", "messageCode": "usage_available", "lastSuccessAt": 1800000000,
                 "source": "embedded_codex_usage",
                 "fiveHour": {"status": "available", "remainingPercent": 0, "resetsAt": 1800018000, "windowMinutes": 300},
                 "weekly": {"status": "available", "remainingPercent": 43, "resetsAt": 1800604800, "windowMinutes": 10080}}
    stale = {**available, "status": "unavailable", "stale": True, "messageCode": "timeout",
             "fiveHour": {**available["fiveHour"], "status": "stale"}, "weekly": dict(empty["weekly"])}
    unsupported = {**empty, "status": "unsupported", "messageCode": "token_sharing_unsupported"}
    return [{"displayKey": "display-" + str(index), "alias": alias, "usageRefreshing": False, "usage": usage}
            for index, (alias, usage) in enumerate((
                ("اسم مشترك", available), ("اسم مشترك", stale),
                ('<img src=x onerror="window.__quota_xss=1"> حساب طويل لمتابعة الدروس والملفات', unsupported),
                ("حساب غير معروف", empty)))]


def pump(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), "background account operation did not settle"


@pytest.fixture
def account_ui(tmp_path, monkeypatch, qt_app):
    config, store = Config(tmp_path), Store(tmp_path / "test.db")
    accounts = EmbeddedAccountsDouble()
    scheduler = SimpleNamespace(accounts=accounts, running=False, workers=lambda: [], idle=lambda: True,
                                disabled_workers={}, forget_keys=lambda: None, shutdown=lambda: None,
                                library=SimpleNamespace())
    scheduler.recheck_account = lambda ident: accounts.recheck(ident) if ident else accounts.refresh()
    opened = []
    monkeypatch.setattr("waraq.bridge.QDesktopServices.openUrl", lambda url: opened.append(url.toString()) or True)
    bridge = Bridge(store, config, None, scheduler)
    bridge.timer.stop()
    bridge._account_usage_timer.stop()
    try:
        yield SimpleNamespace(app=qt_app, config=config, store=store, accounts=accounts,
                              scheduler=scheduler, bridge=bridge, opened=opened)
    finally:
        bridge.closeAccounts()
        if accounts.gate:
            accounts.gate.set()
        pump(qt_app, lambda: not bridge._account_pending)
        dispose_bridge(bridge)
        store.db.close()


def add_account(env):
    env.bridge.addAccount("الحساب الأول")
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    return env.accounts.rows[-1]["id"]


def test_all_account_writes_and_auth_run_off_gui_thread(account_ui):
    env = account_ui
    ident = add_account(env)
    for action in (lambda: env.bridge.renameAccount(ident, "جديد"), lambda: env.bridge.preferAccount(ident),
                   lambda: env.bridge.setAccountEnabled(ident, False), lambda: env.bridge.refreshAccount(ident)):
        action()
        pump(env.app, lambda: not env.bridge._account_operation_busy())
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge.accountLoginChoice.get("loading"))
    assert "NEVER_PUBLISH_THIS" not in json.dumps(env.bridge.accountLoginChoice)
    env.bridge.chooseAccountLoginMethod(ident, "browser")
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.opened and env.accounts.row(ident)["login_state"] == "authenticated"
    assert all(thread != threading.get_ident() for _, thread in env.accounts.calls)
    assert "NEVER_PUBLISH_THIS" not in json.dumps(env.bridge.accountsModel._items)
    assert "PRIVATE_CHALLENGE" not in json.dumps(env.bridge._account_logins)


def test_signal_boundary_strips_native_secrets():
    secret = {"status": "complete", "authenticated": True, "access_token": "SECRET", "identity": "NATIVE"}
    assert Bridge._safe_account_result("finish", secret) == {"status": "complete", "authenticated": True}
    assert Bridge._safe_account_result("create", secret) is None
    assert Bridge._safe_account_result("export", {"exported": 1, "password": "SECRET"}) == {"exported": 1}
    assert Bridge._safe_account_result("import", {"imported": 1, "skipped": 0, "total": 1, "password": "SECRET"}) == {"imported": 1, "skipped": 0, "total": 1}
    assert Bridge._safe_account_result("methods", [{"id": "browser", "type": "oauth", "label": "Login", "secret": "SECRET"}]) == [
        {"id": "browser", "type": "oauth", "label": "Login"}]


def test_transfer_cancel_and_invalid_input_do_not_start_workers(account_ui, monkeypatch):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["can_export"] = True
    env.bridge._publish_accounts()
    dialogs = []
    monkeypatch.setattr("waraq.bridge.QFileDialog.getSaveFileName", lambda *args: dialogs.append(args) or ("", ""))
    env.bridge.exportAccounts([ident], "short")
    assert not dialogs and env.bridge.accountTransfer["status"] == "error"
    env.bridge.exportAccounts([ident], "password-12345")
    assert len(dialogs) == 1 and env.bridge.accountTransfer["status"] == "idle"
    assert all(action != "export" for action, _ in env.accounts.calls)
    monkeypatch.setattr("waraq.bridge.QFileDialog.getOpenFileName", lambda *args: ("", ""))
    assert env.bridge.chooseAccountImportFile() == ""


def test_transfer_runs_in_background_and_redacts_results_and_errors(account_ui, tmp_path, monkeypatch):
    from waraq.embedded_accounts import AccountEngineError
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["can_export"] = True
    env.bridge._publish_accounts()
    assert env.bridge.accountExportChoices == [{"id": ident, "alias": "الحساب الأول", "email": "PRIVATE_EMAIL"}]
    monkeypatch.setattr("waraq.bridge.QFileDialog.getSaveFileName", lambda *args: (str(tmp_path / "backup.waraq-accounts"), ""))
    signals = []
    env.bridge._accountTaskReady.connect(lambda *args: signals.append(args))
    env.accounts.gate = threading.Event()
    env.bridge.exportAccounts([ident], "  كلمة أمان عربية  ")
    assert env.bridge.accountBusy and env.bridge.accountTransfer["status"] == "working"
    assert not env.bridge.accountTransferAvailable
    env.accounts.gate.set()
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.bridge.accountTransfer["exported"] == 1
    assert "NEVER_PUBLISH_THIS" not in json.dumps(signals)
    assert "كلمة أمان" not in json.dumps(env.bridge.accountTransfer, ensure_ascii=False)
    assert all(thread != threading.get_ident() for action, thread in env.accounts.calls if action == "export")
    env.accounts.transfer_error = AccountEngineError("TRANSFER_DECRYPT_FAILED")
    env.bridge.importAccounts(str(tmp_path / "backup.waraq-accounts"), "wrong-password-123")
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.bridge.accountTransfer == {"status": "error", "message": "كلمة المرور غير صحيحة أو الملف تالف"}
    del env.accounts.transfer_error
    env.bridge.importAccounts(str(tmp_path / "backup.waraq-accounts"), "password-12345")
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.bridge.accountTransfer["imported"] == 1 and env.bridge.accountTransfer["skipped"] == 1


def test_transfer_is_blocked_by_execution_and_uncertain_sessions(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["in_flight"] = 1
    env.bridge._publish_accounts()
    assert not env.bridge.accountTransferAvailable
    env.bridge.importAccounts("unused.waraq-accounts", "password-12345")
    assert env.bridge.accountTransfer["status"] == "error"
    assert all(action != "import" for action, _ in env.accounts.calls)


def test_cached_snapshots_do_not_request_engine_or_quotas(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["usage"]["fiveHour"].update(status="available", remainingPercent=0)
    before = list(env.accounts.calls)
    for _ in range(4):
        env.bridge.refresh()
        snapshot = env.bridge.internet_commands._snapshot()
    assert env.accounts.calls == before
    encoded = json.dumps(snapshot["accountUsage"])
    assert "native-" not in encoded and "PRIVATE_EMAIL" not in encoded and "PRIVATE_NATIVE_ID" not in encoded
    assert snapshot["accountUsage"][0]["usage"]["fiveHour"]["remainingPercent"] == 0


def test_model_updates_preserve_row_identity_and_reset_free_refresh(account_ui):
    env = account_ui
    ident = add_account(env)
    from PySide6.QtTest import QSignalSpy
    resets = QSignalSpy(env.bridge.accountsModel.modelReset)
    env.accounts.row(ident)["usage"]["weekly"].update(status="available", remainingPercent=42)
    env.bridge._publish_accounts()
    assert resets.count() == 0
    assert env.bridge.accountsModel._items[0]["id"] == ident


def test_cancel_and_close_ignore_delayed_login_results(account_ui):
    env = account_ui
    ident = add_account(env)
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge.accountLoginChoice.get("loading"))
    env.accounts.gate = threading.Event()
    env.bridge.chooseAccountLoginMethod(ident, "browser")
    env.bridge.closeAccounts()
    env.accounts.gate.set()
    pump(env.app, lambda: not env.bridge._account_pending)
    assert not env.opened


def test_untrusted_login_url_is_rejected_and_cancelled(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.auth_url = "https://example.invalid/steal"
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge.accountLoginChoice.get("loading"))
    env.bridge.chooseAccountLoginMethod(ident, "browser")
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert not env.opened
    assert "cancel" in [action for action, _ in env.accounts.calls]


def test_engine_health_and_migration_acknowledgement(account_ui):
    env = account_ui
    assert env.bridge.accountEngine["migrationRequired"]
    env.bridge.acknowledgeAccountMigration()
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert not env.bridge.accountEngine["migrationRequired"]
    env.accounts.status.update(state="unavailable", reason="SECRET_EXCEPTION")
    env.bridge._publish_accounts()
    assert "SECRET_EXCEPTION" not in json.dumps(env.bridge.accountEngine)
    env.bridge.recoverAccountEngine()
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert "refresh" in [action for action, _ in env.accounts.calls]


def test_engine_reason_projection_is_allowlisted_and_actionable(account_ui):
    env = account_ui
    env.accounts.status.update(state="unavailable", reason="private_path_is_link", next_retry_at=0,
                               requires_manual_recovery=True)
    env.bridge._publish_accounts()
    status = env.bridge.accountEngine
    assert status["reasonCode"] == "private_path_is_link"
    assert status["requiresManualRecovery"] and status["nextRetryAt"] == 0
    assert "رابط" in status["message"]
    env.accounts.status.update(reason="C:/SECRET/token", next_retry_at=float("inf"))
    env.bridge._publish_accounts()
    status = env.bridge.accountEngine
    assert status["reasonCode"] == "engine_unavailable" and status["nextRetryAt"] == 0
    assert "SECRET" not in json.dumps(status)


def test_disabled_accounts_do_not_claim_engine_is_starting(account_ui):
    env = account_ui
    env.config.snapshot = lambda: {"accounts_enabled": False}
    env.accounts.status = {"state": "starting"}
    env.bridge._publish_accounts()
    assert env.bridge.accountEngine["reasonCode"] == "accounts_disabled"
    assert "معطّلة" in env.bridge.accountEngine["message"]
    env.accounts.status = {"state": "ready"}
    env.bridge._publish_accounts()
    assert env.bridge.accountEngine["state"] == "ready"
    assert "جاهز" in env.bridge.accountEngine["message"]


def _add_synthetic_job(env):
    return env.store.add([{"video_id": "synthetic", "title": "Synthetic", "position": 1}], env.config.snapshot())[0]


def test_idle_projection_skips_jobs_sql_and_json_until_real_sqlite_writes(account_ui, monkeypatch):
    env = account_ui
    ident = _add_synthetic_job(env)
    env.bridge.refresh()
    queries = []
    env.store.db.set_trace_callback(queries.append)
    original = env.store.jobs
    reads = []
    def jobs():
        reads.append(True)
        return original()
    monkeypatch.setattr(env.store, "jobs", jobs)
    for _ in range(5):
        env.bridge.refresh()
    assert not reads
    assert not any("FROM jobs" in query or "FROM stages" in query for query in queries)
    # Same-connection writes require total_changes, not just data_version.
    revision = env.store.revision()
    env.store.set_stage(ident, "download", "running", progress=37)
    assert env.store.revision() != revision
    env.bridge.refresh()
    assert len(reads) == 1 and env.bridge.jobs[0]["progress"] == 37
    # External commits require data_version, not just total_changes.
    revision = env.store.revision()
    with sqlite3.connect(env.store.path) as external:
        external.execute("UPDATE jobs SET title=? WHERE id=?", ("External title", ident))
    assert env.store.revision() != revision
    env.bridge.refresh()
    assert len(reads) == 2 and env.bridge.jobs[0]["title"] == "External title"
    env.store.db.set_trace_callback(None)


def test_retry_deadline_and_midnight_reproject_without_full_job_reads(account_ui, monkeypatch):
    env = account_ui
    ident = _add_synthetic_job(env)
    midnight = time.mktime((2026, 10, 21, 0, 0, 0, 0, 0, -1))
    now = [midnight - 10]
    deadline = midnight + 60
    env.store.set_stage(ident, "download", "pending", next_retry=deadline)
    monkeypatch.setattr("waraq.bridge.time.time", lambda: now[0])
    actual_localtime = time.localtime
    monkeypatch.setattr("waraq.bridge.time.localtime", lambda value=None: actual_localtime(now[0] if value is None else value))
    env.bridge.refresh()
    assert env.bridge.jobs[0]["retryAt"].startswith("2026-10-21")
    monkeypatch.setattr(env.store, "jobs", lambda: pytest.fail("unchanged jobs were reread"))
    now[0] = midnight + 1
    env.bridge.refresh()
    assert env.bridge.jobs[0]["retryAt"] == "00:01"
    now[0] = deadline
    env.bridge.refresh()
    assert env.bridge.jobs[0]["retryAt"] == ""
    # A backwards wall-clock adjustment restores a future retry label too.
    now[0] = midnight + 30
    env.bridge.refresh()
    assert env.bridge.jobs[0]["retryAt"] == "00:01"


def test_cached_jobs_keep_worker_elapsed_logs_and_auto_stop_heartbeat(account_ui, monkeypatch):
    env = account_ui
    ident = _add_synthetic_job(env)
    elapsed = [1]
    env.scheduler.running = True
    env.scheduler.workers = lambda: [{"id": "synthetic", "stage": "download", "elapsed": elapsed[0]}]
    env.bridge.refresh()
    original = env.store.jobs
    reads = []
    monkeypatch.setattr(env.store, "jobs", lambda: reads.append(True) or original())
    elapsed[0] = 2
    env.bridge.refresh()
    assert env.bridge.workers[0]["elapsed"] == 2 and not reads
    env.store.event(ident, "Synthetic event")
    env.bridge.refresh()
    assert env.bridge.logs[0]["message"] == "Synthetic event"
    env.bridge._auto_stop = True
    env.bridge.refresh()
    assert env.bridge._auto_stop_seen
    for stage in ("download", "deepgram", "opencode"):
        env.store.set_stage(ident, stage, "completed")
    env.bridge.refresh()
    assert env.bridge._auto_stop_fired


@pytest.fixture
def automatic_ui(account_ui, monkeypatch):
    from waraq.embedded_accounts import EmbeddedAccountService
    env = account_ui
    calls, fault, gate, response = [], [None], [None], [None]
    def rpc(method, params, directory=None):
        calls.append(method)
        if gate[0]:
            assert gate[0].wait(3)
        if fault[0]:
            raise fault[0]
        if response[0]:
            return response[0](method, params)
        return {"ok": True, "data": {"accounts": []}}
    runtime = SimpleNamespace(rpc=rpc)
    supervisor = SimpleNamespace(ensure=lambda: runtime, close=lambda: None)
    monkeypatch.setattr("waraq.embedded_accounts.private_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    client = EmbeddedAccountService(env.config, supervisor)
    env.scheduler.accounts = client
    env.scheduler.recheck_account = client.recheck
    env.client, env.calls, env.fault, env.gate, env.response = client, calls, fault, gate, response
    try:
        yield env
    finally:
        if gate[0]:
            gate[0].set()
        client.close()
        if client._refresh_thread:
            client._refresh_thread.join(3)


def test_qt_account_timer_shares_scheduler_single_flight_and_busy_state(automatic_ui):
    env = automatic_ui
    env.gate[0] = threading.Event()
    env.bridge._account_usage_timer.start(10)
    env.bridge.timer.start(15)
    pump(env.app, lambda: bool(env.calls))
    assert env.calls == ["accounts.list"]
    assert env.bridge.accountBusy
    assert not env.client.request_refresh(force=True)  # simultaneous scheduler wakeup
    for _ in range(5):
        env.app.processEvents()
        time.sleep(.015)
    assert env.calls == ["accounts.list"]
    env.gate[0].set()
    pump(env.app, lambda: not env.client.refresh_in_flight)
    env.bridge.refresh()
    assert not env.bridge.accountBusy
    assert env.calls == ["accounts.list", "usage.refresh", "accounts.list"]
    # Busy changes still notify QML once healthy metadata/rows stay identical.
    from PySide6.QtTest import QSignalSpy
    spy = QSignalSpy(env.bridge.accountsChanged)
    env.gate[0].clear()
    assert env.client.request_refresh(force=True)
    env.bridge._publish_accounts()
    assert env.bridge.accountBusy and spy.count() == 1
    env.gate[0].set()
    env.client._refresh_thread.join(2)
    env.bridge._publish_accounts()
    assert not env.bridge.accountBusy and spy.count() == 2


def test_qt_account_timer_disabled_backoff_static_and_manual_recovery(automatic_ui):
    from waraq.embedded_runtime import EmbeddedRuntimeError
    env = automatic_ui
    cfg = {"accounts_enabled": False, "accounts_auto_refresh": False}
    env.config.snapshot = lambda: cfg
    env.bridge._account_usage_timer.start(10)
    for _ in range(8):
        env.app.processEvents()
        time.sleep(.01)
    assert not env.calls
    cfg["accounts_enabled"] = True
    env.fault[0] = EmbeddedRuntimeError("private_path_is_link")
    pump(env.app, lambda: env.client.status["requires_manual_recovery"])
    for _ in range(8):
        env.app.processEvents()
        time.sleep(.01)
    assert env.calls == ["accounts.list"]
    env.fault[0] = None
    pump(env.app, lambda: not env.client.refresh_in_flight)
    env.bridge.recoverAccountEngine()
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.client.status["state"] == "ready" and len(env.calls) == 2
    env.client._health.update(state="unavailable", reason="engine_connection_failed", next_retry_at=time.time() + 60)
    env.client._next_refresh = 0
    for _ in range(8):
        env.app.processEvents()
        time.sleep(.01)
    assert len(env.calls) == 2
    assert not env.client.request_refresh(force=True)


def _configure_login_runtime(env, auth_response=None):
    account = {"accountRef": "account_1", "alias": "Synthetic", "loginState": "signed_out", "health": "ready"}
    def response(method, params):
        if auth_response and method in ("auth.status", "auth.begin"):
            overridden = auth_response(method, params)
            if overridden is not None:
                return overridden
        data = {"accounts": [account]}
        if method == "health":
            data = {"authMethods": [{"id": "browser", "label": "Browser"}]}
        elif method == "auth.begin":
            data = {"attemptId": "challenge_1", "accountRef": "account_1", "url": "https://auth.openai.com/login"}
        elif method == "auth.status":
            data = {"accountRef": "account_1", "state": "success"}
        return {"ok": True, "data": data}
    cfg = env.config.snapshot()
    cfg["accounts_auto_refresh"] = False
    env.config.snapshot = lambda: cfg
    env.response[0] = response
    env.client.refresh()
    env.bridge._publish_accounts()
    env.bridge.loginAccount("account_1")
    pump(env.app, lambda: not env.bridge.accountLoginChoice.get("loading"))


def test_delayed_auth_status_then_cancel_keeps_native_cancellation_intent(automatic_ui):
    env = automatic_ui
    entered, release = threading.Event(), threading.Event()
    def delayed(method, params):
        if method == "auth.status":
            entered.set()
            assert release.wait(3)
            return {"ok": True, "data": {"accountRef": "account_1", "state": "pending"}}
    _configure_login_runtime(env, delayed)
    env.bridge.chooseAccountLoginMethod("account_1", "browser")
    pump(env.app, entered.is_set)
    assert env.client.refresh_in_flight
    env.bridge.cancelAccountLogin("account_1")
    assert env.bridge._account_ops["account_1"] == "cancel" and env.bridge.accountBusy
    release.set()
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.calls.count("auth.cancel") == 1
    assert not env.client._auth and not env.bridge._account_logins


def test_automatic_refresh_racing_login_choice_queues_user_selection(automatic_ui):
    env = automatic_ui
    _configure_login_runtime(env)
    env.gate[0] = threading.Event()
    assert env.client.request_refresh(force=True)
    pump(env.app, lambda: env.calls.count("accounts.list") == 2)
    env.bridge.chooseAccountLoginMethod("account_1", "browser")
    assert not env.bridge.accountLoginChoice
    assert env.bridge._account_ops["account_1"] == "begin" and env.bridge.accountBusy
    assert "auth.begin" not in env.calls  # queued behind the automatic RPC
    env.gate[0].set()
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.calls.count("auth.begin") == 1 and len(env.opened) == 1


def test_cancel_during_auth_begin_waits_for_challenge_binding_then_cancels(automatic_ui):
    env = automatic_ui
    entered, release = threading.Event(), threading.Event()
    def delayed(method, params):
        if method == "auth.begin":
            entered.set()
            assert release.wait(3)
    _configure_login_runtime(env, delayed)
    env.bridge.chooseAccountLoginMethod("account_1", "browser")
    pump(env.app, entered.is_set)
    env.bridge.cancelAccountLogin("account_1")
    assert env.bridge._account_ops["account_1"] == "cancel" and env.bridge.accountBusy
    release.set()
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.calls.count("auth.cancel") == 1
    assert not env.client._auth and not env.opened
