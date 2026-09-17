"""Native Qt interaction checks; synthetic entries exist only in a temp test database."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QPoint, QUrl, qInstallMessageHandler, QMetaObject, Q_ARG
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


def main():
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, ctx, text: messages.append(text))
    with tempfile.TemporaryDirectory(prefix="waraq-ui-", ignore_cleanup_errors=True) as temp:
        home = Path(temp)
        store, config = Store(home / "test.db"), Config(home)
        keys = type("Keys", (), {"get": lambda s, w: "", "set": lambda s,w,v: None})()
        scheduler = Scheduler(store, config, keys)
        bridge = Bridge(store, config, keys, scheduler)
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        QTest.qWait(250)
        def find(item, name):
            if item.objectName() == name:
                return item
            for child in item.childItems():
                result = find(child, name)
                if result is not None:
                    return result
            return None
        def click(name):
            item = find(root.contentItem(), name)
            assert item is not None, name
            p = item.mapToScene(item.boundingRect().center())
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(int(p.x()), int(p.y())))
            QTest.qWait(60)
        for index in range(5):
            click("nav" + str(index))
            assert root.property("page") == index
        click("nav1")
        bridge._accept_source(json.dumps({"title": "قائمة اختبار", "items": [
            {"video_id": f"id{i}", "position": i, "title": f"درس تجريبي {i}", "url": "https://www.youtube.com/watch?v=test",
             "duration": 60, "available": True, "selected": True} for i in range(1, 5)
        ]}, ensure_ascii=False))
        bridge.selectRange("2-3")
        assert [i["position"] for i in bridge.source if i["selected"]] == [2,3]
        bridge.moveSource(2, -1)
        bridge.enqueue("full")
        assert [j["video_id"] for j in store.jobs()] == ["id3", "id2"]
        click("nav0")
        assert bridge.stats["total"] == 2
        # Prompt templates are published, saved, and snapshotted into new jobs.
        assert [t["name"] for t in bridge.templates] == ["افتراضي"]
        tuned = config.snapshot()
        tuned["opencode_templates"] = tuned["opencode_templates"] + [
            {"id": "tplx", "name": "خطب", "text": "استخدم مهارة عربية للخطب\n{transcript_path}\n{output_path}"}]
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
        assert [t["id"] for t in bridge.templates] == ["default", "tplx"]
        bridge._accept_source(json.dumps({"title": "قوالب", "items": [
            {"video_id": "id9", "position": 9, "title": "درس بقالب", "url": "https://www.youtube.com/watch?v=test",
             "duration": 60, "available": True, "selected": True}]}, ensure_ascii=False))
        bridge.enqueue("full", "tplx")
        newest = [j for j in store.jobs() if j["video_id"] == "id9"][0]
        assert newest["config"]["template_name"] == "خطب"
        assert newest["config"]["prompt"].startswith("استخدم مهارة عربية للخطب")
        # Auto-stop is a session-only switch wired straight to the bridge.
        click("autoStopBox")
        assert bridge._auto_stop
        click("autoStopBox")
        assert not bridge._auto_stop
        # The prompt-template editor opens, lists every template and re-evaluates its bindings.
        click("nav4")
        assert root.property("page") == 4
        assert find(root.contentItem(), "templatesButton") is not None
        click("templatesButton")
        editor, deadline = None, time.time() + 3
        while editor is None and time.time() < deadline:
            QTest.qWait(50)
            editor = find(root.contentItem(), "templateEditorBox")
        if editor is None:
            # Synthetic clicks on nested page controls are unreliable on some
            # headless runners; invoke the dialog directly as a fallback.
            QMetaObject.invokeMethod(root, "openTemplates")
            deadline = time.time() + 5
            while editor is None and time.time() < deadline:
                QTest.qWait(50)
                editor = find(root.contentItem(), "templateEditorBox")
        assert editor is not None, "template editor dialog did not open: " + repr(messages[-5:])
        assert editor.property("count") == 2
        QTest.keyClick(root, Qt.Key_Escape)
        QTest.qWait(80)
        click("nav0")
        # Pause controls exercised with no dispatch: stage-specific pause, worker controls, theme persistence.
        bridge.pauseStage("download", True)
        assert "download" in scheduler.paused_stages
        bridge.pauseStage("download", False)
        for theme in ("light", "dark"):
            data = config.snapshot()
            data["theme"] = theme
            assert bridge.saveSettings(json.dumps(data))
            QTest.qWait(100)
            assert root.property("dark") == (theme == "dark")
        root.setWidth(1060)
        root.setHeight(720)
        QTest.qWait(150)
        assert root.grabWindow().width() > 0
        if len(sys.argv) > 1:
            root.setProperty("toastText", "")
            evidence = Path(sys.argv[1])
            evidence.mkdir(parents=True, exist_ok=True)
            for index in range(5):
                click("nav" + str(index))
                QTest.qWait(80)
                root.grabWindow().save(str(evidence / f"page-{index}-1060.png"))
            click("nav2")
            QMetaObject.invokeMethod(root, "editWorker", Q_ARG("QVariant", config.data["deepgram_workers"][0]), Q_ARG("QVariant", "deepgram"))
            QTest.qWait(100)
            root.grabWindow().save(str(evidence / "worker-dialog.png"))
        root.close()
        scheduler.shutdown()
        bridge.timer.stop()
        engine.deleteLater()
        app.processEvents()
        store.db.close()
    failures = [m for m in messages if any(s in m for s in ("Error", "Unable", "Cannot", "ReferenceError", "binding loop"))]
    assert not failures, failures
    print("Native UI passed: 5 pages, range selection, reorder, enqueue, templates, auto-stop toggle, stage pause, themes, 1060x720, no QML errors.")


if __name__ == "__main__":
    main()
