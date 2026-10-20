"""Native quota edge cases and frozen-column live-model acceptance; no network."""
from __future__ import annotations

import sys
import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QPointF, QMetaObject, Qt, QUrl, qInstallMessageHandler
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from test_account_bridge import EmbeddedAccountsDouble, phone_usage_fixtures
from qt_test_support import dispose_bridge
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.store import Store


ROOT = Path(__file__).resolve().parents[1]


def main():
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    engine = QQmlApplicationEngine()
    theme_component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "waraq/qml/Theme.qml")))
    theme = theme_component.create()
    quota_component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "waraq/qml/AccountQuotaCell.qml")))
    quota = quota_component.createWithInitialProperties({"theme": theme})
    assert quota is not None, [error.toString() for error in quota_component.errors()]
    cases = [
        ({"status": "available", "remainingPercent": 0}, True, "0٪", True),
        ({"status": "available", "remainingPercent": 43.666}, True, "43.7٪", False),
        ({"status": "stale", "remainingPercent": 15}, True, "15٪", True),
        ({"status": "available", "remainingPercent": 100}, True, "100٪", False),
        ({"status": "available", "remainingPercent": -1}, False, "غير معروف", False),
        ({"status": "available", "remainingPercent": 101}, False, "غير معروف", False),
        ({"status": "available", "remainingPercent": float("nan")}, False, "غير معروف", False),
        ({"status": "available", "remainingPercent": float("inf")}, False, "غير معروف", False),
        ({"status": "available", "remainingPercent": "40"}, False, "غير معروف", False),
        ({"status": "unsupported", "remainingPercent": 40}, False, "غير معروف", False),
        ({"status": "unavailable"}, False, "غير معروف", False),
    ]
    for data, known, text, attention in cases:
        quota.setProperty("windowData", data)
        assert quota.property("known") == known, data
        assert quota.property("percentText") == text, data
        assert quota.property("attention") == attention, data
        assert quota.property("stale") == (data["status"] == "stale"), data
    quota.deleteLater()
    theme.deleteLater()
    app.processEvents()

    with tempfile.TemporaryDirectory(prefix="waraq-table-ui-") as temporary:
        home = Path(temporary)
        config, store = Config(home), Store(home / "ui.db")
        accounts = EmbeddedAccountsDouble()
        for index in range(12):
            row = accounts.create("حساب " + str(index + 1))
            row["usage"] = phone_usage_fixtures()[index % 4]["usage"]
        scheduler = SimpleNamespace(accounts=accounts, running=False, workers=lambda: [], idle=lambda: True,
                                    disabled_workers={}, forget_keys=lambda: None, shutdown=lambda: None,
                                    library=SimpleNamespace())
        scheduler.recheck_account = lambda ident: accounts.recheck(ident) if ident else accounts.refresh()
        bridge = Bridge(store, config, None, scheduler)
        bridge.timer.stop()
        bridge._account_usage_timer.stop()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(ROOT / "waraq/qml/Main.qml")))
        root = engine.rootObjects()[0] if engine.rootObjects() else None
        try:
            assert root is not None, messages
            root.setProperty("page", 2)
            root.setProperty("width", 1060)
            QTest.qWait(150)

            def find(name, owner=None):
                origin = owner or root
                item = origin.findChild(QObject, name)
                pending = [origin.contentItem() if origin is root else origin]
                while item is None and pending:
                    candidate = pending.pop()
                    if candidate.objectName() == name:
                        item = candidate
                        break
                    if hasattr(candidate, "childItems"):
                        pending.extend(reversed(candidate.childItems()))
                assert item is not None, name
                return item

            table = find("accountTable")
            listing = find("accountList")
            scroller = find("accountTableHorizontalScroll")
            assert listing.property("count") == 12
            assert table.property("middleWidth") > table.property("viewportWidth")
            card = find("accountCard")
            identity = find("accountIdentityCell", card)
            actions = find("accountActionsCell", card)
            middle = find("accountMiddleColumns", card)
            identity_x, actions_x = identity.property("x"), actions.property("x")
            scroller.setProperty("contentX", 0)
            QTest.qWait(30)
            identity_x, actions_x = identity.property("x"), actions.property("x")
            start = middle.property("x")
            scroller.setProperty("contentX", 80)
            QTest.qWait(30)
            assert abs(middle.property("x") - start + 80) < 1
            assert identity.property("x") == identity_x, (identity_x, identity.property("x"), card.property("width"), table.property("width"))
            assert actions.property("x") == actions_x
            assert abs(identity_x - (card.property("width") - table.property("identityWidth"))) < 1
            assert actions_x == 0

            # Exercise real keyboard and pointer events, not just QML setters.
            bar = find("accountHorizontalScrollBar")
            bar.forceActiveFocus()
            assert bar.property("activeFocus")
            QTest.keyClick(root, Qt.Key_Home)
            assert scroller.property("contentX") == 0
            QTest.keyClick(root, Qt.Key_Right)
            assert scroller.property("contentX") > 0
            QTest.keyClick(root, Qt.Key_End)
            maximum = scroller.property("contentWidth") - scroller.property("width")
            assert abs(scroller.property("contentX") - maximum) < 1
            QTest.keyClick(root, Qt.Key_Left)
            assert scroller.property("contentX") < maximum
            QTest.keyClick(root, Qt.Key_End)
            point = bar.mapToScene(QPointF(2, bar.property("height") / 2)).toPoint()
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, point)
            QTest.qWait(40)
            assert scroller.property("contentX") < maximum

            # Partial data must remain independently known/stale for each window.
            five = find("accountFiveHourQuotaCell", card)
            weekly = find("accountWeeklyQuotaCell", card)
            assert five.property("known") and five.property("remaining") == 0
            assert weekly.property("known") and weekly.property("remaining") == 43

            QMetaObject.invokeMethod(find("accountDetailsButton", card), "clicked")
            QTest.qWait(30)
            assert card.property("detailsOpen")
            assert find("accountContent", card).property("visible")
            for name in ("accountUsageFiveHour", "accountUsageWeekly", "accountUsageUpdated",
                         "accountUsageMessage", "accountUsageRefreshReason", "accountDeviceCode"):
                find(name, card)

            # Details stays available in the fixed action column at either end.
            details = find("accountDetailsButton", card)
            assert details.parentItem().parentItem().objectName() == "accountActionsCell"
            card.setProperty("detailsOpen", False)
            listing.setProperty("contentY", 0)
            more = find("accountMoreButton", card)
            more.forceActiveFocus()
            QTest.keyClick(root, Qt.Key_Space)
            QTest.qWait(30)
            popup = find("accountActionsMenu", card)
            assert popup.property("visible")
            assert find("accountLoginButton", card).property("activeFocus")
            QTest.keyClick(root, Qt.Key_Tab)
            assert find("accountManageUsageButton", card).property("activeFocus")
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(30)
            assert not popup.property("visible")

            # At the supported minimum window and 140%, popup positioning must
            # keep the entire action set visible for top and bottom rows.
            data = config.snapshot()
            data.update(ui_scale=1.4)
            config.save(data)
            bridge.settingsChanged.emit()
            root.setProperty("width", 1060)
            root.setProperty("height", 720)
            QTest.qWait(120)
            page_flickable = find("workersPageScroll").property("contentItem")
            page_flickable.setProperty("contentY", max(0, listing.mapToScene(QPointF()).y() - 300))
            QTest.qWait(40)
            for name in ("accountRefreshButton", "accountMoreButton", "accountUsageRefreshButton", "accountDetailsButton"):
                control = find(name, card)
                position = control.mapToScene(QPointF())
                assert 0 <= position.x() and position.x() + control.property("width") <= 1060
                assert 0 <= position.y() and position.y() + control.property("height") <= 720
            for scroll_y in (0, 270):
                listing.setProperty("contentY", scroll_y)
                QTest.qWait(40)
                pending = [listing]
                visible_buttons = []
                while pending:
                    item = pending.pop()
                    if item.objectName() == "accountMoreButton" and item.isVisible():
                        position = item.mapToScene(QPointF(0, 0))
                        if 0 <= position.y() and position.y() + item.property("height") <= 720:
                            visible_buttons.append(item)
                    if hasattr(item, "childItems"):
                        pending.extend(item.childItems())
                assert visible_buttons, "minimum-window account actions were inaccessible"
                button = min(visible_buttons, key=lambda item: item.mapToScene(QPointF()).y()) if scroll_y == 0 else max(visible_buttons, key=lambda item: item.mapToScene(QPointF()).y())
                button.forceActiveFocus()
                QTest.keyClick(root, Qt.Key_Space)
                QTest.qWait(40)
                owner = button.parentItem()
                while owner.objectName() != "accountCard":
                    owner = owner.parentItem()
                active_popup = find("accountActionsMenu", owner)
                assert active_popup.property("visible")
                content = active_popup.property("contentItem")
                position = content.mapToScene(QPointF(0, 0))
                padding = active_popup.property("padding")
                assert position.x() - padding >= -1
                assert position.y() - padding >= -1
                assert position.x() + content.property("width") + padding <= 1061
                assert position.y() + content.property("height") + padding <= 721
                names = set()
                for _ in range(9):
                    focused = root.activeFocusItem()
                    names.add(focused.objectName())
                    point = focused.mapToScene(QPointF(focused.property("width") / 2, focused.property("height") / 2))
                    assert 0 <= point.x() <= 1060 and 0 <= point.y() <= 720
                    QTest.keyClick(root, Qt.Key_Tab)
                assert {"accountLoginButton", "accountManageUsageButton", "accountRenameButton", "accountRemoveButton"}.issubset(names), names
                QTest.keyClick(root, Qt.Key_Escape)
                QTest.qWait(30)
                assert button.property("activeFocus")
            page_flickable.setProperty("contentY", 0)
            QTest.qWait(40)
            help_button = find("accountHelpButton")
            help_button.forceActiveFocus()
            QTest.keyClick(root, Qt.Key_Space)
            QTest.qWait(40)
            help_popup = find("accountHelpPopup")
            assert help_popup.property("visible")
            help_content = help_popup.property("contentItem")
            position = help_content.mapToScene(QPointF())
            padding = help_popup.property("padding")
            assert position.x() - padding >= -1 and position.y() - padding >= -1
            assert position.x() + help_content.property("width") + padding <= 1061
            assert position.y() + help_content.property("height") + padding <= 721
            assert find("accountHelpCloseButton").property("activeFocus")
            QTest.keyClick(root, Qt.Key_Tab)
            assert find("accountHelpCloseButton").property("activeFocus")
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(30)
            assert help_button.property("activeFocus")
            data.update(ui_scale=1.0)
            config.save(data)
            bridge.settingsChanged.emit()
            root.setProperty("height", 900)
            listing.setProperty("contentY", 0)
            QTest.qWait(80)
            more.forceActiveFocus()
            QTest.keyClick(root, Qt.Key_Space)
            QTest.qWait(30)
            rename = find("accountRenameButton", card)
            point = rename.mapToScene(QPointF(rename.property("width") / 2, rename.property("height") / 2)).toPoint()
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, point)
            QTest.qWait(40)
            assert not popup.property("visible")
            assert find("renameAccountDialog").property("visible")
            QTest.keyClick(root, Qt.Key_Escape)

            # Live updates keep the vertical viewport and never reset the model.
            listing.setProperty("contentY", 90)
            QTest.qWait(30)
            before = listing.property("contentY")
            resets = QSignalSpy(bridge.accountsModel.modelReset)
            accounts.rows[0]["usage"]["fiveHour"]["remainingPercent"] = 37
            bridge._publish_accounts()
            QTest.qWait(50)
            assert resets.count() == 0
            assert abs(listing.property("contentY") - before) < 2
            assert five.property("remaining") == 37

            # Rebinding a row after a reorder closes its old-account action popup.
            QMetaObject.invokeMethod(find("accountMoreButton", card), "clicked")
            QTest.qWait(30)
            popup = find("accountActionsMenu", card)
            assert popup.property("visible")
            accounts.rows.reverse()
            bridge._publish_accounts()
            QTest.qWait(60)
            assert not popup.property("visible")

            if "--screenshots" in sys.argv:
                card.setProperty("detailsOpen", False)
                accounts.rows = accounts.rows[-4:][::-1]
                accounts.status["migration_required"] = False
                for index, row in enumerate(accounts.rows):
                    row.update(alias=["الحساب الرئيسي", "حساب التفريغ", "حساب المراجعة", "حساب جديد"][index],
                               login_state="authenticated" if index < 3 else "signed_out",
                               email="account" + str(index + 1) + "@example.test", preferred=index == 0)
                    row["usage"] = copy.deepcopy(phone_usage_fixtures()[index]["usage"])
                accounts.rows[0]["usage"]["fiveHour"]["remainingPercent"] = 85
                accounts.rows[1]["usage"]["fiveHour"].update(status="available", remainingPercent=23.4)
                accounts.rows[1]["usage"]["weekly"].update(status="available", remainingPercent=12.5, resetsAt=1800604800)
                accounts.rows[2]["usage"] = copy.deepcopy(phone_usage_fixtures()[1]["usage"])
                accounts.rows[2]["usage"]["fiveHour"]["remainingPercent"] = 61
                bridge._publish_accounts()
                root.setProperty("width", 1380)
                root.setProperty("height", 900)
                output = ROOT / "output/account-table-native"
                output.mkdir(parents=True, exist_ok=True)
                for theme_name, scale in (("dark", 1.0), ("light", 1.0), ("dark", 1.4), ("light", 1.4)):
                    data = config.snapshot()
                    data.update(theme=theme_name, ui_scale=scale)
                    config.save(data)
                    bridge.settingsChanged.emit()
                    listing.setProperty("contentY", 0)
                    find("workersPageScroll").setProperty("contentY", 0)
                    QTest.qWait(180)
                    scroller.setProperty("contentX", max(0, scroller.property("contentWidth") - scroller.property("width")))
                    QTest.qWait(30)
                    root.grabWindow().save(str(output / (theme_name + "-" + str(scale) + ".png")))
                root.setProperty("width", 1060)
                root.setProperty("height", 720)
                QTest.qWait(150)
                root.grabWindow().save(str(output / "light-minimum-1.4.png"))

            bad = [message for message in messages if any(value in message for value in (
                "ReferenceError", "TypeError", "Cannot assign", "Binding loop", "Detected anchors"))]
            assert not bad, bad
            print("Quota data edges, frozen columns, row details, live scroll and popup identity checks passed")
        finally:
            if root is not None:
                root.close()
            engine.deleteLater()
            dispose_bridge(bridge)
            app.processEvents()
            store.db.close()


if __name__ == "__main__":
    main()
