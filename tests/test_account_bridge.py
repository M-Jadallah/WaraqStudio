"""Embedded account bridge acceptance with cached synthetic data; no OAuth/network."""
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

    def refresh_usage(self, account_id=None, manual=False):
        self._called("usage")
        return self.list()

    def acknowledge_migration(self):
        self._called("acknowledge")
        self.status["migration_required"] = False

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
    assert Bridge._safe_account_result("methods", [{"id": "browser", "type": "oauth", "label": "Login", "secret": "SECRET"}]) == [
        {"id": "browser", "type": "oauth", "label": "Login"}]


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
