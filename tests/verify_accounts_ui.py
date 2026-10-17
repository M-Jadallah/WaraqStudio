"""Native account panel acceptance; synthetic runtime only, no real credentials."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QPoint, QPointF, QUrl, Qt, qInstallMessageHandler
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from scheduler_test_support import SchedulerHarness
from test_account_bridge import Runtime, UsageMonitorDouble
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
        scheduler.account_usage.stop()
        usage = UsageMonitorDouble()
        scheduler.account_usage = usage
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
            if not matches and parent is None:
                popup = root.findChild(QObject, name)
                if popup:
                    return popup
            assert matches, name
            return matches[0]

        def click(name, parent=None):
            item = find(name, parent)
            assert item.isVisible() and item.property("enabled"), name
            # Quota cards can exceed the viewport at large text sizes. Exercise
            # the same nested list/page scrolling a keyboard or touch user needs.
            ancestor = item.parentItem()
            while ancestor:
                if ancestor.property("contentY") is not None and ancestor.property("contentHeight") is not None:
                    center = item.mapToItem(ancestor, item.boundingRect().center()).y()
                    current = ancestor.property("contentY")
                    limit = max(0, ancestor.property("contentHeight") - ancestor.property("height"))
                    if center < 20 or center > ancestor.property("height") - 20:
                        ancestor.setProperty("contentY", min(limit, max(0, current + center - ancestor.property("height") / 2)))
                        QTest.qWait(45)
                ancestor = ancestor.parentItem()
            position = item.mapToScene(item.boundingRect().center())
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(int(position.x()), int(position.y())))
            QTest.qWait(45)

        def settle(predicate, timeout=3):
            deadline = time.monotonic() + timeout
            while not predicate() and time.monotonic() < deadline:
                QTest.qWait(15)
            assert predicate(), "account action did not finish"

        def choose_method(label):
            settle(lambda: not bridge.accountLoginChoice.get("loading"))
            dialog = find("accountLoginMethodDialog")
            settle(lambda: dialog.property("opened"))
            # A queued catalog result creates delegates before Qt Quick's next
            # polish/render pass. Their initial rectangles can overlap, so a
            # click at the requested label can otherwise hit the last method.
            # Two presented frames also drain any frame already in flight when
            # the catalog arrived; wait for lifecycle signals, not a fixed sleep.
            for _ in range(2):
                presented = QSignalSpy(root.frameSwapped)
                root.update()
                settle(lambda: presented.count() > 0)
            buttons = find_all(dialog.property("contentItem"), "accountLoginMethodButton")
            method_button = next(button for button in buttons if button.property("text") == label)
            position = method_button.mapToScene(method_button.boundingRect().center())
            geometry = [{"label": button.property("text"), "center":
                         (button.mapToScene(button.boundingRect().center()).x(),
                          button.mapToScene(button.boundingRect().center()).y())}
                        for button in buttons]
            assert method_button.isVisible() and method_button.property("enabled"), geometry
            assert not any(button is not method_button and button.isVisible()
                           and button.contains(button.mapFromScene(position)) for button in buttons), geometry
            before = len(runtime.selected_methods)
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(int(position.x()), int(position.y())))
            settle(lambda: len(runtime.selected_methods) > before)
            expected = next(method["id"] for method in runtime.methods if method["label"] == label)
            assert runtime.selected_methods[before:] == [expected], (label, geometry, runtime.selected_methods)

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
            settle(lambda: not bridge._account_operation_busy())
            assert not runtime.calls and not opened
            assert scheduler.accounts.list()[0]["login_state"] == "logged_out"
            assert find("accountLoginMethodDialog").property("visible")
            click("accountLoginChoiceCancelButton")
            assert not bridge.accountLoginChoice and not opened
            click("accountLoginButton", first)
            choose_method("Sign in with ChatGPT")
            settle(lambda: bool(bridge._account_logins))
            assert runtime.selected_methods[-1] == "chatgpt-token-sharing"
            assert len(opened) == 1 and bridge.accountsModel._items[0]["needsCode"]
            assert not find("accountRemoveButton", first).property("enabled")
            click("accountCancelLoginButton", first)
            settle(lambda: not bridge._account_operation_busy())
            runtime.begin["mode"] = "auto"
            click("accountLoginButton", first)
            choose_method("Codex browser (legacy)")
            settle(lambda: not bridge._account_operation_busy())
            assert runtime.selected_methods[-1] == "chatgpt-browser"
            assert scheduler.accounts.list()[0]["login_state"] == "authenticated"
            click("accountRefreshButton", first)
            settle(lambda: not bridge._account_operation_busy())
            assert "Plus" in find("accountStatus", first).property("text")
            assert "غير معروف" in find("accountUsageFiveHour", first).property("text")
            usage.gate.set()
            click("accountUsageRefreshButton", first)
            settle(lambda: not usage.pending)
            assert usage.requests[-1] == (ident, True)
            assert "0٪" in find("accountUsageFiveHour", first).property("text")
            assert "43٪" in find("accountUsageWeekly", first).property("text")
            assert "غير معروف" not in find("accountUsageUpdated", first).property("text")
            usage.snapshots[ident].update(canRefresh=False, refreshWaitSeconds=17, refreshBlockReason="refresh_wait")
            bridge.refresh()
            assert not find("accountUsageRefreshButton", first).property("enabled")
            assert "17" in find("accountUsageRefreshReason", first).property("text")
            click("accountManageUsageButton", first)
            assert opened[-1] == "https://chatgpt.com/settings/usage"
            usage.snapshots[ident].update(canRefresh=True, refreshWaitSeconds=0, refreshBlockReason="")
            bridge.refresh()
            # Manual telemetry remains usable while an account executes work.
            with store.lock:
                lease, _ = scheduler.accounts.acquire("fixture-active-job", "fixture-worker", "model")
            assert lease and lease["account_id"] == ident
            bridge.refresh()
            assert find("accountUsageRefreshButton", first).property("enabled")
            assert not find("accountLoginButton", first).property("enabled")
            scheduler.accounts.release(lease)
            bridge.refresh()
            usage.maintenance.add(ident)
            bridge.refresh()
            assert not find("accountLoginButton", first).property("enabled")
            assert not find("accountRemoveButton", first).property("enabled")
            assert not bridge.accountBusy
            usage.maintenance.clear()
            bridge.refresh()
            assert find("accountLoginButton", first).property("enabled")
            click("accountPreferButton", first)
            assert scheduler.accounts.list()[0]["preferred"]
            click("accountDownButton", first)
            assert scheduler.accounts.list()[1]["id"] == ident
            # A refresh changes rows in place rather than resetting the list or scroll.
            resets = []
            bridge.accountsModel.modelReset.connect(lambda: resets.append(True))
            bridge.refresh()
            assert not resets
            rows = scheduler.accounts.list()
            available = usage.snapshots[ident]
            usage.snapshots[rows[0]["id"]] = {**available, "stale": True, "messageCode": "timeout",
                "fiveHour": {**available["fiveHour"], "status": "stale"},
                "weekly": {"status": "unavailable", "remainingPercent": None, "resetsAt": None, "windowMinutes": None}}
            usage.snapshots[rows[2]["id"]] = {**usage.public_snapshot("unknown"),
                "status": "unsupported", "messageCode": "token_sharing_unsupported", "canRefresh": False,
                "refreshBlockReason": "usage_unsupported"}
            bridge.refresh()
            assert "قراءة قديمة" in find("accountUsageFiveHour").property("text")
            assert "غير معروف" in find("accountUsageWeekly").property("text")
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
                    for label_name in ("accountStatus", "accountError", "accountAliasLabel", "accountUsagePanel"):
                        label = find(label_name, card)
                        if label.isVisible():
                            assert label.property("y") + label.property("height") <= content.property("height") + 1
                    for button in find_all(card, "accountRemoveButton") + find_all(card, "accountLoginButton") + find_all(card, "accountUsageRefreshButton"):
                        assert button.property("x") + button.property("width") <= content.property("width") + 1
                # Present one coherent card in the viewport, without a routine
                # settings toast left over from the action checks above.
                root.setProperty("toastText", "")
                find("accountList").setProperty("contentY", 0)
                scroller = find("workersPageScroll").property("contentItem")
                list_top = find("accountList").mapToItem(scroller, QPointF(0, 0)).y() + scroller.property("contentY")
                scroller.setProperty("contentY", min(list_top, max(0, scroller.property("contentHeight") - scroller.property("height"))))
                QTest.qWait(75)
                assert root.grabWindow().save(str(screenshots / f"accounts-{theme}-{scale}-{compact}.png"))
                scroller.setProperty("contentY", max(0, scroller.property("contentHeight") - scroller.property("height")))
                QTest.qWait(75)
                worker_position = workers.mapToScene(workers.boundingRect().center())
                assert 0 < worker_position.y() < root.height(), "worker list must remain reachable by scrolling"
                assert root.grabWindow().save(str(screenshots / f"workers-{theme}-{scale}-{compact}.png"))
                scroller.setProperty("contentY", 0)
                QTest.qWait(50)
                # Opening and cancelling the method chooser never begins OAuth.
                current = scheduler.accounts.list()[0]
                calls_before = len(runtime.calls)
                bridge.loginAccount(current["id"])
                settle(lambda: not bridge._account_operation_busy())
                dialog = find("accountLoginMethodDialog")
                assert dialog.property("visible") and dialog.property("width") <= root.width() - 20
                assert dialog.property("height") <= root.height() - 20
                explanations = find_all(root.contentItem(), "accountLoginMethodExplanation")
                assert any("ليست ضمانًا" in label.property("text") for label in explanations)
                assert any("غير مدعومة" in label.property("text") for label in explanations)
                root.setProperty("toastText", "")
                assert root.grabWindow().save(str(screenshots / f"login-methods-{theme}-{scale}-{compact}.png"))
                click("accountLoginChoiceCancelButton")
                assert len(runtime.calls) == calls_before
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
            settle(lambda: not bridge._account_operation_busy())
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
