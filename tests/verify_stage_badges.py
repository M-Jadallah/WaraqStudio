"""Native, offline StageBadge state, palette, typography and SVG checks."""
import itertools
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Qt, qInstallMessageHandler
from PySide6.QtGui import QColor, QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
STATES = ("pending", "running", "completed", "skipped", "failed", "blocked", "uncertain", "paused", "cancelled")
STAGES = (("download", "التنزيل"), ("deepgram", "التفريغ"), ("opencode", "ملف Word"))
STATUS_LABELS = dict(zip(STATES, ("انتظار", "جارٍ", "مكتمل", "متجاوز", "فشل", "يتطلب إجراء", "غير مؤكدة", "متوقف", "ملغى")))
SURFACES = ("panel", "inset", "hover")
IDENTITY_ROLES = {"download": "accent", "deepgram": "info", "opencode": "gold"}
FILL_ROLES = {"download": "stageDownloadCompleted", "deepgram": "stageTranscriptCompleted", "opencode": "stageWordCompleted"}
PALETTES = {
    True: {"accent": "#4BC6C3", "info": "#8FCBFF", "gold": "#D8AB64", "stageDownloadCompleted": "#2AA8A6", "stageTranscriptCompleted": "#5C9DCF", "stageWordCompleted": "#BA8C44", "stageCompletedInk": "#06272B"},
    False: {"accent": "#0B7478", "info": "#1B62A0", "gold": "#815A1F", "stageDownloadCompleted": "#0B7478", "stageTranscriptCompleted": "#1B62A0", "stageWordCompleted": "#815A1F", "stageCompletedInk": "#FFFFFF"},
}


def luminance(color):
    channels = (color.redF(), color.greenF(), color.blueF())
    return sum((c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4) * w
               for c, w in zip(channels, (.2126, .7152, .0722)))


def contrast(a, b):
    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + .05) / (lo + .05)


def composite(foreground, background):
    alpha = foreground.alphaF()
    return QColor.fromRgbF(*(a * alpha + b * (1 - alpha) for a, b in zip(
        (foreground.redF(), foreground.greenF(), foreground.blueF()),
        (background.redF(), background.greenF(), background.blueF()))))


