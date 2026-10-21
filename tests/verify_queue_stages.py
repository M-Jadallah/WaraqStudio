"""Offline mixed-task queue: fixed RTL stage columns and requested-only badges.

Run: python tests/verify_queue_stages.py [evidence-directory]
All source files, configuration and SQLite data are temporary.
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

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, Q_ARG, Qt, QUrl, qInstallMessageHandler
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

STAGES = ("download", "deepgram", "opencode")


def descendants(item):
    yield item
    for child in item.childItems():
        yield from descendants(child)


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
    with tempfile.TemporaryDirectory(prefix="waraq-stage-columns-", ignore_cleanup_errors=True) as temp, SchedulerHarness() as harness:
        home = Path(temp)
        config, store = Config(home), Store(home / "tasks.db")
        config.migration_note = ""
        text, audio = home / "imported.txt", home / "imported.mp3"
        text.write_text("تفريغ محلي لاختبار مراحل Word فقط", encoding="utf-8")
        audio.write_bytes(b"synthetic local audio; providers never run")
        fixtures = []

        def add(label, enabled, expected, source="", skipped=""):
            saved = config.snapshot()
            saved.update({stage + "_enabled": stage in enabled for stage in STAGES})
            index = len(fixtures) + 1
            ident = store.add([{"video_id": f"stages-{index}", "title": label, "position": index,
                                "url": "https://example.invalid/offline" if not source else ""}],
                              saved, input_path=source)[0]
            for stage in expected:
                store.set_stage(ident, stage, "skipped" if stage == skipped else "completed")
            fixtures.append((ident, tuple(expected), skipped))

        # Mixed types appear together at the top of the real queue in evidence.
        add("المعالجة الكاملة — تنزيل وتفريغ وWord", STAGES, STAGES)
        add("التفريغ فقط — تنزيل وتفريغ", STAGES[:2], STAGES[:2])
        add("التنزيل فقط", STAGES[:1], STAGES[:1])
        add("ملف TXT محلي — Word فقط", STAGES, ("opencode",), str(text))
        add("صوت محلي — تفريغ وWord", STAGES, STAGES[1:], str(audio))
        add("إعداد مخصص للصوت — تفريغ فقط", ("deepgram",), ("deepgram",), str(audio))
        add("إعداد مخصص للصوت — Word فقط", ("opencode",), ("opencode",), str(audio))
        add("مرحلة Word مطلوبة ومتجاوزة", STAGES, STAGES, skipped="opencode")
        add("إعداد TXT دون مرحلة قابلة للتطبيق", ("deepgram",), (), str(text))

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
        root.show()
        QTest.qWait(180)

        def find(name):
            result = root.findChild(QObject, name)
            if result is None:
                result = next((item for item in descendants(root.contentItem()) if item.objectName() == name), None)
            assert result is not None, name
            return result

        def names(item, prefix):
            return {child.objectName()[len(prefix):] for child in descendants(item) if child.objectName().startswith(prefix)}

        view, popup = find("jobsList"), find("jobDialog")
        measurements = []
        cases = 0
        for (width, height), theme, scale, compact in itertools.product(
                ((1380, 900), (1060, 720)), ("dark", "light"), (.8, 1., 1.4), (False, True)):
            root.setWidth(width)
            root.setHeight(height)
            settings = config.snapshot()
            # Changing current settings must never rewrite a saved task's request.
            settings.update(theme=theme, ui_scale=scale, ui_compact=compact,
                            download_enabled=True, deepgram_enabled=False, opencode_enabled=False,
                            ui_rail_collapsed=False)
            assert bridge.saveSettings(json.dumps(settings, ensure_ascii=False))
            root.setProperty("page", 0)
            root.setProperty("toastText", "")
            QTest.qWait(180)
            centers = {}
            column_centers = None
            rows = []
            for index, (ident, expected, skipped) in enumerate(fixtures):
                assert QMetaObject.invokeMethod(view, "positionViewAtIndex", Q_ARG(int, index), Q_ARG(int, 0))
                QMetaObject.invokeMethod(view, "forceLayout")
                QTest.qWait(20)
                job = bridge.jobById(ident)
                assert tuple(step["name"] for step in job["steps"]) == expected, (ident, expected, job["steps"])
                row, flow, column = (find(prefix + ident) for prefix in ("queueRow-", "queueStages-", "queueStageColumn-"))
                assert flow.parentItem() == column, "The named stage Row stays directly under the named column"
                assert flow.metaObject().className().startswith("QQuickRow"), flow.metaObject().className()
                assert flow.width() > 0 and flow.height() > 0, (expected, flow.width(), flow.height())
                assert names(flow, "queueStage-" + ident + "-") == set(expected)
                slots = [find("queueStageSlot-" + ident + "-" + stage) for stage in STAGES]
                slot_x = [slot.mapToScene(slot.boundingRect().center()).x() for slot in slots]
                assert slot_x[0] > slot_x[1] > slot_x[2], slot_x
                if column_centers is None:
                    column_centers = slot_x
                else:
                    assert all(abs(x - reference) <= 1 for x, reference in zip(slot_x, column_centers)), (expected, slot_x, column_centers)
                for stage, slot, x in zip(STAGES, slots, slot_x):
                    assert slot.width() > 0 and slot.height() > 0
                    if stage in expected:
                        badge = find("queueStage-" + ident + "-" + stage)
                        assert badge.isVisible()
                        point = badge.mapToScene(badge.boundingRect().center())
                        assert abs(point.x() - x) <= 1, (stage, point.x(), x)
                        if stage in centers:
                            assert abs(point.x() - centers[stage]) <= 1, (stage, ident, point.x(), centers[stage])
                        else:
                            centers[stage] = point.x()
                        left = badge.mapToItem(slot, badge.boundingRect().topLeft()).x()
                        right = badge.mapToItem(slot, badge.boundingRect().bottomRight()).x()
                        assert left >= -1 and right <= slot.width() + 1, (stage, left, right, slot.width())
                        bottom = badge.mapToItem(row, badge.boundingRect().bottomRight()).y()
                        assert bottom <= row.height() + 1, (stage, bottom, row.height())
                        if stage == skipped:
                            assert badge.property("status") == "skipped" and not badge.property("completed")
                    else:
                        # Empty slots own no badge, keyboard target or hover tooltip.
                        for child in descendants(slot):
                            assert not child.property("activeFocusOnTab"), (ident, stage, child.objectName())
                            assert child.metaObject().indexOfProperty("stageName") < 0, (ident, stage)
                            assert child.metaObject().indexOfProperty("description") < 0, (ident, stage)
                            assert not any("HoverHandler" in obj.metaObject().className() for obj in child.children()), (ident, stage)
                root.setProperty("selectedJob", job)
                assert QMetaObject.invokeMethod(popup, "open")
                QTest.qWait(20)
                assert names(root.contentItem(), "jobDialogStage-") == set(expected), (ident, expected)
                for stage in expected:
                    badge = find("jobDialogStage-" + stage)
                    assert badge.isVisible()
                    if stage == skipped:
                        assert badge.property("status") == "skipped" and not badge.property("completed")
                assert QMetaObject.invokeMethod(popup, "close")
                QTest.qWait(10)
                rows.append({"id": ident, "stages": expected, "slotCenters": slot_x})
            cases += 1
            measurements.append({"width": width, "height": height, "theme": theme, "scale": scale, "compact": compact, "rows": rows})
            view.setProperty("contentY", 0)
            QTest.qWait(40)
            if evidence:
                capture = root.grabWindow()
                assert not capture.isNull(), (width, theme, scale, compact)
                assert capture.save(str(evidence / f"queue-{width}-{theme}-{round(scale*100)}-{'compact' if compact else 'normal'}.png"))

        # Keep each dialog open while real store/model updates change execution
        # state. Even a newly running omitted stage must never acquire a badge.
        transitions = 0
        for index, changing in ((0, "deepgram"), (2, "download")):
            ident, expected, _ = fixtures[index]
            assert QMetaObject.invokeMethod(view, "positionViewAtIndex", Q_ARG(int, index), Q_ARG(int, 0))
            QTest.qWait(30)
            before = {stage: find("queueStageSlot-" + ident + "-" + stage).mapToScene(
                find("queueStageSlot-" + ident + "-" + stage).boundingRect().center()).x() for stage in STAGES}
            root.setProperty("selectedJob", bridge.jobById(ident))
            assert QMetaObject.invokeMethod(popup, "open")
            QTest.qWait(30)
            for status in ("failed", "pending", "running", "completed"):
                store.set_stage(ident, changing, status, progress=63 if status == "running" else -1)
                if index == 2:
                    store.set_stage(ident, "deepgram", status)
                    store.set_stage(ident, "opencode", status)
                bridge.refresh()
                QTest.qWait(40)
                flow = find("queueStages-" + ident)
                assert names(flow, "queueStage-" + ident + "-") == set(expected)
                assert names(root.contentItem(), "jobDialogStage-") == set(expected)
                for stage in STAGES:
                    slot = find("queueStageSlot-" + ident + "-" + stage)
                    assert abs(slot.mapToScene(slot.boundingRect().center()).x() - before[stage]) <= 1, (ident, status, stage)
                for prefix in ("queueStage-" + ident + "-", "jobDialogStage-"):
                    badge = find(prefix + changing)
                    assert badge.property("status") == status, (ident, prefix, status)
                    assert badge.property("completed") == (status == "completed")
                transitions += 1
            assert QMetaObject.invokeMethod(popup, "close")
            QTest.qWait(30)

        root.close()
        harness.shutdown_and_join()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)
        dispose_bridge(bridge)
        app.processEvents()
        store.db.close()
    errors = [message for message in messages if message.startswith("file:") or any(term in message.lower() for term in ("binding loop", "failed to load", "referenceerror", "typeerror", "recursive rearrange"))]
    if evidence:
        (evidence / "stage-column-measurements.json").write_text(json.dumps(measurements, ensure_ascii=False, indent=2), encoding="utf-8")
        (evidence / "qml-messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not errors, errors
    print(f"Queue stage columns passed: {cases} layouts, {len(fixtures)} mixed saved requests each, {transitions} live state transitions, fixed scene x, empty slots without focus/hover/badges, requested skipped stages, matching dialog names, no QML errors.")


if __name__ == "__main__":
    main()
