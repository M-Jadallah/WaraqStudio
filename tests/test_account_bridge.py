"""Account UI orchestration uses native-runtime doubles: no authentication/network."""
from __future__ import annotations

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest
from qt_test_support import dispose_bridge

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.store import Store


class Accounts:
    def __init__(self):
        self.rows = []
        self.records = []

    def list(self):
        return copy.deepcopy(self.rows)

    def create(self, alias):
        row = {"id": "a" + str(len(self.rows) + 1), "alias": alias, "priority": len(self.rows),
               "enabled": True, "preferred": False, "in_flight": 0, "login_state": "signed_out",
               "cooldown_seconds": 0, "email": "", "subscription": "", "error": ""}
        self.rows.append(row)
        return dict(row)

    def row(self, ident):
        return next(row for row in self.rows if row["id"] == ident)

    def set_login_state(self, ident, state, error=""):
        row = self.row(ident)
        assert not row["in_flight"]
        row.update(login_state=state, error=error)

    def record_login_result(self, ident, result, reset_cooldown=True):
        self.records.append((ident, result, reset_cooldown))
        self.row(ident).update(login_state="authenticated", email=result.get("email", ""),
                               subscription=result.get("subscription", ""), identity=result.get("identity"))

    def remove(self, ident):
        assert not self.row(ident)["in_flight"]
        self.rows = [row for row in self.rows if row["id"] != ident]

    def update(self, ident, **fields):
        self.row(ident).update(fields)

    def prefer(self, ident):
        for row in self.rows:
            row["preferred"] = row["id"] == ident


class Runtime:
    def __init__(self):
        self.calls = []
        self.begin = {"url": "https://auth.openai.com/authorize?state=PRIVATE_STATE", "mode": "auto"}
        self.finish = {"status": "complete", "authenticated": True, "identity": "stable-account",
                       "email": "test@example.invalid", "subscription": "Plus", "access_token": "SECRET_TOKEN"}
        self.gate = None
        self.method_gate = None
        self.selected_methods = []
        self.methods = [
            {"id": "chatgpt-token-sharing", "type": "oauth", "label": "Sign in with ChatGPT", "kind": "siwc",
             "usage": {"status": "unsupported", "reason_code": "token_sharing_unsupported", "source": ""}},
            {"id": "chatgpt-browser", "type": "oauth", "label": "Codex browser (legacy)", "kind": "legacy_codex",
             "usage": {"status": "available", "reason_code": "legacy_supported", "source": "legacy_codex_usage"}}]

    def auth_methods(self, ident):
        if self.method_gate:
            assert self.method_gate.wait(5)
        if isinstance(self.methods, Exception):
            raise self.methods
        return self.methods

    def begin_login(self, ident, method=None):
        assert method in {item["id"] for item in self.methods}, "UI must explicitly choose an offered method"
        self.selected_methods.append(method)
        self.calls.append(("begin", ident, threading.get_ident()))
        if self.gate:
            assert self.gate.wait(5)
        if isinstance(self.begin, Exception):
            raise self.begin
        return self.begin

    def finish_login(self, ident, code=None):
        self.calls.append(("finish", ident, threading.get_ident()))
        return self.finish

    def metadata(self, ident):
        self.calls.append(("metadata", ident, threading.get_ident()))
        return self.finish

    def cancel_login(self, ident):
        self.calls.append(("cancel", ident, threading.get_ident()))

    def delete_auth(self, ident):
        self.calls.append(("delete", ident, threading.get_ident()))

    def close(self):
        pass