def locate(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        result = locate(child, name)
        if result is not None:
            return result
    return None


def icon_path(source):
    assert isinstance(source, QUrl), source
    assert source.isRelative() or source.isLocalFile(), source
    return Path(source.toLocalFile()) if source.isLocalFile() else ROOT / "waraq/qml" / source.toString()


def svg_strokes(path):
    document = ET.parse(path).getroot()
    strokes = [node.attrib["stroke"] for node in document.iter() if "stroke" in node.attrib]
    assert strokes and all(stroke not in ("none", "currentColor") for stroke in strokes), path
    return [QColor(stroke) for stroke in strokes]


def validate_badge(badge, row, dark, scale, style, surface):
    context = (dark, scale, style, surface, row)
    theme = badge.property("theme")
    for role, value in PALETTES[dark].items():
        assert theme.property(role) == QColor(value), (context, role, theme.property(role))
    completed = row["status"] == "completed"
    hazardous = row["status"] in ("failed", "blocked", "uncertain")
    identity = theme.property(IDENTITY_ROLES[row["name"]])
    fill = theme.property(FILL_ROLES[row["name"]])
    foreground = theme.property("stageCompletedInk") if completed else identity
    status_role = "danger" if hazardous else {"running": "info", "paused": "warning"}.get(row["status"], "muted")
    status_color = foreground if completed else theme.property(status_role)
    outline = fill if completed else theme.property(status_role) if hazardous or row["status"] in ("running", "paused") else identity
    assert badge.property("stageColor") == identity, context
    assert badge.property("completed") == completed, context
    assert badge.property("completedFillColor") == fill, context
    assert badge.property("foregroundColor") == foreground, context
    assert badge.property("statusColor") == status_color, context
    assert badge.property("outlineColor") == outline, context
    assert badge.property("testBorderColor") == outline, context
    assert badge.property("testBorderWidth") == (2 if hazardous or row["status"] == "running" else 1), context
    assert badge.property("hazardous") == hazardous, context
    background = badge.property("backgroundColor")
    assert background == badge.property("color"), context
    assert (background == fill if completed else background.alpha() == 0), context
    if dark:
        assert luminance(fill) < luminance(identity), context
    else:
        assert fill == identity, context
    actual_background = composite(background, theme.property(surface))
    for role, actual in (("foreground", foreground), ("status", status_color)):
        assert contrast(actual, actual_background) >= 4.5, (context, role, contrast(actual, actual_background))
    assert contrast(outline, theme.property(surface)) >= 3, (context, "border")
    assert badge.property("stageLabel") == row["label"], context
    assert badge.property("statusLabel") == STATUS_LABELS[row["status"]], context
    description = row["label"] + " — " + STATUS_LABELS[row["status"]]
    assert badge.property("description") == description, context
    assert badge.property("testAccessibleName") == description, context
    assert badge.property("testAccessibleRole") == 41, context  # Accessible.StaticText
    assert badge.property("activeFocusOnTab"), context
    assert badge.property("compact") == (style != "full"), context
    assert badge.property("mini") == (style == "mini"), context
    assert badge.property("radius") == 6, context

    suffix = ("-on-fill-dark.svg" if dark else "-on-fill-light.svg") if completed else ("-dark.svg" if dark else "-light.svg")
    expected_sources = {"stage": "stage-" + row["name"] + suffix, "status": "status-" + row["status"] + suffix}
    for kind, filename in expected_sources.items():
        assert Path(badge.property(kind + "IconSource")).name == filename, context
    text_count = image_count = 0
    assert badge.property("testImagesReady"), (context, "Image.Ready")
    for item in badge.findChildren(QObject):
        if item.metaObject().indexOfProperty("font") >= 0:
            font = item.property("font")
            assert font.weight() == QFont.Weight.Bold, context
            assert font.family() == "Segoe UI", context
            text = item.property("text")
            if text in (row["label"], STATUS_LABELS[row["status"]]):
                expected_color = status_color if text == STATUS_LABELS[row["status"]] else foreground
                assert item.property("color") == expected_color, (context, text)
                # Full stage title is 12px; status/compact labels remain 11px.
                full_title = text == row["label"] and "ColumnLayout" in item.parent().parent().metaObject().className()
                assert font.pixelSize() == max(11, round((12 if full_title else 11) * scale)), (context, text, font.pixelSize())
                if item.property("visible"):
                    text_count += 1
        if item.metaObject().indexOfProperty("source") >= 0:
            source = item.property("source")
            path = icon_path(source)
            assert path.is_file(), (context, source)
            kind = "stage" if path.name.startswith("stage-") else "status"
            assert path.name == expected_sources[kind], (context, path)
            expected_color = foreground if kind == "stage" else status_color
            for stroke in svg_strokes(path):
                assert stroke == expected_color, (context, path, stroke.name(), expected_color.name())
                assert contrast(stroke, actual_background) >= 3, (context, path)
            if item.property("visible"):
                image_count += 1
    assert text_count == {"full": 2, "compact": 1, "mini": 0}[style], (context, text_count)
    assert image_count == 2, (context, image_count)
    layouts = badge.childItems()
    full = next(item for item in layouts if "ColumnLayout" in item.metaObject().className())
    compact = next(item for item in layouts if "RowLayout" in item.metaObject().className())
    active_layout = full if style == "full" else compact
    assert active_layout.x() >= 0 and active_layout.y() >= 0, (context, "content origin")
    assert active_layout.x() + active_layout.width() <= badge.width() + .01, (context, "content width")
    assert active_layout.y() + active_layout.height() <= badge.height() + .01, (context, "content height")
    if style != "full":
        assert badge.height() == round(26 * scale), (context, "compact height contract")
    if style == "full":
        for layout in full.childItems():
            children = layout.childItems()
            assert len(children) == 2 and children[0].x() < children[1].x(), (context, "RTL label/icon order")


def main():
    evidence = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "output/bold-stage-ui"
    evidence.mkdir(parents=True, exist_ok=True)
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.environ.get("WINDIR"):
        os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["WINDIR"]) / "Fonts"))
    QQuickStyle.setStyle("Basic")
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    messages = []
    qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    engine = QQmlApplicationEngine()
    source = '''import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ApplicationWindow {
    width: 850; height: 900; visible: true
    property bool darkMode: true
    property real uiScale: 1
    property string badgeStyle: "full"
    property string surfaceRole: "panel"
    property string transitionStatus: "pending"
    Theme { id: visualTheme; dark: darkMode; scale: uiScale }
    color: visualTheme[surfaceRole]
    component ValidationBadge: StageBadge {
        id: validationBadge
        readonly property real testBorderWidth: border.width
        readonly property color testBorderColor: border.color
        readonly property string testAccessibleName: Accessible.name
        readonly property int testAccessibleRole: Accessible.role
        // Keep QQuickImageBase::Status in QML: PySide does not expose its enum converter.
        readonly property bool testImagesReady: imagesReady(validationBadge)
        function imagesReady(item) {
            if (item instanceof Image && item.status !== Image.Ready) return false
            for (var i = 0; i < item.children.length; ++i)
                if (!imagesReady(item.children[i])) return false
            return true
        }
        theme: visualTheme
        compact: badgeStyle !== "full"
        mini: badgeStyle === "mini"
    }
    ColumnLayout {
        anchors.centerIn: parent; spacing: 16
        Text { text: "مراحل المعالجة · جميع الحالات"; color: visualTheme.ink; font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; font.pixelSize: visualTheme.fs(20); Layout.alignment: Qt.AlignRight }
        GridLayout {
            objectName: "stateGrid"
            columns: 3; layoutDirection: Qt.RightToLeft; columnSpacing: 12; rowSpacing: 8
            Repeater {
                model: %s
                delegate: ValidationBadge {
                    required property var modelData
                    objectName: modelData.name + "-" + modelData.status
                    step: modelData
                }
            }
        }
        RowLayout {
            layoutDirection: Qt.RightToLeft; Layout.alignment: Qt.AlignHCenter
            Repeater {
                model: %s
                delegate: ValidationBadge {
                    required property var modelData
                    objectName: "transition-" + modelData.name
                    step: ({name: modelData.name, label: modelData.label, status: transitionStatus})
                }
            }
        }
    }
}'''
    rows = [{"name": stage, "label": label, "status": status} for status in STATES for stage, label in STAGES]
    transition_rows = [{"name": stage, "label": label} for stage, label in STAGES]
    engine.loadData((source % (json.dumps(rows, ensure_ascii=False), json.dumps(transition_rows, ensure_ascii=False))).encode("utf-8"),
                    QUrl.fromLocalFile(str(ROOT / "waraq/qml/StageBadgeValidation.qml")))
    assert engine.rootObjects(), messages
    window = engine.rootObjects()[0]
    QTest.qWait(100)
    badges = {row["name"] + "-" + row["status"]: locate(window.contentItem(), row["name"] + "-" + row["status"]) for row in rows}
    transitions = {row["name"]: locate(window.contentItem(), "transition-" + row["name"]) for row in transition_rows}
    assert all(badges.values()) and all(transitions.values()), messages
    assert locate(window.contentItem(), "stateGrid").property("layoutDirection") == Qt.RightToLeft
    cases = surface_checks = transition_checks = 0
    for dark in (True, False):
        window.setProperty("darkMode", dark)
        for scale, style in itertools.product((.8, 1., 1.4), ("full", "compact", "mini")):
            window.setProperty("uiScale", scale)
            window.setProperty("badgeStyle", style)
            for surface in SURFACES:
                window.setProperty("surfaceRole", surface)
                QTest.qWait(80)
                for row in rows:
                    name = row["name"] + "-" + row["status"]
                    assert locate(window.contentItem(), name) == badges[name], name
                    validate_badge(badges[name], row, dark, scale, style, surface)
                    surface_checks += 1
                screenshot = window.grabWindow()
                assert not screenshot.isNull(), "Window capture unavailable"
                assert screenshot.save(str(evidence / f"stage-states-{'dark' if dark else 'light'}-{round(scale*100)}-{style}-{surface}.png"))
            cases += len(rows)
    # These delegates survive completed->pending and failure->pending->running->completed,
    # including live theme changes: no stale fill, ink, status icon or accessible name.
    for scale, style, surface in itertools.product((.8, 1., 1.4), ("full", "compact", "mini"), SURFACES):
        window.setProperty("uiScale", scale)
        window.setProperty("badgeStyle", style)
        window.setProperty("surfaceRole", surface)
        QTest.qWait(40)  # Settle density/layout before recording the transition baseline.
        geometry = {}
        for status in ("completed", "pending", "failed", "pending", "running", "completed", "skipped", "cancelled"):
            window.setProperty("transitionStatus", status)
            for dark in (True, False, True):
                window.setProperty("darkMode", dark)
                QTest.qWait(12)
                for row in transition_rows:
                    badge = locate(window.contentItem(), "transition-" + row["name"])
                    assert badge == transitions[row["name"]], (row, "delegate recreated")
                    validate_badge(badge, dict(row, status=status), dark, scale, style, surface)
                    size = (badge.width(), badge.height())
                    original = geometry.setdefault(row["name"], size)
                    assert size[1] == original[1], (row, style, "height changed during transition")
                    if style != "full":
                        assert size == original, (row, style, "compact geometry changed during transition")
                    transition_checks += 1
    window.close()
    failures = [message for message in messages if message.startswith("file:") or "error" in message.lower() or "failed" in message.lower()]
    assert not failures, failures
    print(f"StageBadge acceptance passed: {cases} stage/state/theme/scale/style cases, {surface_checks} surface checks, {transition_checks} live transition/theme checks; text contrast >=4.5, icons/borders >=3, Bold text and ready local SVGs.")


if __name__ == "__main__":
    main()
