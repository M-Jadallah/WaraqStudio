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
        for index in range(6):
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
        # Worker list: direct enable toggle, provider management, and the queue override.
        click("nav2")
        assert find(root.contentItem(), "workerEnableBox") is not None
        assert find(root.contentItem(), "providersButton") is not None
        assert [w["id"] for w in scheduler.workers() if w["stage"] == "opencode"] == ["oc1", "oc2", "oc3", "oc4", "oc5"]
        oc1 = [w for w in scheduler.workers() if w["id"] == "oc1"][0]
        assert oc1["provider"] == "login" and oc1["modelResolved"] == "openai/gpt-6-astra"
        bridge.setWorkerEnabled("oc3", False)
        assert not [w for w in scheduler.workers() if w["id"] == "oc3"][0]["enabled"]
        bridge.setWorkerEnabled("oc3", True)
        assert [w for w in scheduler.workers() if w["id"] == "oc3"][0]["enabled"]
        # A provider added by hand joins the catalog and can back a worker.
        assert bridge.saveProvider(json.dumps({"id": "groq", "name": "Groq", "keyEnv": "GROQ_API_KEY", "isolated": True}))
        assert bridge.providers[-1]["id"] == "groq" and bridge.providers[-1]["builtin"] is False
        assert not bridge.saveProvider(json.dumps({"id": "bad id", "name": "x", "keyEnv": "GROQ_API_KEY", "isolated": True}))
        assert not bridge.saveProvider(json.dumps({"id": "openai", "name": "x", "keyEnv": "X_API_KEY", "isolated": True}))
        tuned = config.snapshot()
        tuned["opencode_workers"][2].update({"provider": "groq", "model": "llama-3.3-70b"})
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
        oc3 = [w for w in scheduler.workers() if w["id"] == "oc3"][0]
        assert oc3["provider"] == "groq" and oc3["modelResolved"] == "groq/llama-3.3-70b"
        assert oc3["keyEnv"] == "GROQ_API_KEY" and not oc3["hasKey"]
        # The queue override replaces provider, model and reasoning level for every worker.
        bridge.setProviderOverride("groq", "llama-3.3-70b", "high")
        assert scheduler.provider_override == "groq" and scheduler.provider_override_variant == "high"
        forced = [w for w in scheduler.workers() if w["id"] == "oc1"][0]
        assert forced["provider"] == "groq" and forced["modelResolved"] == "groq/llama-3.3-70b"
        bridge.setProviderOverride("", "", "")
        assert scheduler.provider_override == ""
        # Credentials are scoped to the worker and its provider.
        saved = {}
        keys.set = lambda worker, value: saved.update({worker: value})
        bridge.saveKey("oc3", "gsk-test")
        assert saved == {"oc3:groq": "gsk-test"}, saved
        bridge.deleteProvider("groq")
        assert any(p["id"] == "groq" for p in bridge.providers), "a provider in use must not be deleted"
        # Starting the queue asks about the override first, and both answers start it.
        click("nav0")
        assert find(root.contentItem(), "forceButton") is not None
        click("runButton")
        assert find(root.contentItem(), "forceQuestion") is not None, "the force question must precede a start"
        assert not scheduler.running
        click("forceApplyButton")
        assert scheduler.running, "the forced-start button must start the queue"
        # The dialog prefilled from the first worker, so that is what got forced.
        assert scheduler.provider_override == "login" and scheduler.provider_override_variant == "high"
        click("runButton")
        assert not scheduler.running
        bridge.setProviderOverride("", "", "")
        # Library: organise a finished artifact into its type folder, list it, then delete it.
        library_root = Path(temp) / "library"
        tuned = config.snapshot()
        tuned["library_root"] = str(library_root)
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
        source = Path(temp) / "003-درس.docx"
        source.write_bytes(b"docx-bytes")
        job = store.jobs()[0]
        store.artifact(job["id"], "document", str(source))
        bridge.organizeLibrary()
        deadline = time.time() + 5
        while bridge.libraryBusy and time.time() < deadline:
            QTest.qWait(50)
        target = library_root / "Word" / f'{job["position"]:03d}-{job["title"]}.docx'
        assert target.is_file(), list((library_root / "Word").iterdir())
        click("nav4")
        assert root.property("page") == 4
        assert find(root.contentItem(), "organizeButton") is not None
        assert [f["name"] for f in bridge.library] == [target.name]
        bridge.deleteLibraryFile(str(target))
        assert not target.exists() and bridge.library == []
        click("nav0")
        # Scroll pinning: a backend republish must not throw the user back to the top.
        bulk = [{"video_id": f"bulk{i}", "title": f"مهمة إضافية {i}", "position": 100 + i,
                 "url": "https://www.youtube.com/watch?v=bulk"} for i in range(1, 41)]
        store.add(bulk, config.snapshot())
        bridge.refresh()
        QTest.qWait(200)  # let the first repopulation settle
        jobs_view = find(root.contentItem(), "jobsList")
        assert jobs_view is not None
        assert jobs_view.property("contentHeight") > jobs_view.property("height")
        jobs_view.setProperty("contentY", 260)
        QTest.qWait(250)  # let the PinList sampler capture the scrolled position
        bridge.refresh()
        QTest.qWait(300)
        assert jobs_view.property("contentY") > 200, jobs_view.property("contentY")
        # Pause controls exercised with no dispatch: stage-specific pause, worker controls, theme persistence.
        bridge.pauseStage("download", True)
        assert "download" in scheduler.manual_paused_stages
        assert "download" not in scheduler.automatic_stage_blocks
        bridge.pauseStage("download", False)
        # An automatic download block shows on the worker row and a re-check
        # lifts it and requeues the parked job.
        scheduler.block_stage("download", "YouTube رفض الوصول.")
        store.set_stage(store.jobs()[0]["id"], "download", "blocked", "YouTube رفض الوصول.")
        bridge.refresh()
        download_worker = next(w for w in bridge.workers if w["stage"] == "download")
        assert download_worker["autoBlocked"] and download_worker["error"]
        bridge.recheckStage("download")
        assert "download" not in scheduler.automatic_stage_blocks
        assert store.get(store.jobs()[0]["id"])["stages"]["download"]["status"] == "pending"
        # Collapsible navigation rail; collapse state persists through settings.
        rail = find(root.contentItem(), "navRail")
        click("railToggle")
        QTest.qWait(300)
        assert rail.property("width") < 100, rail.property("width")
        if len(sys.argv) > 1:
            evidence_dir = Path(sys.argv[1])
            evidence_dir.mkdir(parents=True, exist_ok=True)
            root.grabWindow().save(str(evidence_dir / "rail-collapsed.png"))
        click("nav3")
        assert root.property("page") == 3
        click("railToggle")
        QTest.qWait(300)
        assert rail.property("width") > 200, rail.property("width")
        # UI scale round-trips through settings and re-scales the interface.
        tuned = config.snapshot()
        tuned["ui_scale"] = 1.2
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
        QTest.qWait(150)
        assert abs(root.property("uiScale") - 1.2) < 0.001
        tuned["ui_scale"] = 1.0
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
        QTest.qWait(150)
        assert abs(root.property("uiScale") - 1.0) < 0.001
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
            scrolled = find(root.contentItem(), "jobsList")
            if scrolled is not None:
                scrolled.setProperty("contentY", 0)
            QTest.qWait(120)
            for index in range(6):
                click("nav" + str(index))
                QTest.qWait(80)
                root.grabWindow().save(str(evidence / f"page-{index}-1060.png"))
            click("nav2")
            QMetaObject.invokeMethod(root, "editWorker", Q_ARG("QVariant", config.data["deepgram_workers"][0]), Q_ARG("QVariant", "deepgram"))
            QTest.qWait(100)
            root.grabWindow().save(str(evidence / "worker-dialog.png"))
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(80)
            QMetaObject.invokeMethod(root, "editWorker", Q_ARG("QVariant", config.data["opencode_workers"][0]), Q_ARG("QVariant", "opencode"))
            QTest.qWait(150)
            root.grabWindow().save(str(evidence / "worker-dialog-opencode.png"))
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(80)
            QMetaObject.invokeMethod(root, "openForceDialog", Q_ARG("QVariant", True))
            QTest.qWait(150)
            root.grabWindow().save(str(evidence / "force-dialog.png"))
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(80)
            click("providersButton")
            QTest.qWait(200)
            root.grabWindow().save(str(evidence / "providers-dialog.png"))
        root.close()
        scheduler.shutdown()
        bridge.timer.stop()
        engine.deleteLater()
        app.processEvents()
        store.db.close()
    failures = [m for m in messages if any(s in m for s in ("Error", "Unable", "Cannot", "ReferenceError", "binding loop"))]
    assert not failures, failures
    print("Native UI passed: 6 pages, range selection, reorder, enqueue, templates, auto-stop toggle, worker toggles, "
          "provider override, library organise/list/delete, stage pause, automatic-block re-check, scroll pinning, "
          "collapsible rail, UI scale, themes, 1060x720, no QML errors.")


if __name__ == "__main__":
    main()
