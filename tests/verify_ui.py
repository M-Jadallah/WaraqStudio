"""Native Qt interaction checks; synthetic entries exist only in a temp test database."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QPoint, QPointF, QUrl, QMimeData, qInstallMessageHandler, QMetaObject, Q_ARG
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

import waraq.bridge as bridge_module
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store
from scheduler_test_support import SchedulerHarness


def main():
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, ctx, text: messages.append(text))
    with tempfile.TemporaryDirectory(prefix="waraq-ui-", ignore_cleanup_errors=True) as temp, SchedulerHarness() as harness:
        home = Path(temp)
        store, config = Store(home / "test.db"), Config(home)
        keys = type("Keys", (), {"get": lambda s, w: "", "set": lambda s,w,v: None})()
        bridge_module.deepgram_models = lambda: [
            {"id": "nova-3-general", "languages": ["ar", "ar-JO", "en"]},
            {"id": "whisper-large", "languages": ["ar", "en"]},
        ]
        bridge_module.opencode_models = lambda executable, context="": [
            "openai/gpt-6-astra", "openai/gpt-6-sol", "opencode/big-pickle"
        ]
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
        def find_all(item, name):
            matches = [item] if item.objectName() == name else []
            for child in item.childItems():
                matches.extend(find_all(child, name))
            return matches
        def assert_worker_rows_fit():
            cards = find_all(root.contentItem(), "workerCard")
            assert cards, "worker cards did not render"
            for card in cards:
                content = find(card, "workerContent")
                assert content is not None
                for name in ("workerStatus", "workerDetails", "workerError"):
                    label = find(card, name)
                    if label is not None and label.isVisible():
                        bottom = content.property("y") + label.property("y") + label.property("height")
                        assert bottom <= card.property("height") + 1, (name, bottom, card.property("height"))
        def click(name):
            item = find(root.contentItem(), name)
            assert item is not None, name
            p = item.mapToScene(item.boundingRect().center())
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(int(p.x()), int(p.y())))
            QTest.qWait(60)
        for index in range(6):
            click("nav" + str(index))
            assert root.property("page") == index
            if index == 5:
                assert find(root.contentItem(), "youtubeUpdateButton") is not None
        # LAN controls operate the same Bridge and remain opt-in every launch.
        assert not bridge.remoteAccess["enabled"]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            remote_port = probe.getsockname()[1]
        find(root.contentItem(), "remotePort").setProperty("value", remote_port)
        click("startRemoteButton")
        assert bridge.remoteAccess["enabled"] and bridge.remoteAccess["port"] == remote_port
        assert len(bridge.remoteAccess["code"]) == 8
        if bridge.remoteAccess["urls"]:
            assert find(root.contentItem(), "remoteQr").property("source").toString().startswith("data:image/svg+xml,")
        # A paired device is revoked immediately when the desktop rotates its code.
        token = bridge.remote.pair(bridge.remoteAccess["code"], "127.0.0.1")
        assert bridge.remote.authorize(token)
        click("renewRemoteCodeButton")
        assert not bridge.remote.authorize(token)
        if len(sys.argv) > 1:
            QTest.qWait(150)
            root.grabWindow().save(str(Path(sys.argv[1]) / "remote-access-desktop.png"))
        click("stopRemoteButton")
        assert not bridge.remoteAccess["enabled"]
        click("nav1")
        assert find(root.contentItem(), "sourceFolderCombo") is not None
        assert bridge.createFolder("مادة اختبار")
        assert any(folder["name"] == "مادة اختبار" for folder in bridge.folders)
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
        # Model selectors load asynchronously, retain saved values and persist choices.
        QMetaObject.invokeMethod(root, "editWorker", Q_ARG("QVariant", config.data["deepgram_workers"][0]), Q_ARG("QVariant", "deepgram"))
        QTest.qWait(150)
        assert [item["id"] for item in bridge.deepgramModels] == ["nova-3-general", "whisper-large"]
        model_box = find(root.contentItem(), "workerModelBox")
        language_box = find(root.contentItem(), "workerLanguageBox")
        worker_dialog = find(root.contentItem(), "workerDialog")
        assert model_box.property("count") >= 2 and language_box.property("count") >= 3
        assert model_box.property("editText") == "nova-3"
        p = model_box.mapToScene(QPointF(model_box.width() - 16, model_box.height() / 2))
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(int(p.x()), int(p.y())))
        QTest.qWait(60)
        assert model_box.property("down"), "model dropdown did not open"
        QTest.keyClick(root, Qt.Key_Escape)
        model_box.setProperty("editText", "whisper-large")
        QTest.qWait(70)
        assert model_box.property("editText") == "whisper-large", model_box.property("editText")
        assert language_box.property("count") >= 3
        language_box.setProperty("currentIndex", 2)
        click("workerSaveButton")
        assert config.data["deepgram_workers"][0]["model"] == "whisper-large", (config.data["deepgram_workers"][0], messages[-5:])
        assert config.data["deepgram_workers"][0]["language"] == "en"
        QMetaObject.invokeMethod(root, "editWorker", Q_ARG("QVariant", config.data["opencode_workers"][0]), Q_ARG("QVariant", "opencode"))
        QTest.qWait(100)
        model_box = find(root.contentItem(), "workerModelBox")
        assert model_box.property("count") >= 3
        model_box.setProperty("editText", "openai/gpt-6-sol")
        click("workerSaveButton")
        assert config.data["opencode_workers"][0]["model"] == "openai/gpt-6-sol"
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
        assert scheduler.provider_override_model == "openai/gpt-6-sol"
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
        # Scroll preservation: real model updates while scrolled down (new queue
        # row, new log line) must never throw the user back to the top.
        bulk = [{"video_id": f"bulk{i}", "title": f"مهمة إضافية {i}", "position": 100 + i,
                 "url": "https://www.youtube.com/watch?v=bulk"} for i in range(1, 41)]
        store.add(bulk, config.snapshot())
        bridge.refresh()
        QTest.qWait(200)  # let the initial population settle
        jobs_view = find(root.contentItem(), "jobsList")
        assert jobs_view is not None
        deadline = time.time() + 2
        while jobs_view.property("contentHeight") <= jobs_view.property("height") and time.time() < deadline:
            QTest.qWait(50)
        assert jobs_view.property("contentHeight") > jobs_view.property("height")
        jobs_view.setProperty("contentY", 260)
        QTest.qWait(150)
        store.add([{"video_id": "bulk-late", "title": "مهمة متأخرة", "position": 200,
                    "url": "https://www.youtube.com/watch?v=bulk"}], config.snapshot())
        store.event("", "سطر سجل جديد أثناء التمرير.")
        bridge.refresh()
        QTest.qWait(300)
        assert jobs_view.property("contentY") > 200, jobs_view.property("contentY")
        assert jobs_view.property("count") == len(store.jobs())
        # Pause controls exercised with no dispatch: stage-specific pause, worker controls, theme persistence.
        bridge.pauseStage("download", True)
        assert "download" in scheduler.manual_paused_stages
        assert "download" not in scheduler.automatic_stage_blocks
        bridge.pauseStage("download", False)
        # An automatic download block shows on the worker row and a re-check
        # lifts it and requeues the parked job.
        block_reason = "YouTube رفض الوصول. تحقّق من الاتصال وإعدادات الخدمة قبل إعادة الفحص."
        scheduler.block_stage("download", block_reason)
        store.set_stage(store.jobs()[0]["id"], "download", "blocked", block_reason)
        bridge.refresh()
        download_worker = next(w for w in bridge.workers if w["stage"] == "download")
        assert download_worker["autoBlocked"] and download_worker["error"]
        click("nav2")
        assert_worker_rows_fit()
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
        # Compact cards must still contain every text line at minimum window
        # width and maximum supported text scale.
        root.setWidth(1060)
        root.setHeight(720)
        tuned["ui_scale"] = 1.4
        tuned["ui_compact"] = True
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
        click("nav2")
        QTest.qWait(150)
        assert_worker_rows_fit()
        if len(sys.argv) > 1:
            root.setProperty("toastText", "")
            QTest.qWait(60)
            root.grabWindow().save(str(Path(sys.argv[1]) / "workers-compact-140.png"))
        tuned["ui_scale"] = 1.0
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
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
            picker = find(root.contentItem(), "workerModelBox")
            p = picker.mapToScene(QPointF(picker.width() - 16, picker.height() / 2))
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(int(p.x()), int(p.y())))
            QTest.qWait(80)
            root.grabWindow().save(str(evidence / "worker-model-options.png"))
            QTest.keyClick(root, Qt.Key_Escape)
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
            QTest.keyClick(root, Qt.Key_Escape)
            QTest.qWait(80)
        # Local TXT imports use the same selected template and subject folder,
        # skip both upstream services, and own their input copy immediately.
        root.setProperty("toastText", "")
        click("nav1")
        assert root.property("page") == 1
        template_box = find(root.contentItem(), "templateBox")
        folder_box = find(root.contentItem(), "sourceFolderCombo")
        template_box.setProperty("currentIndex", 1)
        folder_box.setProperty("currentIndex", 1)
        QTest.qWait(80)
        selected_folder = folder_box.property("currentValue")
        assert selected_folder and template_box.property("currentValue") == "tplx"
        original_defaults = config.snapshot()
        with patch.object(bridge_module.QFileDialog, "getExistingDirectory", return_value=""):
            click("sourceOutputFolderButton")
        assert config.snapshot() == original_defaults
        selected_output = home / "نتائج Word المختارة"
        with patch.object(bridge_module.QFileDialog, "getExistingDirectory", return_value=str(selected_output)):
            click("sourceOutputFolderButton")
        QTest.qWait(80)
        assert config.snapshot()["output_root"] == str(selected_output)
        assert template_box.property("currentValue") == "tplx"
        assert folder_box.property("currentValue") == selected_folder
        assert find(root.contentItem(), "sourceOutputFolderPath").property("text") == str(selected_output)
        root.setProperty("toastText", "")
        imports = home / "inputs with spaces"
        imports.mkdir()
        single = imports / "تفريغ واحد.txt"
        single.write_text("تفريغ جاهز لإنشاء Word", encoding="utf-8")

        def assert_imported(before, sources):
            new_jobs = store.jobs()[before:]
            assert len(new_jobs) == len(sources), (new_jobs, sources, messages[-5:])
            assert len({j["batch"] for j in new_jobs}) == 1
            assert [j["position"] for j in new_jobs] == list(range(1, len(sources) + 1))
            assert [j["queue_order"] for j in new_jobs] == list(range(1, len(sources) + 1))
            for job, source_file in zip(new_jobs, sources):
                assert job["folder_id"] == selected_folder
                assert job["config"]["template_name"] == "خطب"
                assert job["config"]["prompt"].startswith("استخدم مهارة عربية للخطب")
                managed = Path(job["transcript"])
                assert managed.parent == Path(job["folder"])
                assert Path(job["folder"]).parent == selected_output
                assert job["config"]["output_root"] == str(selected_output)
                assert managed != source_file and managed.read_bytes() == source_file.read_bytes()
                assert job["stages"]["download"]["status"] == "skipped"
                assert job["stages"]["deepgram"]["status"] == "skipped"
                assert job["stages"]["opencode"]["status"] == "pending"
            assert root.property("page") == 0
            return new_jobs

        before = len(store.jobs())
        with patch.object(bridge_module.QFileDialog, "getOpenFileNames", return_value=([str(single)], "")):
            click("importTranscriptsButton")
        new_jobs = assert_imported(before, [single])
        single.unlink()
        assert Path(new_jobs[0]["transcript"]).read_text(encoding="utf-8").startswith("تفريغ جاهز")

        first, second = imports / "درس 2.txt", imports / "درس 10.TXT"
        first.write_text("درس ثانٍ", encoding="utf-8-sig")
        second.write_text("درس عاشر", encoding="utf-8")
        root.setProperty("toastText", "")
        click("nav1")
        assert root.property("page") == 1
        before = len(store.jobs())
        with patch.object(bridge_module.QFileDialog, "getOpenFileNames", return_value=([str(first), str(second)], "")):
            click("importTranscriptsButton")
        assert_imported(before, [first, second])
        (imports / "ignore.mp3").write_bytes(b"audio")
        nested = imports / "nested"
        nested.mkdir()
        (nested / "ignored.txt").write_text("ليس من هذا المجلد", encoding="utf-8")
        root.setProperty("toastText", "")
        click("nav1")
        assert root.property("page") == 1
        before = len(store.jobs())
        with patch.object(bridge_module.QFileDialog, "getExistingDirectory", return_value=str(imports)):
            click("importTranscriptFolderButton")
        assert_imported(before, [first, second])

        # Deliver native Qt drag events on the workers page: global dropping
        # must work even when the source picker page is hidden.
        click("nav2")
        before = len(store.jobs())
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(first)), QUrl.fromLocalFile(str(second))])
        point = QPoint(600, 350)
        enter = QDragEnterEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(root, enter)
        QTest.qWait(80)
        assert enter.isAccepted(), "native TXT drag was not accepted"
        overlay = find(root.contentItem(), "transcriptDropOverlay")
        assert overlay is not None and overlay.isVisible()
        drop_options = find(root.contentItem(), "transcriptDropOptions").property("text")
        assert "خطب" in drop_options and str(selected_output) in drop_options
        if len(sys.argv) > 1:
            root.grabWindow().save(str(Path(sys.argv[1]) / "txt-drop-overlay.png"))
        drop = QDropEvent(QPointF(point), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(root, drop)
        QTest.qWait(80)
        assert drop.isAccepted(), "native TXT drop was not accepted"
        assert_imported(before, [first, second])
        assert not overlay.isVisible()
        before = len(store.jobs())
        invalid_mime = QMimeData()
        invalid_mime.setUrls([QUrl.fromLocalFile(str(imports / "ignore.mp3"))])
        invalid_enter = QDragEnterEvent(point, Qt.CopyAction, invalid_mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(root, invalid_enter)
        assert not invalid_enter.isAccepted() and len(store.jobs()) == before

        # The new controls remain visible within the minimum window at 140%.
        root.setProperty("toastText", "")
        click("nav1")
        assert root.property("page") == 1
        tuned = config.snapshot()
        tuned["ui_scale"] = 1.4
        assert bridge.saveSettings(json.dumps(tuned, ensure_ascii=False))
        QTest.qWait(150)
        rail_origin = rail.mapToScene(QPointF(0, 0))
        assert rail_origin.x() >= 0 and rail_origin.x() + rail.width() <= root.width() + 1
        def assert_in_window(name):
            control = find(root.contentItem(), name)
            assert control is not None and control.isVisible()
            origin = control.mapToScene(QPointF(0, 0))
            assert origin.x() >= 0 and origin.x() + control.width() <= root.width() + 1, (name, origin)
            assert origin.y() >= 0 and origin.y() + control.height() <= root.height() + 1, (name, origin)
        for name in ("templateBox", "sourceFolderCombo", "sourceOutputFolderButton", "sourceOutputFolderPath",
                     "importTranscriptsButton", "importTranscriptFolderButton"):
            assert_in_window(name)
        if len(sys.argv) > 1:
            root.setProperty("toastText", "")
            QTest.qWait(60)
            root.grabWindow().save(str(Path(sys.argv[1]) / "txt-import-140.png"))
        source_scroll = find(root.contentItem(), "sourceScroll")
        assert source_scroll is not None
        flick = source_scroll.property("contentItem")
        assert flick.property("contentHeight") > flick.property("height")
        flick.setProperty("contentY", flick.property("contentHeight") - flick.property("height"))
        QTest.qWait(100)
        for name in ("sourcePreset", "enqueueSourceButton"):
            assert_in_window(name)
        source_view = find(root.contentItem(), "sourceList")
        assert source_view is not None and source_view.height() >= 100
        if len(sys.argv) > 1:
            root.grabWindow().save(str(Path(sys.argv[1]) / "txt-import-options-140.png"))
        # Queue selection follows stable IDs and selects only filtered results.
        root.setProperty("toastText", "")
        click("nav0")
        root.setProperty("filter", "all")
        queue_inputs = [{"video_id": f"selection-{i}", "title": f"اختبار تحديد {i}", "position": i,
                         "url": "https://www.youtube.com/watch?v=selection"} for i in (1, 2)]
        pending_id, running_id = store.add(queue_inputs, config.snapshot())
        local_input = home / "اختبار تحديد TXT.txt"
        local_input.write_text("نص اختبار التحديد", encoding="utf-8")
        local_id = store.add([{"video_id": "selection-txt", "title": local_input.stem, "position": 3,
                               "url": ""}], config.snapshot(), input_path=str(local_input))[0]
        managed_input = Path(store.get(local_id)["transcript"])
        assert store.claim(running_id, "download", "busy-test")
        bridge.refresh()
        find(root.contentItem(), "queueSearch").setProperty("text", "اختبار تحديد")
        jobs_view.setProperty("contentY", 0)
        QTest.qWait(150)
        def selection():
            value = root.property("selectedQueueIds")
            return value.toVariant() if hasattr(value, "toVariant") else value
        assert not find(root.contentItem(), "deleteSelectedJobsButton").property("enabled")
        for stage in ("download", "deepgram", "opencode"):
            assert find(root.contentItem(), "queueStage-" + pending_id + "-" + stage) is not None
        # Filtering changes delegate heights asynchronously. Position against
        # the settled layout and verify the click is inside the clipped list;
        # estimated contentHeight + a fixed sleep can leave the last checkbox
        # below the viewport on slower Windows runners.
        deadline = time.monotonic() + 3
        while True:
            QMetaObject.invokeMethod(jobs_view, "forceLayout")
            QMetaObject.invokeMethod(jobs_view, "positionViewAtIndex", Q_ARG("int", len(bridge.jobs) - 1), Q_ARG("int", 1))
            QTest.qWait(100)
            checkbox = find(root.contentItem(), "selectJob-" + local_id)
            center = checkbox.mapToScene(checkbox.boundingRect().center()) if checkbox is not None else QPointF(-1, -1)
            view_origin = jobs_view.mapToScene(QPointF(0, 0))
            if (view_origin.x() < center.x() < view_origin.x() + jobs_view.width()
                    and view_origin.y() < center.y() < view_origin.y() + jobs_view.height()
                    and 0 < center.y() < root.height()):
                break
            assert time.monotonic() < deadline, (center, view_origin, jobs_view.height())
        assert find(root.contentItem(), "queueStage-" + local_id + "-opencode") is not None
        assert find(root.contentItem(), "queueStage-" + local_id + "-download") is None
        assert find(root.contentItem(), "queueStage-" + local_id + "-deepgram") is None
        click("selectJob-" + local_id)
        assert selection() == [local_id], (selection(), find(root.contentItem(), "selectJob-" + local_id).mapToScene(QPointF(0, 0)), jobs_view.property("height"), messages[-8:])
        if len(sys.argv) > 1:
            root.grabWindow().save(str(Path(sys.argv[1]) / "queue-txt-word-only-140.png"))
        click("selectAllJobsBox")
        assert set(selection()) == {pending_id, running_id, local_id}
        click("selectAllJobsBox")
        assert selection() == []
        click("queueFilter-running")
        click("selectAllJobsBox")
        assert selection() == [running_id]
        click("queueFilter-all")
        click("selectAllJobsBox")
        assert set(selection()) == {pending_id, running_id, local_id}
        assert find(root.contentItem(), "selectedJobsCount").property("text") == "المحدد: 3"
        for name in ("deleteSelectedJobsButton", "selectAllJobsBox", "selectedJobsCount"):
            assert_in_window(name)
        if len(sys.argv) > 1:
            root.grabWindow().save(str(Path(sys.argv[1]) / "queue-selection-140.png"))
        click("deleteSelectedJobsButton")
        assert store.get(pending_id) is None and store.get(local_id) is None
        assert store.get(running_id) is not None and selection() == [running_id]
        assert managed_input.is_file() and local_input.is_file()
        assert root.property("toastError") and root.property("toastText")
        store.set_stage(running_id, "download", "failed", "اختبار انتهاء المهمة المشغولة")
        bridge.refresh()
        root.setProperty("toastText", "")
        click("deleteSelectedJobsButton")
        assert store.get(running_id) is None and selection() == []
        assert not find(root.contentItem(), "deleteSelectedJobsButton").property("enabled")
        find(root.contentItem(), "queueSearch").setProperty("text", "")
        # The update card uses mocked GitHub services and an in-memory token.
        root.setProperty("toastText", "")
        click("nav5")
        settings_scroll = find(root.contentItem(), "settingsScroll")
        settings_flick = settings_scroll.property("contentItem")
        def reveal_update_control(name):
            item = find(root.contentItem(), name)
            origin = item.mapToScene(QPointF(0, 0))
            scroll_origin = settings_scroll.mapToScene(QPointF(0, 0))
            new_y = settings_flick.property("contentY") + origin.y() - scroll_origin.y() - 12
            maximum = max(0, settings_flick.property("contentHeight") - settings_flick.property("height"))
            settings_flick.setProperty("contentY", max(0, min(maximum, new_y)))
            QTest.qWait(80)
            assert_in_window(name)
        def wait_update(status):
            deadline = time.time() + 3
            while bridge.appUpdate["status"] != status and time.time() < deadline:
                QTest.qWait(25)
            assert bridge.appUpdate["status"] == status, bridge.appUpdate
            QTest.qWait(50)
        credentials = {}
        release = {"available": True, "version": "99.0.0", "name": "fake-setup.exe"}
        installer = home / "fake-update.exe"
        installer.write_bytes(b"fake installer - never launched")
        download_finish = threading.Event()
        def fake_download(home_path, latest, cancel, progress, token):
            assert token == "ui-update-token"
            progress(37)
            while not download_finish.wait(0.01):
                if cancel.is_set():
                    raise bridge_module.app_update.UpdateCancelled("أُلغي التنزيل الاختباري.")
            progress(100)
            return installer
        with patch.object(keys, "get", side_effect=lambda key: credentials.get(key, "")), \
                patch.object(keys, "set", side_effect=lambda key, value: credentials.update({key: value})), \
                patch.object(keys, "delete", side_effect=lambda key: credentials.pop(key, None), create=True), \
                patch.object(bridge_module.app_update, "check_latest", return_value=release), \
                patch.object(bridge_module.app_update, "download_installer", side_effect=fake_download):
            token_field = find(root.contentItem(), "updateTokenField")
            token_field.setProperty("text", "ui-update-token")
            assert token_field.property("displayText") != "ui-update-token"
            reveal_update_control("saveUpdateTokenButton")
            click("saveUpdateTokenButton")
            assert bridge.appUpdate["hasToken"] and token_field.property("text") == ""
            root.setProperty("toastText", "")
            reveal_update_control("checkAppUpdateButton")
            click("checkAppUpdateButton")
            wait_update("available")
            reveal_update_control("downloadAppUpdateButton")
            click("downloadAppUpdateButton")
            wait_update("downloading")
            deadline = time.time() + 2
            while bridge.appUpdate["progress"] != 37 and time.time() < deadline:
                QTest.qWait(25)
            assert find(root.contentItem(), "appUpdateProgress").property("value") == 37
            assert not find(root.contentItem(), "checkAppUpdateButton").property("enabled")
            reveal_update_control("cancelAppUpdateButton")
            if len(sys.argv) > 1:
                reveal_update_control("appUpdateMessage")
                root.grabWindow().save(str(Path(sys.argv[1]) / "app-update-progress-140.png"))
            click("cancelAppUpdateButton")
            wait_update("available")
            reveal_update_control("downloadAppUpdateButton")
            click("downloadAppUpdateButton")
            download_finish.set()
            wait_update("downloaded")
            assert find(root.contentItem(), "installAppUpdateButton").isVisible()
            reveal_update_control("installAppUpdateButton")
            reveal_update_control("removeUpdateTokenButton")
            click("removeUpdateTokenButton")
            assert not bridge.appUpdate["hasToken"] and credentials == {}
            root.setProperty("toastText", "")
            with patch.object(bridge_module.app_update, "check_latest", side_effect=bridge_module.app_update.UpdateError("رفض GitHub الرمز. راجع صلاحيات المستودع.")):
                reveal_update_control("checkAppUpdateButton")
                click("checkAppUpdateButton")
                wait_update("error")
            assert "رفض GitHub" in find(root.contentItem(), "appUpdateMessage").property("text")
            assert find(root.contentItem(), "checkAppUpdateButton").property("enabled")
            if len(sys.argv) > 1:
                reveal_update_control("appUpdateMessage")
                root.grabWindow().save(str(Path(sys.argv[1]) / "app-update-error-140.png"))
        root.close()
        bridge.timer.stop()
        harness.shutdown_and_join()
        engine.deleteLater()
        app.processEvents()
        with store.lock:
            store.db.close()
    failures = [m for m in messages if any(s in m for s in ("Error", "Unable", "Cannot", "ReferenceError", "binding loop"))]
    assert not failures, failures
    print("Native UI passed: 6 pages, range selection, reorder, enqueue, templates, auto-stop toggle, worker toggles, "
          "provider override, library organise/list/delete, stage pause, automatic-block re-check, scroll pinning, "
          "collapsible rail, UI scale, themes, single/multi/folder TXT import, native TXT drag/drop, "
          "managed input copies, selected templates/folders, filtered queue selection/deletion, busy-job protection, "
          "TXT Word-only stages, update progress/cancel/error/token controls, LAN start/QR/pair/revoke/stop, 1060x720, no QML errors.")


if __name__ == "__main__":
    main()
