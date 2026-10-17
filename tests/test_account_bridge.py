"""Account UI orchestration uses native-runtime doubles: no authentication/network."""
from __future__ import annotations

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

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

    def begin_login(self, ident):
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


def pump(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), "background account operation did not settle"


@pytest.fixture
def account_ui(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
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
        store.db.close()


def add_account(env):
    env.bridge.addAccount("الحساب الأول")
    return env.accounts.rows[-1]["id"]


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
    env.bridge.loginAccount(ident)
    assert env.bridge.accountBusy
    assert not env.opened  # the worker may finish, but only a queued GUI callback can open the browser
    pump(env.app, lambda: not env.bridge.accountBusy)
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
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert not env.opened
    assert env.bridge.accountsModel._items[0]["error"]
    assert any(call[0] == "cancel" for call in env.runtime.calls)


def test_cancel_rejects_late_begin_callback_and_duplicate_actions(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.gate = threading.Event()
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: bool(env.runtime.calls))
    env.bridge.loginAccount(ident)
    env.bridge.removeAccount(ident)
    assert len(env.runtime.calls) == 1
    env.bridge.cancelAccountLogin(ident)
    pump(env.app, lambda: any(call[0] == "cancel" for call in env.runtime.calls))
    env.runtime.gate.set()
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert not env.opened and not env.accounts.records
    assert env.accounts.row(ident)["login_state"] == "signed_out"


def test_manual_code_field_only_required_by_native_method(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.begin["mode"] = "code"
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: bool(env.bridge._account_logins))
    assert env.bridge.accountsModel._items[0]["needsCode"]
    assert not any(call[0] == "finish" for call in env.runtime.calls)
    env.bridge.finishAccountLogin(ident, "")
    assert not any(call[0] == "finish" for call in env.runtime.calls)
    env.bridge.finishAccountLogin(ident, "private-code")
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert "private-code" not in json.dumps(env.bridge.accountsModel._items)


def test_metadata_reserves_account_and_preserves_observed_cooldown(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["cooldown_seconds"] = 120
    env.bridge.refreshAccount(ident)
    assert env.accounts.row(ident)["login_state"] == "checking"
    pump(env.app, lambda: not env.bridge.accountBusy)
    assert env.accounts.records[-1][2] is False
    assert "2" in env.bridge.accountsModel._items[0]["quotaText"]


def test_active_account_cannot_relogin_rename_or_remove(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["in_flight"] = 2
    env.bridge.loginAccount(ident)
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
    env.bridge.loginAccount(ident)
    pump(env.app, lambda: not env.bridge.accountBusy)
    visible = json.dumps(env.bridge.accountsModel._items) + str(notices) + str(env.store.events(100))
    assert "SECRET_TOKEN" not in visible and "PRIVATE_STATE" not in visible


def test_autostop_waits_for_login_and_close_invalidates_late_callbacks(account_ui):
    env = account_ui
    ident = add_account(env)
    env.runtime.gate = threading.Event()
    env.bridge.loginAccount(ident)
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
    env.bridge.loginAccount(ident)
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
    pump(env.app, lambda: not env.bridge.accountBusy)


def test_uncertain_session_recheck_failure_preserves_execution_hold(account_ui):
    env = account_ui
    ident = add_account(env)
    env.accounts.row(ident)["health"] = "execution_uncertain"
    env.accounts.row(ident)["login_state"] = "authenticated"
    def cannot_prove_stopped(account_id):
        raise ValueError("still running: SECRET_TOKEN")
    env.scheduler.recheck_account = cannot_prove_stopped
    env.bridge.recheckAccount(ident)
    pump(env.app, lambda: not env.bridge.accountBusy)
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
