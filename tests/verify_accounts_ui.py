"""Native account panel acceptance; synthetic runtime only, no real credentials."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPoint, QUrl, Qt, qInstallMessageHandler
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from scheduler_test_support import SchedulerHarness
from test_account_bridge import Runtime
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


def main():
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    with tempfile.TemporaryDirectory(prefix="waraq-accounts-ui-", ignore_cleanup_errors=True) as temporary, SchedulerHarness() as harness:
        home = Path(temporary)
        config, store = Config(home), Store(home / "ui.db")
        scheduler = Scheduler(store, config, None)
        native_runtime = scheduler.account_runtime
        runtime = Runtime()
        scheduler.account_runtime = runtime
        bridge = Bridge(store, config, None, scheduler)
        bridge.timer.stop()
        opened = []
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        root.setProperty("page", 2)
        QTest.qWait(150)

        def find_all(item, name):
            found = [item] if item.objectName() == name else []
            for child in item.childItems():
                found.extend(find_all(child, name))
            return found

        def find(name, parent=None):
            matches = find_all(parent or root.contentItem(), name)
            assert matches, name
            return matches[0]

        def click(name, parent=None):
            item = find(name, parent)
            assert item.isVisible() and item.property("enabled"), name
            position = item.mapToScene(item.boundingRect().center())
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(int(position.x()), int(position.y())))
            QTest.qWait(45)

        def settle(predicate, timeout=3):
            deadline = time.monotonic() + timeout
            while not predicate() and time.monotonic() < deadline:
                QTest.qWait(15)
            assert predicate(), "account action did not finish"

        with patch("waraq.bridge.QDesktopServices.openUrl", lambda url: opened.append(url.toString()) or True):
            assert not runtime.calls and not config.data["chatgpt_accounts_enabled"]
            click("managedAccountsEnabled")
            assert config.data["chatgpt_accounts_enabled"]
            for alias in ("الحساب الأول", "الحساب الثاني", "حساب ثالث بأولوية أدنى"):
                find("accountAliasField").setProperty("text", alias)
                click("addAccountButton")
            assert bridge.accountsModel.rowCount() == 3
            assert not runtime.calls
            ident = scheduler.accounts.list()[0]["id"]
            first = find("accountCard")
            runtime.begin["mode"] = "code"
            click("accountLoginButton", first)
            settle(lambda: bool(bridge._account_logins))
            assert len(opened) == 1 and bridge.accountsModel._items[0]["needsCode"]
            assert not find("accountRemoveButton", first).property("enabled")
            click("accountCancelLoginButton", first)
            settle(lambda: not bridge.accountBusy)
            runtime.begin["mode"] = "auto"
            click("accountLoginButton", first)
            settle(lambda: not bridge.accountBusy)
            assert scheduler.accounts.list()[0]["login_state"] == "authenticated"
            click("accountRefreshButton", first)
            settle(lambda: not bridge.accountBusy)
            assert "Plus" in find("accountStatus", first).property("text")
            assert "غير معروفة" in find("accountStatus", first).property("text")
            click("accountPreferButton", first)
            assert scheduler.accounts.list()[0]["preferred"]
            click("accountDownButton", first)
            assert scheduler.accounts.list()[1]["id"] == ident
            # A refresh changes rows in place rather than resetting the list or scroll.
            resets = []
            bridge.accountsModel.modelReset.connect(lambda: resets.append(True))
            bridge.refresh()
            assert not resets
            # Exercise long names, maximum text scale and compact/comfortable themes.
            scheduler.accounts.update(scheduler.accounts.list()[0]["id"], alias="الحساب المخصص لمعالجة الدروس العربية ومتابعة الملفات والتحقق من النتائج العلمية")
            bridge.refresh()
            screenshots = Path(tempfile.gettempdir()) / "waraq-account-ui-validation"
            screenshots.mkdir(exist_ok=True)
            for theme, scale, compact in (("dark", 1.0, True), ("light", 1.4, False), ("dark", 1.4, True)):
                config.save({"theme": theme, "ui_scale": scale, "ui_compact": compact})
                bridge.settingsChanged.emit()
                root.resize(1060, 720)
                QTest.qWait(200)
                panel = find("chatgptAccountsPanel")
                assert panel.property("height") > 0
                workers = find("workerList")
                assert workers.property("height") >= 70, (theme, scale, workers.property("height"))
                for card in find_all(root.contentItem(), "accountCard"):
                    content = find("accountContent", card)
                    assert content.property("width") > 0
                    for label_name in ("accountStatus", "accountError", "accountAliasLabel"):
                        label = find(label_name, card)
                        if label.isVisible():
                            assert label.property("y") + label.property("height") <= content.property("height") + 1
                    for button in find_all(card, "accountRemoveButton") + find_all(card, "accountLoginButton"):
                        assert button.property("x") + button.property("width") <= content.property("width") + 1
                assert root.grabWindow().save(str(screenshots / f"accounts-{theme}-{scale}-{compact}.png"))
                scroller = find("workersPageScroll").property("contentItem")
                scroller.setProperty("contentY", max(0, scroller.property("contentHeight") - scroller.property("height")))
                QTest.qWait(75)
                worker_position = workers.mapToScene(workers.boundingRect().center())
                assert 0 < worker_position.y() < root.height(), "worker list must remain reachable by scrolling"
                assert root.grabWindow().save(str(screenshots / f"workers-{theme}-{scale}-{compact}.png"))
                scroller.setProperty("contentY", 0)
                QTest.qWait(50)
            config.save({"theme": "dark", "ui_scale": 1.0, "ui_compact": True})
            bridge.settingsChanged.emit()
            QTest.qWait(100)
            first = find("accountCard")
            click("accountRenameButton", first)
            find("accountRenameField").setProperty("text", "اسم جديد")
            click("accountRenameSaveButton")
            assert scheduler.accounts.list()[0]["alias"] == "اسم جديد"
            click("accountRemoveButton", first)
            assert not any(call[0] == "delete" for call in runtime.calls)
            click("accountRemoveCancelButton")
            assert len(scheduler.accounts.list()) == 3
            click("accountRemoveButton", first)
            click("accountRemoveConfirmButton")
            settle(lambda: not bridge.accountBusy)
            assert len(scheduler.accounts.list()) == 2
            errors = [message for message in messages if any(word in message for word in ("Error", "ReferenceError", "TypeError", "Binding loop", "Unable to assign"))]
            assert not errors, errors
        bridge.closeAccounts()
        scheduler.account_runtime = native_runtime
        harness.shutdown_and_join()
        engine.deleteLater()
        app.processEvents()
        store.db.close()
    print("Native accounts UI passed: opt-in, three accounts, OAuth browser/cancel/login, metadata, priority/preference, stable model, dark/light, minimum1060x720 and scale1.4; no native auth/network. Screenshots:", screenshots)


if __name__ == "__main__":
    main()