class UsageMonitorDouble:
    """Asynchronous safe telemetry double; deliberately separate from native auth."""
    def __init__(self):
        self.callback = None
        self.snapshots = {}
        self.pending = set()
        self.epochs = {}
        self.requests = []
        self.applied_threads = []
        self.gate = threading.Event()
        self.workers = []
        self.stopped = False
        self.scheduled = 0
        self.maintenance = set()

    def set_callback(self, callback):
        self.callback = callback

    def state(self, ident):
        return {"refreshing": ident in self.pending, "maintenance": ident in self.maintenance}

    def public_snapshot(self, ident):
        window = {"status": "unavailable", "remainingPercent": None, "resetsAt": None, "windowMinutes": None}
        snapshot = copy.deepcopy(self.snapshots.get(ident, {
            "status": "unavailable", "stale": False, "lastSuccessAt": None, "lastAttemptAt": None,
            "messageCode": "not_checked", "source": "", "fiveHour": dict(window), "weekly": dict(window)}))
        snapshot["refreshing"] = ident in self.pending
        snapshot.setdefault("canRefresh", not self.stopped and ident not in self.pending)
        snapshot.setdefault("refreshWaitSeconds", 0)
        snapshot.setdefault("refreshBlockReason", "refreshing" if ident in self.pending else "")
        return snapshot

    def request(self, ident, manual=False):
        if self.stopped or ident in self.pending or not self.public_snapshot(ident)["canRefresh"]:
            return False
        self.pending.add(ident)
        self.requests.append((ident, manual))
        epoch = self.epochs.get(ident, 0)
        def work():
            self.gate.wait(3)
            if not self.stopped:
                self.callback({"account_id": ident, "auth_epoch": epoch})
        worker = threading.Thread(target=work, daemon=True)
        self.workers.append(worker)
        worker.start()
        return True

    def request_all(self):
        return sum(self.request(ident, True) for ident in self.snapshots)

    def apply(self, payload):
        self.applied_threads.append(threading.get_ident())
        ident = payload["account_id"]
        if self.stopped or ident not in self.pending or self.epochs.get(ident, 0) != payload["auth_epoch"]:
            return False
        self.pending.discard(ident)
        self.snapshots[ident] = {
            "status": "available", "stale": False, "lastSuccessAt": 1800000000, "lastAttemptAt": 1800000000,
            "messageCode": "usage_available", "source": "legacy_codex_usage",
            "fiveHour": {"status": "available", "remainingPercent": 0, "resetsAt": 1800018000, "windowMinutes": 300},
            "weekly": {"status": "available", "remainingPercent": 43, "resetsAt": 1800604800, "windowMinutes": 10080}}
        return True

    def invalidate(self, ident):
        self.epochs[ident] = self.epochs.get(ident, 0) + 1
        self.pending.discard(ident)
        self.snapshots.pop(ident, None)

    def schedule_due(self):
        self.scheduled += 1

    def stop(self):
        self.stopped = True
        self.pending.clear()
        self.gate.set()


def attach_usage(env):
    monitor = UsageMonitorDouble()
    env.scheduler.account_usage = monitor
    monitor.set_callback(env.bridge._accountUsageReady.emit)
    return monitor


