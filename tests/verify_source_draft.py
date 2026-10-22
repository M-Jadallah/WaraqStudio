"""Native source-page acceptance and optional screenshot matrix; no real services."""
from __future__ import annotations

import faulthandler
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QObject, QPoint, QPointF, QUrl, Qt, qInstallMessageHandler
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qt_test_support import dispose_bridge
from scheduler_test_support import SchedulerHarness
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store


def find(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        match = find(child, name)
        if match is not None:
            return match
    return None


def main():
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="waraq-source-ui-", ignore_cleanup_errors=True) as temp, SchedulerHarness() as harness:
        home = Path(temp)
        config, store = Config(home), Store(home / "test.db")
        config.data["accounts_enabled"] = False
        config.data["opencode_templates"].append({"id": "chosen", "name": "قالب الدروس المختار", "text": "اقرأ {transcript_path} واكتب {output_path}"})
        material = store.create_folder("مادة الدروس التجريبية")
        chosen_output = home / "نتائج الدفعة المختارة"
        chosen_output.mkdir()
        original_output = config.data["output_root"]
        scheduler = Scheduler(store, config, None)
        scheduler.accounts.request_refresh = lambda *args, **kwargs: False
        scheduler.tick = lambda: None
        discovery_calls = []
        def discover(url, cfg, cancel, refresh):
            discovery_calls.append(url)
            return {"title": "دروس عربية — قائمة YouTube", "items": [
                {"video_id": f"lesson{i}", "title": f"الدرس {i}: موضوع عربي طويل لاختبار عرض عناوين المصادر", "position": i,
                 "url": f"https://www.youtube.com/watch?v=lesson{i}", "available": True, "selected": True, "duration": 2400}
                for i in range(1, 9)
            ]}
        scheduler.youtube.discover = discover
        bridge = Bridge(store, config, None, scheduler)
        bridge.timer.stop()
        bridge._account_usage_timer.stop()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        root = engine.rootObjects()[0]
        try:
            QTest.qWait(150)
            root.setProperty("page", 1)
            QTest.qWait(80)
            def control(name):
                item = find(root.contentItem(), name)
                assert item is not None, "Missing source control: " + name
                return item
            scroll = control("sourceScroll")
            flick = scroll.property("contentItem")
            def reveal(item):
                assert item.isVisible(), "Hidden control: " + item.objectName()
                QTest.qWait(10)
                top = scroll.mapToScene(QPointF(0, 0)).y()
                bottom = top + scroll.property("height")
                item_top = item.mapToScene(QPointF(0, 0)).y()
                item_bottom = item_top + item.property("height")
                offset = 0
                if item_top < top + 8:
                    offset = item_top - top - 8
                elif item_bottom > bottom - 8:
                    offset = item_bottom - bottom + 8
                if offset:
                    maximum = max(0, flick.property("contentHeight") - flick.property("height"))
                    flick.setProperty("contentY", min(maximum, max(0, flick.property("contentY") + offset)))
                    QTest.qWait(30)
                position = item.mapToScene(item.boundingRect().center())
                assert top <= position.y() <= bottom, (item.objectName(), position.y(), top, bottom)
                return position
            def click(name):
                item = control(name)
                position = reveal(item)
                assert item.property("enabled"), "Disabled source control: " + name
                QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(round(position.x()), round(position.y())))
                QTest.qWait(40)
            def choose(name, index, expected=None):
                item = control(name)
                reveal(item)
                item.forceActiveFocus()
                QTest.keyClick(root, Qt.Key_Space)
                QTest.qWait(20)
                QTest.keyClick(root, Qt.Key_Home)
                for _ in range(index):
                    QTest.keyClick(root, Qt.Key_Down)
                QTest.keyClick(root, Qt.Key_Return)
                QTest.qWait(60)
                expected = index if expected is None else expected
                assert item.property("currentIndex") == expected, (name, item.property("currentIndex"), expected)
            def wait_for(predicate):
                deadline = time.monotonic() + 3
                while not predicate() and time.monotonic() < deadline:
                    QTest.qWait(15)
                assert predicate(), messages[-8:]
            def assert_blank():
                for name in ("templateBox", "sourceFolderCombo", "sourceOutputFolderCombo", "sourcePreset"):
                    assert control(name).property("currentIndex") == -1, (name, control(name).property("currentIndex"))
                assert not control("enqueueSourceButton").property("enabled")
            def assert_tools(kind):
                for candidate, name in (("youtube", "sourceYoutubeTools"), ("transcript", "sourceTranscriptTools"), ("audio", "sourceAudioTools")):
                    assert control(name).isVisible() == (candidate == kind), (kind, name)
                assert control("sourcePreset").isVisible() == (kind == "youtube")
            assert_blank()
            assert_tools("youtube")
            choose("templateBox", 1)
            choose("sourceFolderCombo", 0)
            choose("sourceOutputFolderCombo", 0)
            assert bridge.sourceDraft.templateId == "chosen"
            assert bridge.sourceDraft.folderId == material
            assert bridge.sourceDraft.outputRoot == original_output
            # Opening the picker from the real ComboBox and cancelling must not
            # destroy its binding to the draft's selected path.
            with patch("waraq.source_draft.QFileDialog.getExistingDirectory", return_value=""):
                choose("sourceOutputFolderCombo", 1, expected=0)
            assert control("sourceOutputFolderCombo").property("currentIndex") == 0
            bridge.sourceDraft.setOutputRoot(str(chosen_output))
            QTest.qWait(40)
            assert control("sourceOutputFolderCombo").property("currentValue") == str(chosen_output), "Browse cancel destroyed output ComboBox binding"
            config.data["opencode_templates"].insert(0, {"id": "another", "name": "قالب آخر", "text": "آخر {transcript_path} {output_path}"})
            bridge.settingsChanged.emit()
            QTest.qWait(40)
            assert control("templateBox").property("currentValue") == "chosen"
            assert control("sourceFolderCombo").property("currentValue") == material
            assert control("sourceOutputFolderCombo").property("currentValue") == str(chosen_output)
            # Keyboard focus traverses real form controls.
            template = control("templateBox")
            reveal(template)
            template.forceActiveFocus()
            assert template.property("activeFocus")
            QTest.keyClick(root, Qt.Key_Tab)
            QTest.qWait(20)
            assert not template.property("activeFocus")
            QTest.keyClick(root, Qt.Key_Backtab)
            QTest.qWait(20)
            assert root.activeFocusItem() is not None
            click("sourceLocalTab")
            assert_tools("transcript")
            transcript = home / "تفريغ درس عربي.txt"
            original_bytes = "تفريغ عربي جاهز\r\n".encode("utf-8-sig")
            transcript.write_bytes(original_bytes)
            with patch("waraq.source_draft.QFileDialog.getOpenFileNames", return_value=([str(transcript)], "")):
                click("importTranscriptsButton")
            assert bridge.sourceDraft.itemCount == 1
            assert control("sourceList").property("count") == 1
            assert not store.jobs()
            assert not list(chosen_output.iterdir())
            click("sourceAudioTab")
            assert_tools("audio")
            audio = home / "درس صوتي.mp3"
            audio.write_bytes(b"synthetic audio")
            with patch("waraq.source_draft.QFileDialog.getOpenFileNames", return_value=([str(audio)], "")):
                click("importLocalButton")
            assert bridge.sourceDraft.itemCount == 1
            click("sourceYoutubeTab")
            assert_tools("youtube")
            url = control("urlField")
            reveal(url)
            url.forceActiveFocus()
            app.clipboard().setText("https://www.youtube.com/playlist?list=synthetic")
            QTest.keyClick(root, Qt.Key_V, Qt.ControlModifier)
            QTest.qWait(20)
            click("discoverButton")
            wait_for(lambda: bridge.sourceDraft.itemCount == 8 and not bridge.sourceDraft.busy)
            assert len(discovery_calls) == 1 and not store.jobs()
            choose("sourcePreset", 0)
            assert bridge.sourceDraft.preset == "full"
            assert control("enqueueSourceButton").property("enabled")
            # Successful real UI confirmation consumes all drafts and selects no
            # new defaults, including the ComboBox previously opened for browse.
            click("sourceLocalTab")
            assert bridge.sourceDraft.itemCount == 1
            click("enqueueSourceButton")
            assert root.property("page") == 0
            assert len(store.jobs()) == 1
            assert Path(store.jobs()[0]["transcript"]).read_bytes() == original_bytes
            assert Path(store.jobs()[0]["config"]["output_root"]) == chosen_output.resolve()
            assert config.data["output_root"] == original_output
            root.setProperty("page", 1)
            QTest.qWait(50)
            assert_blank()
            for tab, kind in (("sourceAudioTab", "audio"), ("sourceLocalTab", "transcript"), ("sourceYoutubeTab", "youtube")):
                click(tab)
                assert_tools(kind)
                assert bridge.sourceDraft.itemCount == 0
            # Rebuild a visual-only draft for all screenshot cases.
            bridge.sourceDraft.setTemplateId("chosen")
            bridge.sourceDraft.setFolderId(material)
            bridge.sourceDraft.setOutputRoot(str(chosen_output))
            bridge.sourceDraft.setPreset("full")
            bridge.sourceDraft.stagePaths([str(transcript)], "transcript")
            bridge.sourceDraft.stagePaths([str(audio)], "audio")
            bridge.sourceDraft.discover("https://www.youtube.com/playlist?list=synthetic", False)
            wait_for(lambda: bridge.sourceDraft.itemCount == 8 and not bridge.sourceDraft.busy)
            # Pair boundary sizes with scale and rail extremes. Together these
            # cases exercise stacked and side-by-side source layouts.
            matrix = [(1060, 720, .8, False), (1060, 720, 1.4, False),
                      (1060, 720, 1.4, True), (1380, 900, 1., False),
                      (1920, 1080, 1., True), (1920, 1080, 1.4, False)]
            screenshots = 0
            for theme in ("dark", "light"):
                for width, height, scale, collapsed in matrix:
                    settings = config.snapshot()
                    settings.update(theme=theme, ui_scale=scale, ui_rail_collapsed=collapsed)
                    assert bridge.saveSettings(json.dumps(settings, ensure_ascii=False))
                    root.setWidth(width)
                    root.setHeight(height)
                    QTest.qWait(60)
                    for kind in ("youtube", "transcript", "audio"):
                        bridge.sourceDraft.setKind(kind)
                        root.setProperty("toastText", "")
                        flick.setProperty("contentY", 0)
                        QTest.qWait(40)
                        assert_tools(kind)
                        assert flick.property("contentWidth") <= flick.property("width") + 1, (width, scale, collapsed)
                        settings_card = control("sourceSettingsCard")
                        input_card = control("sourceInputCard")
                        items_list = control("sourceList")
                        assert settings_card.property("width") <= scroll.property("availableWidth") + 1
                        settings_position = settings_card.mapToScene(QPointF(0, 0))
                        input_position = input_card.mapToScene(QPointF(0, 0))
                        list_position = items_list.mapToScene(QPointF(0, 0))
                        assert abs(settings_position.x() - input_position.x()) <= 1
                        assert input_position.y() >= settings_position.y() + settings_card.height()
                        if root.property("contentUnits") >= 850:
                            assert list_position.x() + items_list.width() < settings_position.x()
                        else:
                            assert list_position.y() > input_position.y() + input_card.height()
                        footer = control("enqueueSourceButton")
                        reveal(footer)
                        point = footer.mapToScene(footer.boundingRect().center())
                        assert 0 <= point.x() <= root.width() and 0 <= point.y() <= root.height()
                        if output:
                            prefix = f"source-{theme}-{kind}-{width}x{height}-{int(scale * 100)}-{'collapsed' if collapsed else 'expanded'}"
                            assert root.grabWindow().save(str(output / (prefix + "-footer.png")))
                            flick.setProperty("contentY", 0)
                            QTest.qWait(20)
                            assert root.grabWindow().save(str(output / (prefix + ".png")))
                            screenshots += 2
            # Keyboard/programmatic focus must bring offscreen source controls
            # into view without relying on this test's mouse reveal() helper.
            settings = config.snapshot()
            settings.update(ui_scale=1.4, ui_rail_collapsed=False)
            assert bridge.saveSettings(json.dumps(settings, ensure_ascii=False))
            root.setWidth(1060)
            root.setHeight(720)
            bridge.sourceDraft.setKind("transcript")
            QTest.qWait(80)
            template = control("templateBox")
            template.forceActiveFocus()
            QTest.qWait(80)
            flick.setProperty("contentY", 0)
            def focus_visible(name):
                item = control(name)
                item.forceActiveFocus()
                QTest.qWait(80)
                assert item.property("activeFocus"), name
                viewport_top = scroll.mapToScene(QPointF(0, 0)).y()
                viewport_bottom = viewport_top + scroll.height()
                item_top = item.mapToScene(QPointF(0, 0)).y()
                item_bottom = item_top + item.height()
                assert viewport_top - 1 <= item_top and item_bottom <= viewport_bottom + 1, (
                    "Focused source control remained offscreen", name, item_top, item_bottom,
                    viewport_top, viewport_bottom, flick.property("contentY"))
            focus_visible("importTranscriptsButton")
            assert flick.property("contentY") > 0
            focus_visible("templateBox")
            assert flick.property("contentY") < 200
            flick.setProperty("contentY", 0)
            focus_visible("enqueueSourceButton")
            assert flick.property("contentY") > 0
            focus_visible("templateBox")
            failures = [m for m in messages if any(token in m.lower() for token in
                        ("referenceerror", "typeerror", "binding loop", "cannot assign", "unable to assign", "cannot read"))]
            assert not failures, failures
            print(f"Native source UI passed: real dropdown activation/cancel/reset, mandatory choices, 3 input kinds, staged imports, queued YouTube discovery, batch snapshot, keyboard focus/automatic scroll at 140%, 36 layout cases, {screenshots} screenshots; no QML binding errors.")
        finally:
            root.close()
            bridge.sourceDraft.close()
            harness.shutdown_and_join()
            engine.deleteLater()
            app.processEvents()
            dispose_bridge(bridge)
            with store.lock:
                store.db.close()


if __name__ == "__main__":
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, repeat=True)
    try:
        main()
    finally:
        faulthandler.cancel_dump_traceback_later()
