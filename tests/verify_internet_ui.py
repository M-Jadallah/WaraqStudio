"""Native Internet access card acceptance with real Bridge and no external calls.

Run: python tests/verify_internet_ui.py [screenshot-directory]
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QMetaObject, QUrl, qInstallMessageHandler
from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


class Vault:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def main():
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    with tempfile.TemporaryDirectory(prefix="waraq-internet-native-") as temporary, ExitStack() as mocks:
        home = Path(temporary)
        config, store, vault = Config(home), Store(home / "test.db"), Vault()
        scheduler = Scheduler(store, config, vault)
        bridge = Bridge(store, config, vault, scheduler)
        bridge.timer.stop()
        connector = bridge.internet
        calls = []
        for method in ("register", "start", "createPair", "refreshPhones", "approvePair",
                       "rejectPair", "revokePhone", "forgetDevice"):
            mocks.enter_context(patch.object(connector, method,
                side_effect=lambda *args, method=method: calls.append((method, args))))
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(ROOT / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        root.setWidth(1060)
        root.setHeight(720)
        root.setProperty("page", 5)
        QTest.qWait(200)

        def walk(item):
            yield item
            for child in item.childItems():
                yield from walk(child)

        def find(name):
            return next((item for item in walk(root.contentItem()) if item.objectName() == name), None)

        def text_item(value, parent=None):
            return next((item for item in walk(parent or root.contentItem())
                         if item.isVisible() and item.property("text") == value), None)

        def control(name):
            item = find(name)
            assert item is not None, name
            return item

        scroll = control("settingsScroll")
        flick = scroll.property("contentItem")

        def reveal(item):
            assert item.isVisible(), item.objectName()
            QTest.qWait(60)  # Let dynamic LAN/Internet sections finish relayout first.
            origin, viewport = item.mapToScene(QPointF(0, 0)), scroll.mapToScene(QPointF(0, 0))
            maximum = max(0, flick.property("contentHeight") - flick.property("height"))
            target = flick.property("contentY") + origin.y() - viewport.y() - 16
            flick.setProperty("contentY", max(0, min(maximum, target)))
            QTest.qWait(80)
            point = item.mapToScene(item.boundingRect().center())
            assert 0 <= point.x() <= root.width() and 0 <= point.y() <= root.height(), (item.objectName(), point)
            viewport = scroll.mapToScene(QPointF(0, 0))
            top = item.mapToScene(QPointF(0, 0))
            assert viewport.y() <= point.y() <= viewport.y() + scroll.height(), ("outside scroll", item.objectName(), point)
            if item.height() + 32 <= scroll.height():
                assert top.y() >= viewport.y() - 1 and top.y() + item.height() <= viewport.y() + scroll.height() + 1, item.objectName()

        def click(item):
            if isinstance(item, str):
                item = control(item)
            reveal(item)
            assert item.property("enabled") is not False, item.objectName()
            point = item.mapToScene(item.boundingRect().center())
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
            QTest.qWait(70)

        def publish():
            connector.changed.emit()
            QTest.qWait(100)

        def bounds():
            card = control("internetAccessCard")
            for item in walk(card):
                if item is card or not item.isVisible() or item.width() <= 0 or item.height() <= 0:
                    continue
                # Internal text/indicator children may have intentional paint margins;
                # validate each application layout/control rather than Qt internals.
                if not item.objectName() and item.property("text") is None:
                    continue
                top = item.mapToItem(card, QPointF(0, 0))
                assert top.x() >= -1 and top.x() + item.width() <= card.width() + 1, (
                    "horizontal", item.objectName(), item.property("text"), top.x(), item.width(), card.width())
                assert top.y() >= -1 and top.y() + item.height() <= card.height() + 1, (
                    "vertical", item.objectName(), item.property("text"), top.y(), item.height(), card.height())
            card_top = card.mapToScene(QPointF(0, 0))
            scroll_top = scroll.mapToScene(QPointF(0, 0))
            assert card_top.x() >= scroll_top.x() - 1
            assert card_top.x() + card.width() <= scroll_top.x() + scroll.width() + 1

        try:
            assert not connector.state()["registered"] and not connector.state()["connected"]
            gateway, name, enrollment = (control(n) for n in ("internetGateway", "internetDeviceName", "internetEnrollment"))
            assert gateway.isVisible() and name.isVisible() and enrollment.isVisible()
            assert gateway.property("text") == "https://waraqstudio.jadallah.work"
            assert "هاتف واحد لكل جهاز" in control("internetOnePhonePolicy").property("text")
            assert not control("registerInternetButton").property("enabled")
            gateway.setProperty("text", " https://waraq.example.test ")
            name.setProperty("text", " كمبيوتر الاختبار ")
            enrollment.setProperty("text", "synthetic-one-use-enrollment")
            assert enrollment.property("displayText") != enrollment.property("text")
            QTest.qWait(50)
            click("registerInternetButton")
            assert calls[-1] == ("register", ("https://waraq.example.test", "synthetic-one-use-enrollment", "كمبيوتر الاختبار")), calls
            assert enrollment.property("text") == "", "Enrollment secret must clear immediately after submit"

            connector._error = "خطأ اتصال اختباري: تحقق من شهادة HTTPS."
            publish()
            assert control("internetError").isVisible()
            assert "شهادة HTTPS" in control("internetError").property("text")
            connector._identity = {"url": "https://waraq.example.test", "device_id": "test_device_12345678", "name": "كمبيوتر الاختبار"}
            connector._error = ""
            publish()
            denied = "هذا الجهاز مقترن بهاتف بالفعل. ألغِ ربط الهاتف الحالي أولًا."
            connector._accept_event((connector._epoch, "control", (
                {"ok": False, "status": 409, "error": denied}, ("pair_create", time.monotonic()+10, {}))))
            assert control("internetError").property("text") == denied
            assert connector.state()["registered"], "Refusing a second phone must preserve the device identity"
            connector._error = ""
            publish()
            assert not gateway.isVisible() and not name.isVisible() and not enrollment.isVisible()
            assert control("startInternetButton").isVisible()
            click("startInternetButton")
            assert calls[-1] == ("start", ())
            connector._connected, connector._status = True, "متصل"
            connector._pair_url = "https://waraq.example.test/pair#synthetic-ticket-for-ui"
            connector._claims = [{"claim_id": "claim-test", "phone_name": "هاتف الاختبار", "match_code": "482731", "expires_at": time.time() + 300}]
            connector._phones = [{"id": "phone-test", "name": "هاتف مقترن سابقًا", "created_at": time.time(), "last_seen": time.time()}]
            publish()
            assert "متصل" in control("internetStatus").property("text")
            assert "كمبيوتر الاختبار" in control("internetStatus").property("text")
            qr = control("internetQr")
            assert qr.isVisible() and qr.property("source").toString().startswith("data:image/svg+xml,")
            assert QQmlExpression(engine.rootContext(), qr, "status === 1").evaluate()[0], messages
            card = control("internetAccessCard")
            assert any("هاتف الاختبار" in str(item.property("text")) and "482731" in str(item.property("text")) for item in walk(card))
            click("pairInternetButton")
            assert calls[-1] == ("createPair", ())
            click("refreshInternetPhonesButton")
            assert calls[-1] == ("refreshPhones", ())
            click("approveInternetPhoneButton")
            assert calls[-1] == ("approvePair", ("claim-test",))
            click(text_item("رفض", card))
            assert calls[-1] == ("rejectPair", ("claim-test",))
            click(text_item("إلغاء الربط", card))
            assert calls[-1] == ("revokePhone", ("phone-test",))

            # Forgetting requires a native confirmation; cancel does not revoke.
            before = len(calls)
            click("forgetInternetButton")
            dialog = next((item for item in root.findChildren(QObject)
                           if item.property("title") == "إلغاء تسجيل هذا الجهاز"), None)
            assert dialog is not None and dialog.property("visible")
            assert len(calls) == before
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(70)
            assert not dialog.property("visible") and len(calls) == before
            click("forgetInternetButton")
            assert dialog.property("visible")
            assert QMetaObject.invokeMethod(dialog, "accept")
            QTest.qWait(70)
            assert calls[-1] == ("forgetDevice", ())

            for theme in ("light", "dark"):
                for scale in (1.0, 1.4):
                    tuned = config.snapshot()
                    tuned.update(theme=theme, ui_scale=scale)
                    assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
                    QTest.qWait(160)
                    assert root.property("dark") == (theme == "dark")
                    bounds()
                    for object_name in ("internetStatus", "pairInternetButton", "internetQr", "approveInternetPhoneButton"):
                        reveal(control(object_name))
                    if output:
                        root.grabWindow().save(str(output / f"internet-native-{theme}-{round(scale * 100)}.png"))

            identity, claims, phones, pair_url = connector._identity, connector._claims, connector._phones, connector._pair_url
            connector._identity, connector._claims, connector._phones, connector._pair_url = {}, [], [], ""
            connector._connected = False
            publish()
            bounds()
            for object_name in ("internetGateway", "internetDeviceName", "internetEnrollment", "registerInternetButton"):
                reveal(control(object_name))
            connector._identity, connector._claims, connector._phones, connector._pair_url = identity, claims, phones, pair_url
            connector._connected = True
            publish()

            # Use real LAN lifecycle and actual connector.stop, with no scheduler
            # worker thread or external call. Internet shutdown must stay scoped.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            bridge.remote.start(port)
            assert bridge.remoteAccess["enabled"]
            scheduler.running = True
            bridge.refresh()
            click("stopInternetButton")
            assert not connector.state()["connected"]
            assert scheduler.running and bridge.remoteAccess["enabled"]
            assert not control("pairInternetButton").isVisible()
            assert control("startInternetButton").isVisible()
            assert not control("forgetInternetButton").property("enabled")
        finally:
            scheduler.running = False
            bridge.stopInternet()
            bridge.stopRemote()
            scheduler.shutdown()
            root.close()
            engine.deleteLater()
            app.processEvents()
            store.db.close()
    failures = [message for message in messages if any(token in message for token in (
        "ReferenceError", "TypeError", "Cannot", "Unable", "binding loop", "failed to load"))]
    assert not failures, failures
    print("Native Internet UI passed: enrollment fields/masking/clear, Bridge action wiring, status/error, "
          "registered form hiding, QR ready, claim name/code approval/rejection, phone revoke, native forget "
          "confirmation, independent Internet stop preserving LAN+queue, minimum 1060x720, 100/140% scale, "
          "light/dark layout boundaries, no QML errors or external calls.")


if __name__ == "__main__":
    main()