def phone_usage_fixtures():
    """Shared deterministic presentation cases; contains no real account material."""
    empty = UsageMonitorDouble().public_snapshot("unknown")
    available = {**empty, "status": "available", "messageCode": "usage_available", "lastSuccessAt": 1800000000,
                 "source": "legacy_codex_usage",
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
    app = qt_app
    config, store = Config(tmp_path), Store(tmp_path / "test.db")
    accounts, runtime = Accounts(), Runtime()
    scheduler = SimpleNamespace(accounts=accounts, account_runtime=runtime, running=False,
                                workers=lambda: [], idle=lambda: True, disabled_workers={},
                                forget_keys=lambda: None, shutdown=lambda: None, library=SimpleNamespace())
    opened = []
    monkeypatch.setattr("waraq.bridge.QDesktopServices.openUrl", lambda url: opened.append(url.toString()) or True)
    bridge = Bridge(store, config, None, scheduler)
    bridge.timer.stop()
    try:
        yield SimpleNamespace(app=app, config=config, store=store, accounts=accounts, runtime=runtime,
                              scheduler=scheduler, bridge=bridge, opened=opened)
    finally:
        bridge.closeAccounts()
        if runtime.gate:
            runtime.gate.set()
        pump(app, lambda: not bridge._account_pending)
        dispose_bridge(bridge)
        store.db.close()


def add_account(env):
    env.bridge.addAccount("الحساب الأول")
    return env.accounts.rows[-1]["id"]


def start_login(env, ident, method="chatgpt-browser"):
    """Drive the explicit choice before testing the existing OAuth lifecycle."""
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    if env.bridge.accountLoginChoice.get("methods"):
        env.bridge.chooseAccountLoginMethod(ident, method)


def test_account_mode_is_opt_in_and_validates_integer_concurrency(tmp_path):
    config = Config(tmp_path)
    assert config.data["chatgpt_accounts_enabled"] is False
    assert config.data["chatgpt_account_concurrency"] == 5
    for bad in (0, 17, 1.5, True, "5"):
        with pytest.raises(ValueError):
            config.save({"chatgpt_account_concurrency": bad})
    with pytest.raises(ValueError):
        config.save({"chatgpt_accounts_enabled": "true"})


def test_startup_and_registry_edit_do_not_start_native_auth(account_ui):
    env = account_ui
    ident = add_account(env)
    env.bridge.renameAccount(ident, "تسمية جديدة")
    env.bridge.preferAccount(ident)
    env.bridge.setAccountEnabled(ident, False)
    assert env.bridge.accountsModel.rowCount() == 1
    assert not env.runtime.calls and not env.opened
    assert not env.bridge.accountBusy
    assert "تسمية جديدة" in env.bridge.accountsModel._items[0]["alias"]


def test_login_runs_native_calls_off_gui_and_publishes_only_safe_metadata(account_ui):
    env = account_ui
    ident = add_account(env)
    main_thread = threading.get_ident()
    start_login(env, ident)
    assert env.bridge.accountBusy
    assert not env.opened  # the worker may finish, but only a queued GUI callback can open the browser
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert len(env.opened) == 1
    assert all(call[2] != main_thread for call in env.runtime.calls)
    row = env.bridge.accountsModel._items[0]
    assert row["email"] == "test@example.invalid" and row["subscription"] == "Plus"
    assert "غير معروفة" in row["quotaText"]
    assert not any(value in json.dumps(row) for value in ("SECRET_TOKEN", "PRIVATE_STATE", "access_token"))
    assert env.accounts.records[-1][2] is True


@pytest.mark.parametrize("url", ["http://auth.openai.com/authorize", "https://auth.openai.com.evil.invalid/",
                                  "https://user@auth.openai.com/", "file:///C:/danger", "https://auth.openai.com:444/"])
def test_untrusted_oauth_urls_never_open_browser(account_ui, url):
    env = account_ui
    ident = add_account(env)
    env.runtime.begin = {"url": url, "mode": "auto"}
    start_login(env, ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert not env.opened
    assert env.bridge.accountsModel._items[0]["error"]
    assert any(call[0] == "cancel" for call in env.runtime.calls)


def test_cancel_rejects_late_begin_callback_and_duplicate_actions(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.gate = threading.Event()
    start_login(env, ident)
    pump(env.app, lambda: bool(env.runtime.calls))
    env.bridge.loginAccount(ident)
    env.bridge.removeAccount(ident)
    assert len(env.runtime.calls) == 1
    env.bridge.cancelAccountLogin(ident)
    pump(env.app, lambda: any(call[0] == "cancel" for call in env.runtime.calls))
    env.runtime.gate.set()
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert not env.opened and not env.accounts.records
    assert env.accounts.row(ident)["login_state"] == "signed_out"


def test_manual_code_field_only_required_by_native_method(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.begin["mode"] = "code"
    start_login(env, ident)
    pump(env.app, lambda: bool(env.bridge._account_logins))
    assert env.bridge.accountsModel._items[0]["needsCode"]
    assert not any(call[0] == "finish" for call in env.runtime.calls)
    env.bridge.finishAccountLogin(ident, "")
    assert not any(call[0] == "finish" for call in env.runtime.calls)
    env.bridge.finishAccountLogin(ident, "private-code")
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert "private-code" not in json.dumps(env.bridge.accountsModel._items)


def test_metadata_reserves_account_and_preserves_observed_cooldown(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["cooldown_seconds"] = 120
    env.bridge.refreshAccount(ident)
    assert env.accounts.row(ident)["login_state"] == "checking"
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.accounts.records[-1][2] is False
    assert "2" in env.bridge.accountsModel._items[0]["quotaText"]


def test_active_account_cannot_relogin_rename_or_remove(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["in_flight"] = 2
    start_login(env, ident)
    env.bridge.refreshAccount(ident)
    env.bridge.removeAccount(ident)
    env.bridge.renameAccount(ident, "blocked")
    assert not env.runtime.calls
    assert env.accounts.row(ident)["alias"] == "الحساب الأول"


def test_auth_failure_does_not_echo_native_exception_secrets(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.begin = RuntimeError("access_token=SECRET_TOKEN https://auth.openai.com/?state=PRIVATE_STATE")
    notices = []
    env.bridge.notice.connect(lambda message, error: notices.append(message))
    start_login(env, ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    visible = json.dumps(env.bridge.accountsModel._items) + str(notices) + str(env.store.events(100))
    assert "SECRET_TOKEN" not in visible and "PRIVATE_STATE" not in visible


def test_autostop_waits_for_login_and_close_invalidates_late_callbacks(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.gate = threading.Event()
    start_login(env, ident)
    env.scheduler.running = True
    env.bridge._auto_stop = env.bridge._auto_stop_seen = True
    reached = []
    env.bridge.autoStopReached.connect(lambda: reached.append(True))
    env.bridge.refresh()
    assert not reached
    env.bridge.closeAccounts()
    env.runtime.gate.set()
    pump(env.app, lambda: not env.bridge._account_pending)
    assert not env.opened and not env.accounts.records


def test_active_auth_close_can_be_declined_and_update_installer_waits(account_ui, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    env = account_ui
    ident = add_account(env)
    env.runtime.gate = threading.Event()
    start_login(env, ident)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: asked.append(True) or QMessageBox.StandardButton.No)
    assert env.bridge.prepareClose() is False
    assert asked and not env.bridge._accounts_closing
    env.bridge._app_update["status"] = "downloaded"
    env.bridge._update_installer = env.config.home / "fake-installer.exe"
    env.bridge._update_release = {"version": "1.0.0"}
    def no_installer(*args):
        raise AssertionError("Installer must not be verified/launched during authentication")
    monkeypatch.setattr("waraq.bridge.app_update.verify_installer", no_installer)
    notices = []
    env.bridge.notice.connect(lambda message, error: notices.append((message, error)))
    env.bridge.installAppUpdate()
    assert notices and notices[-1][1] is True
    env.bridge.cancelAccountLogin(ident)
    env.runtime.gate.set()
    pump(env.app, lambda: not env.bridge._account_operation_busy())


def test_uncertain_session_recheck_failure_preserves_execution_hold(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["health"] = "execution_uncertain"
    env.accounts.row(ident)["login_state"] = "authenticated"
    def cannot_prove_stopped(account_id):
        raise ValueError("still running: SECRET_TOKEN")
    env.scheduler.recheck_account = cannot_prove_stopped
    env.bridge.recheckAccount(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.accounts.row(ident)["health"] == "execution_uncertain"
    assert env.accounts.row(ident)["login_state"] == "authenticated"
    assert "SECRET_TOKEN" not in str(env.bridge.accountsModel._items)


@pytest.mark.parametrize("health,expected", [("model_limited", "بعض النماذج"), ("cooldown", "بانتظار تجدد"),
                                            ("probe", "لم يُثبت تجدده"), ("healthy", "مسجّل الدخول")])
def test_health_status_distinguishes_limits_without_claiming_remaining_quota(account_ui, health, expected):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident).update(login_state="authenticated", health=health)
    env.bridge.refresh()
    row = env.bridge.accountsModel._items[0]
    assert expected in row["statusText"]
    assert row["quotaText"] == "الحصة المتبقية غير معروفة"
    assert "%" not in row["statusText"] + row["quotaText"]
    assert not env.runtime.calls


def test_usage_is_queued_independent_of_auth_busy_and_active_jobs(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident).update(login_state="authenticated", in_flight=2)
    monitor = attach_usage(env)
    resets = []
    env.bridge.accountsModel.modelReset.connect(lambda: resets.append(True))
    env.bridge.refreshAccountUsage(ident)
    env.bridge.refreshAccountUsage(ident)
    assert monitor.requests == [(ident, True)]
    assert env.bridge.accountsModel._items[0]["usageRefreshing"]
    assert not env.bridge.accountBusy and not env.bridge._account_pending and not env.runtime.calls
    # Polling cache for desktop or phone must never schedule extra reads.
    env.bridge.refresh()
    env.bridge.internet_commands._snapshot()
    assert monitor.requests == [(ident, True)] and monitor.scheduled == 0
    monitor.gate.set()
    for worker in monitor.workers:
        worker.join(1)
    assert not monitor.applied_threads  # Signal reached GUI only after event pumping.
    pump(env.app, lambda: not monitor.pending)
    assert monitor.applied_threads == [threading.get_ident()]
    row = env.bridge.accountsModel._items[0]
    assert row["usage"]["fiveHour"]["remainingPercent"] == 0
    assert not row["usageRefreshing"] and row["inFlight"] == 2
    assert env.accounts.row(ident)["login_state"] == "authenticated"
    assert not resets


@pytest.mark.parametrize("action", ["login", "remove", "close"])
def test_usage_callbacks_cannot_survive_auth_invalidation_or_close(account_ui, action):
    env = account_ui
    ident = add_account(env)
    monitor = attach_usage(env)
    env.bridge.refreshAccountUsage(ident)
    if action == "login":
        start_login(env, ident)
    elif action == "remove":
        env.bridge.removeAccount(ident)
    else:
        start = time.monotonic()
        env.bridge.closeAccounts()
        assert time.monotonic() - start < 0.2
        assert monitor.stopped and not env.bridge._account_usage_timer.isActive()
    monitor.gate.set()
    for worker in monitor.workers:
        worker.join(1)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert ident not in monitor.snapshots


def test_usage_refresh_never_holds_autostop_and_metadata_keeps_usage_epoch(account_ui):
    env = account_ui
    ident = add_account(env)
    monitor = attach_usage(env)
    env.bridge.refreshAccountUsage(ident)
    env.bridge.refreshAccount(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert monitor.epochs == {}, "metadata check is not a new authentication identity"
    reached = []
    env.bridge.autoStopReached.connect(lambda: reached.append(True))
    env.scheduler.running = True
    env.bridge._auto_stop = env.bridge._auto_stop_seen = True
    env.bridge.refresh()
    assert reached and monitor.pending and not env.bridge.accountBusy
    monitor.gate.set()
    for worker in monitor.workers:
        worker.join(1)
    pump(env.app, lambda: not monitor.pending)


@pytest.mark.parametrize("action", ["loginAccount", "removeAccount"])
def test_usage_maintenance_keeps_cache_and_auth_action_available_after_reservation(account_ui, action):
    env = account_ui
    ident = add_account(env)
    monitor = attach_usage(env)
    monitor.snapshots[ident] = phone_usage_fixtures()[0]["usage"]
    before = copy.deepcopy(monitor.snapshots)
    monitor.maintenance.add(ident)
    env.bridge.refresh()
    row = env.bridge.accountsModel._items[0]
    assert row["busy"] and row["usageMaintenance"] and not env.bridge.accountBusy
    getattr(env.bridge, action)(ident)
    assert not env.runtime.calls and not env.bridge.accountBusy
    assert monitor.snapshots == before and monitor.epochs == {}
    monitor.maintenance.clear()
    env.bridge.refresh()
    assert not env.bridge.accountsModel._items[0]["busy"]
    getattr(env.bridge, action)(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    if action == "loginAccount":
        env.bridge.chooseAccountLoginMethod(ident, "chatgpt-browser")
        pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.runtime.calls and monitor.epochs[ident] == 1


def test_login_choice_cancel_listing_failure_and_untrusted_methods_preserve_current_auth(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident).update(login_state="authenticated", identity="stable-account", health="healthy")
    monitor = attach_usage(env)
    monitor.snapshots[ident] = phone_usage_fixtures()[0]["usage"]
    before, cached = copy.deepcopy(env.accounts.row(ident)), copy.deepcopy(monitor.snapshots)
    env.runtime.method_gate = threading.Event()
    env.bridge.loginAccount(ident)
    assert env.bridge.accountLoginChoice["loading"] and env.bridge.accountBusy
    assert env.accounts.row(ident) == before and monitor.snapshots == cached
    env.bridge.cancelAccountLoginChoice()
    env.runtime.method_gate.set()
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.bridge.accountLoginChoice == {} and env.accounts.row(ident) == before
    assert not env.runtime.calls and not env.opened and monitor.epochs == {}
    env.runtime.methods = RuntimeError("SECRET_TOKEN http://private.invalid")
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.bridge.accountLoginChoice["error"] and env.bridge.accountLoginChoice["methods"] == []
    assert "SECRET_TOKEN" not in str(env.bridge.accountLoginChoice)
    assert env.accounts.row(ident) == before and monitor.snapshots == cached
    env.bridge.cancelAccountLoginChoice()
    env.runtime.methods = [{"id": "evil-method", "type": "oauth", "label": "SECRET_TOKEN"}]
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    env.bridge.chooseAccountLoginMethod(ident, "evil-method")
    assert env.accounts.row(ident) == before and not env.runtime.calls


@pytest.mark.parametrize("method", ["chatgpt-browser", "chatgpt-token-sharing"])
def test_explicit_method_selection_and_active_lease_recheck(account_ui, method):
    env = account_ui
    ident = add_account(env)
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert not env.runtime.calls and not env.opened
    env.accounts.row(ident)["in_flight"] = 1
    env.bridge.chooseAccountLoginMethod(ident, method)
    assert not env.runtime.calls and env.accounts.row(ident)["login_state"] == "signed_out"
    assert "انتظر" in env.bridge.accountLoginChoice["error"]
    env.accounts.row(ident)["in_flight"] = 0
    env.bridge.chooseAccountLoginMethod(ident, method)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.runtime.selected_methods == [method] and len(env.opened) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_cancelled_or_failed_reauthorization_restores_verified_existing_login(account_ui, failure):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident).update(login_state="authenticated", identity="stable-account", health="healthy")
    env.runtime.begin["mode"] = "code"
    if failure:
        env.runtime.begin = ValueError("selected method unavailable SECRET_TOKEN")
    start_login(env, ident)
    if not failure:
        pump(env.app, lambda: ident in env.bridge._account_logins)
        env.bridge.cancelAccountLogin(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.accounts.row(ident)["login_state"] == "authenticated"
    assert env.accounts.records[-1][2] is False
    assert any(call[0] == "metadata" for call in env.runtime.calls)
    assert not any(call[0] == "delete" for call in env.runtime.calls)
    assert "SECRET_TOKEN" not in str(env.bridge.accountsModel._items)


def test_cancelled_reauthorization_does_not_claim_auth_when_metadata_is_unverified(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident).update(login_state="authenticated", identity="stable-account", health="healthy")
    env.runtime.begin["mode"] = "code"
    start_login(env, ident)
    pump(env.app, lambda: ident in env.bridge._account_logins)
    env.runtime.finish = {"authenticated": False}
    env.bridge.cancelAccountLogin(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.accounts.row(ident)["login_state"] == "needsverify"
    assert env.bridge.accountsModel._items[0]["error"]


def test_deleted_account_rejects_late_method_catalog(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.method_gate = threading.Event()
    env.bridge.loginAccount(ident)
    env.accounts.remove(ident)
    env.runtime.method_gate.set()
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert not env.bridge.accountLoginChoice and not env.runtime.calls and not env.opened


@pytest.mark.parametrize("reason,wait", [("usage_unsupported", 0), ("refresh_wait", 17), ("managed_accounts_disabled", 0)])
def test_manual_usage_rejection_is_controlled_and_browser_action_is_fixed(account_ui, reason, wait):
    env = account_ui
    ident = add_account(env)
    monitor = attach_usage(env)
    snapshot = monitor.public_snapshot(ident)
    snapshot.update(canRefresh=False, refreshBlockReason=reason, refreshWaitSeconds=wait)
    monitor.snapshots[ident] = snapshot
    messages = []
    env.bridge.notice.connect(lambda message, error: messages.append(message))
    env.bridge.refreshAccountUsage(ident)
    assert not monitor.requests and messages
    if wait:
        assert "17" in messages[-1]
    env.bridge.refreshAllAccountUsage()
    assert "لم يبدأ" in messages[-1] or "بدأ تحديث" in messages[-1]
    env.bridge.manageAccountUsage()
    assert env.opened[-1] == "https://chatgpt.com/settings/usage"
    assert "صراحةً" in messages[-1]


def test_open_login_choice_holds_auto_stop_without_changing_auth(account_ui):
    env = account_ui
    ident = add_account(env)
    before = copy.deepcopy(env.accounts.row(ident))
    reached = []
    env.bridge.autoStopReached.connect(lambda: reached.append(True))
    env.scheduler.running = True
    env.bridge._auto_stop = env.bridge._auto_stop_seen = True
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge._account_operation_busy())
    assert env.bridge.accountBusy and env.bridge.accountLoginChoice["methods"]
    env.bridge.refresh()
    assert not reached and env.accounts.row(ident) == before
    assert not env.runtime.calls and not env.opened
    env.bridge.cancelAccountLoginChoice()
    env.bridge.refresh()
    assert reached and not env.bridge.accountBusy
