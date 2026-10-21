"""Offline native navigation rail acceptance and mandatory screenshot evidence.

Run: python tests/verify_navigation_rail.py [evidence-directory] [--baseline QML-directory]
Uses offscreen/software Qt, temporary configuration/SQLite and dummy credentials.
The optional original QML baseline adds before images and dark pixel regression;
it never skips the final production acceptance matrix.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
if os.environ.get("WINDIR"):
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["WINDIR"]) / "Fonts"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt, QUrl, qInstallMessageHandler
from PySide6.QtGui import QColor, QFont
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qt_test_support import dispose_bridge
from scheduler_test_support import SchedulerHarness
from waraq.bridge import Bridge
from waraq.config import Config
from waraq.scheduler import Scheduler
from waraq.store import Store

ICONS = ("home", "plus", "workers", "library", "folder", "settings")
PALETTES = {
    "light": dict(rail="#e5f0f1", ink="#183c43", muted="#4d6e75", active="#0b7478", hover="#dceced", selected="#ffffff"),
    "dark": dict(rail="#082026", ink="#e8f3ef", muted="#a7c3c8", active="#4bc6c3", hover="#14363e", selected="#06272b"),
}


def descendants(item):
    yield item
    for child in item.childItems():
        yield from descendants(child)


def find(root, name):
    result = root.findChild(QObject, name)
    if result is None:
        result = next((item for item in descendants(root.contentItem()) if item.objectName() == name), None)
    assert result is not None, name
    return result


def evaluate(item, expression):
    query = QQmlExpression(QQmlEngine.contextForObject(item), item, expression)
    result, _undefined = query.evaluate()
    assert not query.hasError(), (expression, query.error().toString())
    return result


def color(item, property_name="color"):
    value = item.property(property_name)
    assert isinstance(value, QColor), (item.objectName(), property_name, value)
    return value.name().lower()


def geometry(item):
    origin = item.mapToScene(QPointF(0, 0))
    return tuple(round(value, 3) for value in (origin.x(), origin.y(), item.width(), item.height()))


def click(root, item):
    center = item.mapToScene(item.boundingRect().center()).toPoint()
    QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
    QTest.qWait(12)


def park_pointer(root):
    QTest.mouseMove(root, QPoint(4, root.height() - 4))
    focused = root.activeFocusItem()
    if focused is not None:
        focused.setFocus(False, Qt.OtherFocusReason)
    root.contentItem().forceActiveFocus(Qt.OtherFocusReason)
    QTest.qWait(12)


def grab(root, evidence, name):
    root.update()
    QTest.qWait(35)
    frame = root.grabWindow()
    assert not frame.isNull(), name
    assert frame.save(str(evidence / (name + ".png"))), name
    return frame


def pixel_at(frame, item, x, y):
    point = item.mapToScene(QPointF(x, y))
    ratio = frame.devicePixelRatio()
    return frame.pixelColor(round(point.x() * ratio), round(point.y() * ratio)).name().lower()


def rail_frame(frame, rail):
    ratio = frame.devicePixelRatio()
    return frame.copy(*(round(value * ratio) for value in geometry(rail)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", nargs="?", type=Path, default=ROOT / "output/light-sidebar-ui")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    app.styleHints().setUseHoverEffects(True)
    app.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    messages, measurements, engines = [], [], []
    qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    with tempfile.TemporaryDirectory(prefix="waraq-rail-", ignore_cleanup_errors=True) as temporary, SchedulerHarness() as harness, \
            patch("waraq.bridge.deepgram_models", return_value=[]):
        home = Path(temporary)
        config, store = Config(home), Store(home / "tasks.db")
        config.migration_note = ""
        config.save(dict(config.snapshot(), theme="light", ui_scale=1.0, ui_rail_collapsed=False, ui_compact=False))
        keys = type("Keys", (), {"get": lambda *args: "", "set": lambda *args: None})()
        scheduler = Scheduler(store, config, keys)
        scheduler.tick = lambda: None
        scheduler.accounts.request_refresh = lambda *args, **kwargs: False
        scheduler.accounts.models = lambda: []
        bridge = Bridge(store, config, keys, scheduler)
        bridge.timer.stop()
        bridge._account_usage_timer.stop()

        def load(directory):
            engine = QQmlApplicationEngine()
            engines.append(engine)
            engine.rootContext().setContextProperty("backend", bridge)
            engine.load(QUrl.fromLocalFile(str(directory / "Main.qml")))
            assert engine.rootObjects(), messages
            window = engine.rootObjects()[0]
            window.setFlags(window.flags() | Qt.FramelessWindowHint)
            window.setPosition(0, 0)
            window.show()
            QTest.qWait(150)
            return window

        def tune(root, **changes):
            settings = config.snapshot()
            settings.update(changes)
            assert bridge.saveSettings(json.dumps(settings, ensure_ascii=False))
            root.setProperty("toastText", "")
            QTest.qWait(155)

        before_dark = {}
        root = None
        try:
            if args.baseline:
                baseline = load(args.baseline.resolve())
                for theme, collapsed in itertools.product(("light", "dark"), (False, True)):
                    tune(baseline, theme=theme, ui_rail_collapsed=collapsed, ui_scale=1.0, ui_compact=False)
                    baseline.setProperty("page", 0)
                    park_pointer(baseline)
                    mode = "collapsed" if collapsed else "expanded"
                    frame = grab(baseline, evidence, f"before-{theme}-{mode}")
                    if theme == "dark":
                        rail = find(baseline, "navRail")
                        before_dark[collapsed] = rail_frame(frame, rail)
                baseline.hide()
                baseline.close()

            root = load(ROOT / "waraq/qml")
            rail, toggle = find(root, "navRail"), find(root, "railToggle")
            buttons = [find(root, f"nav{i}") for i in range(6)]
            backgrounds = [find(root, f"navBackground{i}") for i in range(6)]
            labels = [find(root, f"navLabel{i}") for i in range(6)]
            images = [find(root, f"navIcon{i}") for i in range(6)]
            divider = find(root, "railDivider")
            cases = 0
            for (width, height), theme, scale, collapsed in itertools.product(
                    ((1380, 900), (1060, 720)), ("light", "dark"), (.8, 1.0, 1.4), (False, True)):
                root.setWidth(width)
                root.setHeight(height)
                tune(root, theme=theme, ui_scale=scale, ui_rail_collapsed=collapsed, ui_compact=False)
                park_pointer(root)
                palette = PALETTES[theme]
                expected_width = 64 if collapsed else 220
                assert rail.width() == expected_width
                assert abs(rail.x() + rail.width() - width) < 1
                assert rail.height() == height
                assert root.property("uiScale") == scale
                assert color(rail) == palette["rail"]
                assert divider.isVisible() == (theme == "light")
                assert divider.width() == 1 and divider.x() == 0
                assert color(divider) == ("#9fb8bd" if theme == "light" else "#39646c")
                for name, expected in (("railBrand", "ink"), ("railBrandCaption", "active"), ("railMonogram", "active"), ("railTagline", "muted"), ("railVersion", "muted")):
                    item = find(root, name)
                    assert color(item) == palette[expected], (theme, name, color(item))
                    assert item.isVisible() == (collapsed if name == "railMonogram" else not collapsed)
                stable = [geometry(item) for item in [rail, toggle, *buttons, *images]]
                for page in range(6):
                    click(root, buttons[page])
                    park_pointer(root)
                    assert root.property("page") == page
                    assert stable == [geometry(item) for item in [rail, toggle, *buttons, *images]]
                    for index, (button, background, label, image) in enumerate(zip(buttons, backgrounds, labels, images)):
                        selected = index == page
                        assert color(background) == (palette["active"] if selected else "#000000")
                        assert background.property("color").alpha() == (255 if selected else 0)
                        assert color(label) == palette["selected" if selected else "ink"]
                        assert label.isVisible() == (not collapsed)
                        assert evaluate(button, "Accessible.name") == label.property("text")
                        assert evaluate(button, "ToolTip.text") == label.property("text")
                        expected_icon = ICONS[index] + (("-rail-selected-light" if selected else "-rail-light") if theme == "light" else ("-active" if selected else "")) + ".svg"
                        assert image.property("source").fileName() == expected_icon
                        assert evaluate(image, "status === Image.Ready"), expected_icon
                        assert image.width() == 20 and image.height() == 20
                        ix, iy, iw, ih = geometry(image)
                        bx, by, bw, bh = geometry(button)
                        assert bx <= ix and ix + iw <= bx + bw and by <= iy and iy + ih <= by + bh
                    # Actual hover input distinguishes the active page from normal pages.
                    for index in (page, (page + 1) % 6):
                        QTest.mouseMove(root, buttons[index].mapToScene(buttons[index].boundingRect().center()).toPoint())
                        QTest.qWait(12)
                        assert buttons[index].property("hovered"), (width, height, theme, scale, collapsed, page, index, geometry(buttons[index]), root.isActive(), buttons[index].property("hoverEnabled"))
                        assert color(backgrounds[index]) == palette["active" if index == page else "hover"]
                    park_pointer(root)
                    cases += 1
                measurements.append(dict(width=width, height=height, theme=theme, scale=scale, collapsed=collapsed, geometry=stable))

            # Native Tab navigation and Space activation, with visible focus on each page.
            for theme, collapsed in itertools.product(("light", "dark"), (False, True)):
                root.setWidth(1380)
                root.setHeight(900)
                tune(root, theme=theme, ui_scale=1.0, ui_rail_collapsed=collapsed)
                park_pointer(root)
                root.setProperty("page", 5)
                buttons[0].forceActiveFocus(Qt.TabFocusReason)
                palette = PALETTES[theme]
                for index, button in enumerate(buttons):
                    assert button.property("activeFocus"), (theme, collapsed, index)
                    background = backgrounds[index]
                    assert evaluate(background, "border.width") == (2 if theme == "light" else 1)
                    assert QColor(evaluate(background, "border.color")).name() == palette["ink"]
                    if collapsed:
                        QTest.mouseMove(root, button.mapToScene(button.boundingRect().center()).toPoint())
                        QTest.qWait(440)
                        assert evaluate(button, "ToolTip.visible")
                        assert evaluate(button, "ToolTip.toolTip.visible")
                    QTest.keyClick(root, Qt.Key_Space)
                    QTest.qWait(15)
                    assert root.property("page") == index
                    selected_focus = "#ffffff" if theme == "light" else palette["ink"]
                    assert QColor(evaluate(background, "border.color")).name() == selected_focus
                    if index == 0:
                        mode = "collapsed" if collapsed else "expanded"
                        frame = grab(root, evidence, f"focus-{theme}-{mode}")
                        assert pixel_at(frame, background, 0, background.height() / 2) == selected_focus
                        if frame.devicePixelRatio() == 1:
                            # Verify the renderer's second pixel as well as the QML pen width.
                            assert pixel_at(frame, background, 1, background.height() / 2) == (selected_focus if theme == "light" else palette["active"])
                            assert pixel_at(frame, background, 2, background.height() / 2) == palette["active"]
                    QTest.mouseMove(root, QPoint(4, root.height() - 4))
                    if index < 5:
                        QTest.keyClick(root, Qt.Key_Tab)
                assert evaluate(toggle, "Accessible.name")
                assert evaluate(toggle, "ToolTip.text") == evaluate(toggle, "Accessible.name")
                toggle.forceActiveFocus(Qt.TabFocusReason)
                QTest.keyClick(root, Qt.Key_Space)
                QTest.qWait(155)
                assert root.property("railOpen") == collapsed
                assert config.snapshot()["ui_rail_collapsed"] == (not collapsed)

            # Theme changes are live: all six pages, collapse state and saved settings survive.
            for collapsed, page in itertools.product((False, True), range(6)):
                tune(root, theme="light", ui_rail_collapsed=collapsed, ui_scale=1.0)
                root.setProperty("page", page)
                geometry_before = [geometry(item) for item in [rail, toggle, *buttons, *images]]
                saved = config.snapshot()
                for theme in ("dark", "light"):
                    tune(root, theme=theme)
                    assert root.property("page") == page
                    assert root.property("railOpen") == (not collapsed)
                    assert geometry_before == [geometry(item) for item in [rail, toggle, *buttons, *images]]
                    assert {key: value for key, value in config.snapshot().items() if key != "theme"} == {key: value for key, value in saved.items() if key != "theme"}
                    assert color(rail) == PALETTES[theme]["rail"]

            # Non-null saved frames plus sampled rendered colors and exact original dark rail.
            for theme, collapsed in itertools.product(("light", "dark"), (False, True)):
                tune(root, theme=theme, ui_rail_collapsed=collapsed, ui_scale=1.0, ui_compact=False)
                root.setProperty("page", 0)
                park_pointer(root)
                mode = "collapsed" if collapsed else "expanded"
                frame = grab(root, evidence, f"after-{theme}-{mode}")
                palette = PALETTES[theme]
                assert pixel_at(frame, rail, rail.width() - 3, rail.height() - 3) == palette["rail"]
                assert pixel_at(frame, backgrounds[0], 4, backgrounds[0].height() / 2) == palette["active"]
                if theme == "dark" and collapsed in before_dark:
                    after = rail_frame(frame, rail)
                    assert after == before_dark[collapsed], ("Dark rail rendering changed", mode)
            bad = [message for message in messages if message.startswith("file:") or any(term in message.lower() for term in ("binding loop", "failed to load", "referenceerror", "typeerror", "recursive rearrange", "cannot assign"))]
            assert not bad, bad
        finally:
            for engine in engines:
                for window in engine.rootObjects():
                    window.close()
            harness.shutdown_and_join()
            for engine in engines:
                engine.deleteLater()
                QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)
            dispose_bridge(bridge)
            app.processEvents()
            store.db.close()
            (evidence / "navigation-rail-qml-messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            (evidence / "navigation-rail-measurements.json").write_text(json.dumps(measurements, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Navigation rail passed: {cases} layouts; all six routes, colors, ready icons, hover, keyboard focus/activation, collapsed tooltips/accessibility, live themes, fixed geometry and saved screenshots ({time.monotonic() - started:.1f}s).")


if __name__ == "__main__":
    main()
