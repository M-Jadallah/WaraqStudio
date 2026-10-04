"""Native queue scrolling stress, isolated from user data and network services.

Each case runs in its own process so a blocked Qt loop has a bounded watchdog.
Run directly; the regular pytest suite never creates GUI applications here.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def probe(count):
    from PySide6.QtCore import QPoint, QPointF, QTimer, Qt, QUrl, qInstallMessageHandler
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import waraq.bridge as bridge_module
    from waraq.bridge import Bridge
    from waraq.config import Config
    from waraq.scheduler import Scheduler
    from waraq.store import Store

    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    messages = []
    qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    bridge_module.deepgram_models = lambda: []
    bridge_module.opencode_models = lambda *args: []

    def find(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            result = find(child, name)
            if result is not None:
                return result
        return None

    with tempfile.TemporaryDirectory(prefix="waraq-scroll-", ignore_cleanup_errors=True) as temp:
        home = Path(temp)
        store, config = Store(home / "queue.db"), Config(home)
        config.data.update(ui_scale=1.0, ui_compact=True, ui_rail_collapsed=True)
        ids = store.add([
            {"video_id": "scroll-" + str(index), "position": index + 1, "url": "",
             "title": ("آخر درس needle " if index == count - 1 else "درس ") + str(index + 1)
                      + " شرح طويل في مسائل الدرس وأمثلة متنوعة " * 4}
            for index in range(count)
        ], config.snapshot())
        keys = type("Keys", (), {"get": lambda self, name: "", "set": lambda *args: None})()
        scheduler = Scheduler(store, config, keys)
        bridge = Bridge(store, config, keys, scheduler)
        bridge.timer.stop()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", bridge)
        started = time.perf_counter()
        engine.load(QUrl.fromLocalFile(str(ROOT / "waraq/qml/Main.qml")))
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setWidth(1060)
        window.setHeight(720)
        QTest.qWait(150)
        load_seconds = time.perf_counter() - started
        view = find(window.contentItem(), "jobsList")
        assert view and view.property("count") == count
        assert not scheduler.running and scheduler.thread is None
        samples = [time.perf_counter()]
        heartbeat = QTimer()
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: samples.append(time.perf_counter()))
        heartbeat.start()
        calls, positions = [], []

        def timed(action, delay=12):
            before = time.perf_counter()
            action()
            QTest.qWait(delay)
            calls.append(time.perf_counter() - before)
            positions.append(float(view.property("contentY")))

        point = view.mapToScene(view.boundingRect().center())
        QTest.mouseMove(window, QPoint(int(point.x()), int(point.y())))
        for index in range(90):
            direction = -120 if index < 65 else 120
            timed(lambda: QTest.wheelEvent(window, point, QPoint(0, direction)))
            if index % 15 == 0:
                # Stopped queues still receive refreshes and job-result updates.
                changed_id = ids[min(index, count - 1)]
                store.set_stage(changed_id, "download", "failed" if index % 30 else "pending", "اختبار تحديث أثناء التمرير")
                timed(bridge.refresh)
        wheel_range = max(positions) - min(positions)
        assert wheel_range > 100, "wheel events did not scroll the queue"
        bars = [item for item in view.childItems()
                if "ScrollBar" in item.metaObject().className() and item.height() > item.width()]
        assert bars, "queue scrollbar not rendered"
        bar = bars[0]
        start = bar.mapToScene(QPointF(bar.width() / 2, bar.height() / 2))
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, QPoint(int(start.x()), int(start.y())))
        for step in range(50):
            point_on_bar = bar.mapToScene(QPointF(bar.width() / 2, bar.height() * (0.08 + 0.86 * step / 49)))
            timed(lambda: QTest.mouseMove(window, QPoint(int(point_on_bar.x()), int(point_on_bar.y()))))
        QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, QPoint(int(point_on_bar.x()), int(point_on_bar.y())))
        scroll_range = max(positions) - min(positions)
        assert scroll_range > min(1000, view.property("contentHeight") / 2), scroll_range
        before = time.perf_counter()
        window.setProperty("selectedQueueIds", ids)
        QTest.qWait(50)
        select_seconds = time.perf_counter() - before
        assert find(window.contentItem(), "selectedJobsCount").property("text") == "المحدد: " + str(count)
        window.setProperty("selectedQueueIds", [])
        QTest.qWait(30)
        heartbeat.stop()
        gaps = [end - begin for begin, end in zip(samples, samples[1:])]
        result = {"jobs": count, "load_seconds": round(load_seconds, 3),
                  "scroll_call_max_seconds": round(max(calls), 3),
                  "heartbeat_gap_max_seconds": round(max(gaps), 3),
                  "selection_seconds": round(select_seconds, 3), "scroll_range": round(scroll_range)}
        result["wheel_range"] = round(wheel_range)
        print(json.dumps(result), flush=True)
        assert max(gaps) < 1.5, result
        assert select_seconds < 1.5, result
        assert not messages, messages
        bridge.timer.stop()
        scheduler.shutdown()
        window.close()
        engine.deleteLater()
        app.processEvents()
        with store.lock:
            store.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=int)
    args = parser.parse_args()
    if args.case:
        probe(args.case)
        return
    for count in (100, 500, 2000):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--case", str(count)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=45)
        print(result.stdout, end="", flush=True)
        assert result.returncode == 0, result.stderr
    print("Native stopped-queue scrolling stress passed; no services or user data used.")


if __name__ == "__main__":
    main()
