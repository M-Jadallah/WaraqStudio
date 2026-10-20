"""Native visual/layout acceptance matrix. Uses only temporary synthetic data.

Usage: python tests/verify_redesign.py [evidence-directory]
No remote providers, OS credentials, user databases, or real output paths.
"""
import itertools
import json
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QMetaObject, Q_ARG, QPoint, Qt, QUrl, QCoreApplication, QEvent, qInstallMessageHandler
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import waraq.bridge as bridge_module
from waraq.embedded_accounts import EmbeddedAccountService
from scheduler_test_support import SchedulerHarness
from qt_test_support import dispose_bridge
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


def main():
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.environ.get("WINDIR"):
        os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["WINDIR"]) / "Fonts"))
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    arguments = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    evidence = Path(arguments[0]).resolve() if arguments else None
    modals_only = "--modals-only" in sys.argv
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
    bridge_module.deepgram_models = lambda: [{"id": "nova-3-general", "languages": ["ar", "en"]}]
    EmbeddedAccountService.models = lambda *args: ["openai/gpt-6-astra", "openai/gpt-6-sol"]
    with tempfile.TemporaryDirectory(prefix="waraq-redesign-", ignore_cleanup_errors=True) as temp, SchedulerHarness() as harness:
        home = Path(temp)
        config, store = Config(home), Store(home / "tasks.db")
        config.migration_note = ""
        keys = type("Keys", (), {"get": lambda *args: "", "set": lambda *args: None})()
        scheduler = Scheduler(store, config, keys)
        scheduler.tick = lambda: None
        scheduler.accounts.request_refresh = lambda *args, **kwargs: False
        folder = store.create_folder("الفقه وأصوله")
        store.add([{"title": f"الدرس {i:02d} — شرح أبواب العبادات ومقاصدها وآداب طالب العلم", "video_id": f"demo{i}", "url": "https://example.invalid/demo", "position": i, "duration": 1800 + i * 60} for i in range(1, 65)], config.snapshot(), folder_id=folder)
        jobs = store.jobs()
        for i, job in enumerate(jobs[:8]):
            path = home / f"lesson-{i}.txt"
            path.write_text("نص اختباري محلي", encoding="utf-8")
            store.artifact(job["id"], "transcript", str(path))
            store.set_stage(job["id"], "download", "completed")
            if i == 0:
                store.set_stage(job["id"], "deepgram", "running", progress=63)
            elif i == 1:
                store.set_stage(job["id"], "deepgram", "uncertain", "استجابة غير مؤكدة — يلزم قرار المستخدم قبل إعادة الطلب")
            else:
                store.set_stage(job["id"], "deepgram", "completed")
                store.set_stage(job["id"], "opencode", "completed")
        bridge = Bridge(store, config, keys, scheduler)
        bridge._account_usage_timer.stop()
        bridge._accept_source(json.dumps({"title": "سلسلة دروس الفقه", "items": [{"video_id": f"source{i}", "position": i, "title": f"الدرس {i} — عنوان عربي طويل لاختبار التفاف النص وعدم تداخل الأزرار", "url": "https://example.invalid/source", "duration": 1200, "available": True, "selected": i % 2 == 0} for i in range(1, 45)]}, ensure_ascii=False))
        # Seed a real temporary library scan rather than a second presentation model.
        library = Path(config.data["library_root"]) / "الفقه وأصوله" / "التفريغات"
        library.mkdir(parents=True)
        for i in range(15):
            (library / f"درس الفقه {i:02d}.txt").write_text("نص اختباري", encoding="utf-8")
        bridge.refreshLibrary()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        # The harness alone removes the native frame so the 1920x1080 canvas
        # fits a 1080p monitor. Production keeps its normal Windows frame.
        root.setFlags(root.flags() | Qt.FramelessWindowHint)
        root.setPosition(0, 0)
        root.show()
        QTest.qWait(180)

        def find(name):
            found = root.findChild(QObject, name)
            if found is None:
                def visual(item):
                    if item.objectName() == name:
                        return item
                    for child in item.childItems():
                        result = visual(child)
                        if result is not None:
                            return result
                    return None
                found = visual(root.contentItem())
            assert found is not None, name
            return found

        def click(name):
            item = find(name)
            point = item.mapToScene(item.boundingRect().center())
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
            QTest.qWait(35)

        def save(name):
            if evidence:
                assert root.grabWindow().save(str(evidence / (name + ".png")))

        def tune(**changes):
            data = config.snapshot()
            data.update(changes)
            assert bridge.saveSettings(json.dumps(data, ensure_ascii=False))
            QTest.qWait(180)  # Wait for the existing 140ms navigation-rail transition.
            root.setProperty("toastText", "")

        # Real input preservation across source and settings tabs, not just object existence.
        click("nav1")
        find("urlField").setProperty("text", "https://example.invalid/retained")
        selected = [row["video_id"] for row in bridge.source if row["selected"]]
        click("sourceLocalTab")
        assert root.property("sourceTab") == 1
        click("sourceYoutubeTab")
        assert find("urlField").property("text").endswith("/retained")
        assert selected == [row["video_id"] for row in bridge.source if row["selected"]]
        click("nav5")
        click("settingsTab3")
        prompt = find("promptField")
        draft = prompt.property("text") + "\nمسودة لا تُحفظ تلقائيًا"
        prompt.setProperty("text", draft)
        for tab in range(4):
            click(f"settingsTab{tab}")
            assert root.property("settingsTab") == tab
        assert prompt.property("text") == draft
        assert "مسودة" not in config.data["prompt"]
        # Restore to the saved value before the settings resize matrix.
        prompt.setProperty("text", config.data["prompt"])

        # 432 page configurations, plus all internal settings tabs. No field resets.
        cases = 0
        for width, height in (() if modals_only else ((1060, 720), (1380, 900), (1920, 1080))):
            root.setWidth(width); root.setHeight(height)
            for theme, scale, compact, collapsed in itertools.product(("dark", "light"), (0.8, 1.0, 1.4), (False, True), (False, True)):
                tune(theme=theme, ui_scale=scale, ui_compact=compact, ui_rail_collapsed=collapsed)
                for page in range(6):
                    root.setProperty("page", page)
                    QTest.qWait(15)
                    assert root.property("page") == page
                    assert root.property("dark") == (theme == "dark")
                    assert root.width() == width and root.height() == height, (width, height, root.width(), root.height())
                    for name in ("runButton", "forceButton", f"nav{page}"):
                        control = find(name)
                        point = control.mapToScene(control.boundingRect().center())
                        if not (0 <= point.x() <= width and 0 <= point.y() <= height):
                            save("layout-failure")
                            print("Layout failure", name, "rail", find("navRail").property("x"), find("navRail").property("width"), "units", root.property("contentUnits"))
                        assert 0 <= point.x() <= width and 0 <= point.y() <= height, (width, scale, name, point)
                    if scale == 1.0 and compact and not collapsed:
                        save(f"{theme}-{width}-page-{page}")
                    if width == 1060 and scale == 1.4 and compact and not collapsed:
                        save(f"{theme}-1060-140-page-{page}")
                    if page == 5:
                        for tab in range(4):
                            root.setProperty("settingsTab", tab)
                            QTest.qWait(5)
                        assert find("promptField").property("text") == config.data["prompt"]
                    cases += 1

        # Updates preserve the jobs/source/results/files offsets and keyboard focus.
        tune(theme="dark", ui_scale=1.0, ui_compact=True, ui_rail_collapsed=False)
        root.setWidth(1380); root.setHeight(900)
        for page, name in ((0, "jobsList"), (1, "sourceList"), (3, "resultsList"), (4, "fileList")):
            root.setProperty("page", page)
            QTest.qWait(50)
            view = find(name)
            if view.property("contentHeight") > view.property("height") + 80:
                view.setProperty("contentY", 80)
                before = view.property("contentY")
                focus = find("runButton")
                focus.forceActiveFocus()
                bridge.refresh()
                store.event(jobs[0]["id"], "سطر سجل محلي أثناء التمرير")
                bridge.refresh()
                QTest.qWait(50)
                assert abs(view.property("contentY") - before) <= 1, name
                assert focus.hasActiveFocus(), name

        # Every modal fits the minimum window and keeps its actions reachable.
        dialogs = ("folderDialog", "jobDialog", "retryWarning", "bulkRetry", "paidRerun", "workerDialog", "forceDialog", "providersDialog", "deleteProviderDialog", "deleteFileDialog", "advancedDialog", "templatesDialog", "renameAccountDialog", "removeAccountDialog", "forgetInternetDialog")
        root.setWidth(1060); root.setHeight(720)
        modal_cases = 0
        for theme, scale in itertools.product(("dark", "light"), (1.0, 1.4)):
            tune(theme=theme, ui_scale=scale)
            for name in dialogs:
                find("runButton").forceActiveFocus()
                root.setProperty("selectedJob", bridge.jobById(jobs[1]["id"]))
                root.setProperty("bulkGroup", {"stage": "deepgram", "status": "uncertain", "label": "نتائج غير مؤكدة", "ids": [jobs[1]["id"]]})
                root.setProperty("pendingAccount", {"id": "demo", "alias": "حساب تجريبي"})
                root.setProperty("pendingProvider", {"id": "demo", "name": "مزود اختباري"})
                root.setProperty("pendingFile", {"path": str(home / "demo.txt"), "name": "ملف اختباري طويل الاسم.txt"})
                if name == "workerDialog":
                    QMetaObject.invokeMethod(root, "editWorker", Q_ARG("QVariant", config.data["deepgram_workers"][0]), Q_ARG("QVariant", "deepgram"))
                else:
                    assert QMetaObject.invokeMethod(find(name), "open")
                QTest.qWait(80)
                popup = find(name)
                assert popup.property("visible"), name
                assert popup.property("x") >= -1 and popup.property("y") >= -1, (name, popup.property("y"))
                assert popup.property("x") + popup.property("width") <= root.width() + 1, name
                assert popup.property("y") + popup.property("height") <= root.height() + 1, (name, popup.property("height"))
                if name == "forceDialog":
                    for action_name in ("forceDefaultButton", "forceApplyButton"):
                        action = find(action_name)
                        point = action.mapToScene(action.boundingRect().center())
                        assert popup.property("y") <= point.y() <= popup.property("y") + popup.property("height"), action_name
                if name == "workerDialog":
                    deadline = time.monotonic() + 5
                    while bridge.catalogBusy and time.monotonic() < deadline:
                        QTest.qWait(30)
                    assert not bridge.catalogBusy, "Synthetic model catalog did not settle"
                    QTest.qWait(50)
                    model = find("workerModelBox")
                    model.setProperty("editText", "nova-custom-draft")
                    tune(theme="light" if theme == "dark" else "dark")
                    assert model.property("editText") == "nova-custom-draft"
                    tune(theme=theme)
                save(f"{theme}-{round(scale * 100)}-{name}")
                # Live palette switch must not destroy an already-open draft.
                if name == "templatesDialog":
                    assert find("templateEditorBox").property("count") == len(bridge.templates)
                QTest.keyClick(root, Qt.Key_Escape)
                QTest.qWait(25)
                assert not popup.property("visible"), name
                assert find("runButton").hasActiveFocus(), name
                modal_cases += 1

        root.close(); harness.shutdown_and_join()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)
        dispose_bridge(bridge); app.processEvents(); store.db.close()
    failures = [m for m in messages if m.startswith("file:") or any(term in m.lower() for term in ("error", "unable", "cannot", "binding loop", "failed to load", "invalid property", "overridden", "recursive rearrange", "only supports"))]
    if evidence:
        (evidence / "qml-messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not failures, failures[:20]
    print(f"Redesign acceptance passed: {cases} page configurations, {modal_cases} modal configurations, retained drafts/selections, stable models/focus, no QML errors.")


if __name__ == "__main__":
    main()
