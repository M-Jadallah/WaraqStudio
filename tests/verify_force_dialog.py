"""Offline native acceptance for the session-only OpenCode choice dialog.

Run: python tests/verify_force_dialog.py [evidence-directory]
All configuration, accounts and jobs belong to a temporary test directory.
"""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, QPointF, QMetaObject, Q_ARG, Qt, QUrl, qInstallMessageHandler
from PySide6.QtGui import QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import waraq.bridge as bridge_module
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store
from qt_test_support import dispose_bridge
from scheduler_test_support import SchedulerHarness


def main():
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.environ.get("WINDIR"):
        os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["WINDIR"]) / "Fonts"))
    evidence = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    messages = []
    qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    bridge_module.deepgram_models = lambda: []
    with tempfile.TemporaryDirectory(prefix="waraq-force-dialog-", ignore_cleanup_errors=True) as temp, SchedulerHarness():
        home = Path(temp)
        config, store = Config(home), Store(home / "jobs.db")
        config.migration_note = ""
        keys = type("Keys", (), {"get": lambda *args: "", "set": lambda *args: None})()
        scheduler = Scheduler(store, config, keys)
        scheduler.tick = lambda *args, **kwargs: None
        scheduler.accounts.request_refresh = lambda *args, **kwargs: False
        scheduler.accounts.models = lambda: []
        bridge = Bridge(store, config, keys, scheduler)
        bridge.timer.stop()
        bridge._account_usage_timer.stop()
        # Complete catalog changes explicitly below, without external services.
        bridge.refreshModelCatalogs = lambda: None
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(ROOT / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        root.setFlags(root.flags() | Qt.FramelessWindowHint)
        root.setPosition(0, 0)
        root.show()
        QTest.qWait(180)

        def find(name):
            result = root.findChild(QObject, name)
            assert result is not None, name
            return result

        def invoke(obj, method, *values):
            assert QMetaObject.invokeMethod(obj, method, *[Q_ARG("QVariant", value) for value in values]), method
            QTest.qWait(35)

        def click(name):
            item = find(name)
            assert item.isVisible() and item.isEnabled(), name
            point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, point.toPoint())
            QTest.qWait(50)

        def edit(name, text):
            field = find(name).property("contentItem")
            field.forceActiveFocus()
            QTest.keyClick(root, Qt.Key_A, Qt.ControlModifier)
            QTest.keyClick(root, Qt.Key_Backspace)
            for character in text:
                QTest.keyClick(root, Qt.Key(ord(character.upper())))
            QTest.qWait(50)

        def open_dialog(starting=True):
            invoke(root, "openForceDialog", starting)
            QTest.qWait(80)
            assert dialog.property("opened")

        def close_dialog():
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(70)
            assert not dialog.property("visible")

        def save(name):
            if evidence:
                assert root.grabWindow().save(str(evidence / (name + ".png")))

        dialog = find("forceDialog")
        worker = config.snapshot()["opencode_workers"][0]
        original_workers = config.snapshot()["opencode_workers"]
        calls = []
        original_override = scheduler.set_provider_override

        def capture_override(*args):
            calls.append(args)
            original_override(*args)

        scheduler.set_provider_override = capture_override
        find("runButton").forceActiveFocus()
        open_dialog()
        assert not scheduler.running and not calls
        assert not dialog.property("overrideDraft")
        assert find("forceWorkersCard").property("checked")
        assert not find("forceUnifiedCard").property("checked")
        assert not find("forceFieldsPanel").isVisible()
        assert not dialog.property("providerDraft"), "Opening must not infer an override"
        assert find("forceWorkersCard").hasActiveFocus()

        # Arrow keys and Space choose exactly one card without applying it.
        QTest.keyClick(root, Qt.Key_Left)
        QTest.qWait(50)
        assert dialog.property("overrideDraft")
        assert find("forceUnifiedCard").property("checked")
        assert not find("forceWorkersCard").property("checked")
        assert dialog.property("providerDraft") == (worker.get("provider") or "login")
        assert dialog.property("modelDraft") == worker.get("model", "")
        assert dialog.property("variantDraft") == worker.get("variant", "")
        assert not calls and not scheduler.running
        find("forceWorkersCard").forceActiveFocus()
        QTest.keyClick(root, Qt.Key_Space)
        QTest.qWait(50)
        assert not dialog.property("overrideDraft")
        click("forceUnifiedCard")

        # Standard editable-combo keyboard choices retain their value/label
        # split, including the empty provider-default reasoning value.
        variant = find("forceVariantField")
        variant.forceActiveFocus()
        QTest.keyClick(root, Qt.Key_Home)
        QTest.qWait(35)
        assert dialog.property("variantDraft") == ""
        assert variant.property("editText") == "افتراضي المزود"
        QTest.keyClick(root, Qt.Key_Down)
        QTest.qWait(35)
        assert dialog.property("variantDraft") == "high"
        QTest.keyClick(root, Qt.Key_Down)
        QTest.qWait(35)
        assert dialog.property("variantDraft") == "max"

        # A live draft survives model catalog completion and a theme refresh.
        edit("forceModelField", "openai/custom-draft")
        edit("forceVariantField", "experimental")
        assert dialog.property("modelDraft") == "openai/custom-draft"
        assert dialog.property("variantDraft") == "experimental"
        bridge._opencode_models = ["openai/new-catalog-model", "groq/other-model"]
        bridge.modelCatalogChanged.emit()
        QTest.qWait(90)
        assert find("forceModelField").property("editText") == "openai/custom-draft"
        settings = config.snapshot()
        settings["theme"] = "light"
        assert bridge.saveSettings(json.dumps(settings))
        QTest.qWait(80)
        assert dialog.property("modelDraft") == "openai/custom-draft"
        assert find("forceModelField").property("editText") == "openai/custom-draft"

        # A prefixed model from a different known provider is cleared only
        # after an explicit provider selection; custom identifiers survive.
        invoke(dialog, "chooseProvider", "groq")
        assert dialog.property("modelDraft") == ""
        assert dialog.property("providerHint")
        edit("forceModelField", "custom-model-without-provider")
        invoke(dialog, "chooseProvider", "openai")
        assert dialog.property("modelDraft") == "custom-model-without-provider"
        find("skipForceBox").setProperty("checked", True)
        close_dialog()
        assert not calls and not scheduler.running
        assert not root.property("skipForceQuestion")
        assert find("runButton").hasActiveFocus()
        open_dialog()
        assert not dialog.property("overrideDraft") and not find("skipForceBox").property("checked")
        click("forceUnifiedCard")
        assert dialog.property("modelDraft") == worker.get("model", "")

        # Commit starts once, and settings remain session-local.
        edit("forceModelField", "openai/committed-model")
        edit("forceVariantField", "max")
        find("skipForceBox").setProperty("checked", True)
        click("forceApplyButton")
        assert scheduler.running
        assert len(calls) == 1
        assert scheduler.provider_override == (worker.get("provider") or "login")
        assert scheduler.provider_override_model == "openai/committed-model"
        assert scheduler.provider_override_variant == "max"
        assert root.property("skipForceQuestion")
        invoke(root, "applyForce", True, True)
        assert len(calls) == 1 and scheduler.running, "Duplicate confirmation cannot toggle the queue"
        assert config.snapshot()["opencode_workers"] == original_workers

        # Options edit copies the committed override and cancel discards both
        # a changed card and checkbox; confirmation clears without stopping.
        open_dialog(False)
        assert dialog.property("overrideDraft")
        assert dialog.property("modelDraft") == "openai/committed-model"
        assert dialog.property("variantDraft") == "max"
        click("forceWorkersCard")
        find("skipForceBox").setProperty("checked", False)
        click("forceBackButton")
        assert scheduler.running and len(calls) == 1 and root.property("skipForceQuestion")
        open_dialog(False)
        assert dialog.property("overrideDraft") and find("skipForceBox").property("checked")
        click("forceWorkersCard")
        find("skipForceBox").setProperty("checked", False)
        click("forceApplyButton")
        assert scheduler.running and not scheduler.provider_override, (scheduler.running, scheduler.provider_override, calls, dialog.property("overrideDraft"), dialog.property("visible"))
        assert not root.property("skipForceQuestion") and len(calls) == 2
        assert config.snapshot()["opencode_workers"] == original_workers

        # Default-provider reasoning is a labeled choice with an empty value.
        open_dialog(False)
        click("forceUnifiedCard")
        invoke(dialog, "chooseProvider", "login")
        edit("forceVariantField", "")
        click("forceApplyButton")
        assert scheduler.provider_override_variant == ""
        assert scheduler.running

        # The card grid responds to the modal's width, independent of the
        # side rail, and footer actions stay outside the scrolling body.
        cases = 0
        for theme, scale, (width, height) in itertools.product(
                ("dark", "light"), (.8, 1., 1.4), ((1060, 720), (1380, 900))):
            root.setWidth(width)
            root.setHeight(height)
            settings = config.snapshot()
            settings.update(theme=theme, ui_scale=scale)
            assert bridge.saveSettings(json.dumps(settings))
            open_dialog(False)
            assert 0 <= dialog.property("x")
            assert 0 <= dialog.property("y")
            assert dialog.property("x") + dialog.property("width") <= width + 1
            assert dialog.property("y") + dialog.property("height") <= height + 1
            unified, workers = find("forceUnifiedCard"), find("forceWorkersCard")
            if dialog.property("narrowBody"):
                assert workers.y() > unified.y()
            else:
                assert unified.x() > workers.x(), "Cards follow RTL reading order"
            for name in ("forceApplyButton", "forceBackButton", "skipForceBox"):
                item = find(name)
                point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
                assert dialog.property("y") <= point.y() <= dialog.property("y") + dialog.property("height"), name
                assert item.width() >= item.implicitWidth() - 1, name
            field = find("forceModelField")
            field.forceActiveFocus()
            QTest.qWait(60)
            body = find("forceDialogBody")
            point = field.mapToItem(body, QPointF(0, 0))
            assert point.y() >= -1 and point.y() + field.height() <= body.height() + 1, (theme, scale, width, point.y(), field.height(), body.height())
            if scale == 1. and width == 1380 or scale == 1.4 and width == 1060:
                save(f"{theme}-{width}-{round(scale * 100)}-unified")
                invoke(dialog, "chooseMode", False)
                save(f"{theme}-{width}-{round(scale * 100)}-workers")
            close_dialog()
            cases += 1

        errors = [message for message in messages if any(token in message for token in (
            "Error", "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Unable to assign", "is not a function"))]
        assert not errors, errors
        root.close()
        scheduler.running = False
        scheduler.accounts.close()
        dispose_bridge(bridge)
        engine.deleteLater()
        QTest.qWait(30)
        print(f"OpenCode dialog: draft/confirm/cancel/keyboard/catalog checks and {cases} layout cases passed.")


if __name__ == "__main__":
    main()
