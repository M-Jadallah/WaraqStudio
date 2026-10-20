"""Native task-detail layout/state checks using temporary data and no providers.

Run directly: python tests/verify_task_dialog.py [evidence-directory]
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QMetaObject, QUrl, Qt, qInstallMessageHandler
from PySide6.QtGui import QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
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
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    messages = []
    qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    evidence = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
    bridge_module.deepgram_models = lambda: []
    with tempfile.TemporaryDirectory(prefix="waraq-task-dialog-", ignore_cleanup_errors=True) as temp, SchedulerHarness() as harness:
        home = Path(temp)
        config, store = Config(home), Store(home / "tasks.db")
        config.migration_note = ""
        folder = store.create_folder("الفقه وأصوله")
        ident = store.add([{"video_id": "modal", "title": "درس طويل — شرح أبواب العبادات ومقاصدها وآداب طالب العلم", "url": "https://example.invalid/lesson", "position": 1}], config.snapshot(), folder_id=folder)[0]
        store.set_stage(ident, "download", "completed")
        store.set_stage(ident, "deepgram", "blocked", "تعذرت متابعة الطلب")
        imported = home / "imported.txt"
        imported.write_text("تفريغ محلي", encoding="utf-8")
        local_id = store.add([{"video_id": "local-modal", "title": "تفريغ محلي إلى Word", "position": 2}], config.snapshot(), input_path=str(imported))[0]
        keys = type("Keys", (), {"get": lambda *args: "", "set": lambda *args: None})()
        scheduler = Scheduler(store, config, keys)
        scheduler.tick = lambda: None
        scheduler.accounts.request_refresh = lambda *args, **kwargs: False
        scheduler.accounts.models = lambda: []
        bridge = Bridge(store, config, keys, scheduler)
        bridge.timer.stop()
        bridge._account_usage_timer.stop()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(ROOT / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        root.setFlags(root.flags() | Qt.FramelessWindowHint)
        root.setPosition(0, 0)

        def find(name):
            result = root.findChild(QObject, name)
            if result is not None:
                return result
            def visual(item):
                if item.objectName() == name:
                    return item
                for child in item.childItems():
                    found = visual(child)
                    if found is not None:
                        return found
                return None
            result = visual(root.contentItem())
            assert result is not None, name
            return result

        def tune(theme, scale):
            data = config.snapshot()
            data.update(theme=theme, ui_scale=scale)
            assert bridge.saveSettings(json.dumps(data, ensure_ascii=False))
            root.setProperty("toastText", "")
            QTest.qWait(180)

        popup = find("jobDialog")
        actions = ("OpenWord", "OpenFolder", "Retry", "Cancel", "NewWord", "NewTranscript")
        original = bridge.jobById(ident)
        long_error = "تعذرت متابعة الطلب؛ يلزم مراجعة الخدمة ثم اتخاذ القرار المناسب. " * 12
        cases = 0
        for (width, height), theme, scale in itertools.product(((1060, 720), (1380, 900), (1920, 1080)), ("dark", "light"), (.8, 1., 1.4)):
            root.setWidth(width); root.setHeight(height)
            root.requestActivate()
            tune(theme, scale)
            selected = dict(original, error=long_error, retryAt="13:25", template="قالب الدروس العلمية — تنظيم متكامل", status="blocked", statusLabel="يتطلب إجراء")
            root.setProperty("selectedJob", selected)
            find("runButton").forceActiveFocus()
            assert QMetaObject.invokeMethod(popup, "open")
            QTest.qWait(80)
            assert popup.property("x") >= 0 and popup.property("y") >= 0
            assert popup.property("x") + popup.property("width") <= width + 1
            assert popup.property("y") + popup.property("height") <= height + 1
            close = find("jobDialogCloseButton")
            close_center = close.mapToScene(close.boundingRect().center())
            assert 0 < close_center.y() < height
            assert 0 < close_center.x() < width
            error = find("jobDialogError")
            assert error.property("text") == long_error
            assert error.property("contentHeight") <= error.height() + 1, (width, scale, error.height())
            title = find("jobDialogTitle")
            assert title.property("text") == selected["title"]
            assert title.property("contentHeight") <= title.height() + 1
            stages = [find("jobDialogStage-" + step["name"]) for step in selected["steps"]]
            assert all(stage.isVisible() for stage in stages)
            assert all(a.mapToScene(a.boundingRect().center()).x() > b.mapToScene(b.boundingRect().center()).x() for a, b in zip(stages, stages[1:])), "Stages must read right to left"
            work, log, action = (find("jobDialog" + suffix) for suffix in ("WorkArea", "LogPane", "ActionPane"))
            assert log.height() >= 190 * scale - 1
            if popup.property("narrow"):
                assert action.y() >= log.y() + log.height(), (width, scale)
            else:
                assert action.x() >= log.x() + log.width(), (width, scale)
            body = find("jobDialogBodyScroll")
            flick = body.property("contentItem")
            assert flick.property("contentWidth") <= body.property("availableWidth") + 1, (width, scale)
            # Every action can be brought fully inside the bounded body; Close
            # stays visible while long errors and low windows scroll.
            for suffix in actions:
                button = find("jobDialog" + suffix)
                point = button.mapToItem(flick, button.boundingRect().center())
                target = flick.property("contentY") + point.y() - flick.height() / 2
                maximum = max(0, flick.property("contentHeight") - flick.height())
                flick.setProperty("contentY", max(0, min(maximum, target)))
                QTest.qWait(10)
                point = button.mapToItem(flick, button.boundingRect().center())
                assert 0 <= point.y() <= flick.height(), (width, scale, suffix, point.y(), flick.height(), maximum)
                assert button.width() > 0
                assert button.property("font").weight() == QFont.Weight.Bold
                assert button.property("font").family() == "Segoe UI"
            if width == 1060 and scale == 1.4:
                # Real Tab traversal must reveal controls after the long error;
                # moving only the scrollbar would miss keyboard focus clipping.
                root.setProperty("selectedJob", dict(selected, document="demo.docx", transcript="demo.txt", audio="demo.mp3"))
                flick.setProperty("contentY", 0)
                stages[0].forceActiveFocus()
                seen = set()
                for _ in range(20):
                    QTest.keyClick(root, Qt.Key_Tab)
                    QTest.qWait(20)
                    focused = root.activeFocusItem()
                    node, target = focused, focused
                    while node is not None and node != body:
                        if node.objectName() in ("jobDialogLogScroll", "jobDialogFolderCombo"):
                            target = node
                        node = node.parentItem()
                    if node == body:
                        seen.add(target.objectName())
                        top = target.mapToItem(flick, target.boundingRect().topLeft()).y()
                        bottom = target.mapToItem(flick, target.boundingRect().bottomRight()).y()
                        assert top >= -1 and bottom <= flick.height() + 1, (theme, target.objectName(), top, bottom, flick.height())
                assert {"jobDialogFolderCombo", "jobDialogLogScroll", *("jobDialog" + suffix for suffix in actions)} <= seen, seen
                root.setProperty("selectedJob", selected)
            if evidence and width == 1060 and scale == 1.4:
                flick.setProperty("contentY", max(0, flick.property("contentHeight") - flick.height()))
                QTest.qWait(20)
                assert root.grabWindow().save(str(evidence / f"task-{width}-{height}-{theme}-140-actions.png"))
            if evidence and scale in (1., 1.4):
                flick.setProperty("contentY", 0)
                QTest.qWait(30)
                assert root.grabWindow().save(str(evidence / f"task-{width}-{height}-{theme}-{round(scale*100)}.png"))
            close.forceActiveFocus()
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(30)
            assert not popup.property("visible"), (width, height, theme, scale, root.activeFocusItem())
            assert find("runButton").hasActiveFocus(), (width, height, theme, scale, root.activeFocusItem())
            cases += 1

        # Availability and original action rules remain authoritative in every state.
        tune("dark", 1.)
        root.setProperty("selectedJob", original)
        QMetaObject.invokeMethod(popup, "open")
        QTest.qWait(50)
        for status, available, retry_at in itertools.product(("pending", "running", "completed", "failed", "blocked", "uncertain", "paused", "cancelled"), (False, True), ("", "13:25")):
            root.setProperty("selectedJob", dict(original, status=status, document="demo.docx" if available else "", transcript="demo.txt" if available else "", audio="demo.mp3" if available else "", retryAt=retry_at))
            QTest.qWait(5)
            expected = (available, True, status in ("failed", "blocked", "uncertain", "paused", "cancelled") or bool(retry_at), status != "completed", available and status != "running", available and status != "running")
            actual = tuple(find("jobDialog" + suffix).isEnabled() for suffix in actions)
            assert actual == expected, (status, available, retry_at, actual, expected)
        for status, progress in itertools.product(("running", "completed"), (0, 63, 100, -1)):
            root.setProperty("selectedJob", dict(original, status=status, progress=progress))
            QTest.qWait(10)
            assert find("jobDialogProgressRow").isVisible() == (status == "running")
            if status == "running":
                assert find("jobDialogProgress").property("indeterminate") == (progress < 0)
                assert find("jobDialogProgressLabel").property("text") == (f"{progress}٪" if progress >= 0 else "قيد التنفيذ")
                if progress >= 0:
                    assert find("jobDialogProgress").property("value") == progress
        root.setProperty("selectedJob", bridge.jobById(local_id))
        QTest.qWait(50)
        assert [step["name"] for step in bridge.jobById(local_id)["steps"]] == ["opencode"]
        assert find("jobDialogStage-opencode").isVisible()
        assert not root.findChild(QObject, "jobDialogStage-download")
        assert not root.findChild(QObject, "jobDialogStage-deepgram")

        # A selected, read-only log retains its scroll position as real events append.
        root.setProperty("selectedJob", original)
        for index in range(85):
            store.event(ident, f"سطر التنفيذ المحلي {index}")
        bridge.refresh()
        QMetaObject.invokeMethod(root, "updateJobLog")
        QTest.qWait(60)
        text = find("jobDialogLog")
        assert text.property("readOnly") and text.property("selectByMouse")
        log_flick = find("jobDialogLogScroll").property("contentItem")
        assert log_flick.property("contentHeight") > log_flick.height()
        log_flick.setProperty("contentY", 80)
        before = log_flick.property("contentY")
        store.event(ident, "سطر مضاف أثناء القراءة")
        bridge.refresh()
        QTest.qWait(60)
        assert "سطر مضاف أثناء القراءة" in text.property("text")
        assert abs(log_flick.property("contentY") - before) <= 5, (before, log_flick.property("contentY"))
        # Healthy screenshots use actual store stage state and local artifacts.
        root.setWidth(1380); root.setHeight(900)
        tune("dark", 1.)
        store.set_stage(ident, "deepgram", "running", progress=63)
        bridge.refresh()
        root.setProperty("selectedJob", bridge.jobById(ident))
        find("jobDialogBodyScroll").property("contentItem").setProperty("contentY", 0)
        QTest.qWait(80)
        if evidence:
            assert root.grabWindow().save(str(evidence / "task-1380-900-dark-running-63.png"))
        output = home / "lesson.docx"
        output.write_bytes(b"local test artifact")
        store.artifact(ident, "document", str(output))
        store.set_stage(ident, "deepgram", "completed")
        store.set_stage(ident, "opencode", "completed")
        bridge.refresh()
        root.setProperty("selectedJob", bridge.jobById(ident))
        QTest.qWait(80)
        assert not find("jobDialogProgressRow").isVisible()
        assert find("jobDialogOpenWord").isEnabled()
        if evidence:
            assert root.grabWindow().save(str(evidence / "task-1380-900-dark-completed.png"))
        QTest.keyClick(root, Qt.Key_Escape)
        root.close()
        harness.shutdown_and_join()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)
        dispose_bridge(bridge)
        app.processEvents()
        store.db.close()
    errors = [message for message in messages if message.startswith("file:") or any(term in message.lower() for term in ("binding loop", "failed to load", "referenceerror", "typeerror", "recursive rearrange"))]
    assert not errors, errors
    print(f"Task detail acceptance passed: {cases} size/theme/scale layouts, 32 action scenarios (192 enabled checks), 8 progress states, TXT-only steps, full errors, reachable actions, focus and log retention.")


if __name__ == "__main__":
    main()
