"""Native, offline StageBadge state, palette, typography and SVG checks."""
import json
import sys
from pathlib import Path
from PySide6.QtCore import QObject, QUrl, qInstallMessageHandler
from PySide6.QtGui import QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
STATES = ("pending", "running", "completed", "skipped", "failed", "blocked", "uncertain", "paused", "cancelled")


def contrast(a, b):
    def luminance(color):
        channels = (color.redF(), color.greenF(), color.blueF())
        return sum((c / 12.92 if c <= 0.04045 else ((c + .055) / 1.055) ** 2.4) * w
                   for c, w in zip(channels, (.2126, .7152, .0722)))
    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + .05) / (lo + .05)


def main():
    evidence = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "output/bold-stage-ui"
    evidence.mkdir(parents=True, exist_ok=True)
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
    Theme { id: visualTheme; dark: darkMode; scale: uiScale }
    color: visualTheme.panel
    ColumnLayout {
        anchors.centerIn: parent; spacing: 16
        Text { text: "مراحل المعالجة · جميع الحالات"; color: visualTheme.ink; font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; font.pixelSize: visualTheme.fs(20); Layout.alignment: Qt.AlignRight }
        GridLayout {
            columns: 3; layoutDirection: Qt.RightToLeft; columnSpacing: 12; rowSpacing: 8
            Repeater {
                model: %s
                delegate: StageBadge {
                    required property var modelData
                    objectName: modelData.name + "-" + modelData.status
                    theme: visualTheme
                    step: modelData
                }
            }
        }
    }
}'''
    rows = [{"name": stage, "label": label, "status": status}
            for status in STATES
            for stage, label in (("download", "التنزيل"), ("deepgram", "التفريغ"), ("opencode", "ملف Word"))]
    # URL gives relative components and local SVGs their production directory.
    engine.loadData((source % json.dumps(rows, ensure_ascii=False)).encode("utf-8"),
                    QUrl.fromLocalFile(str(ROOT / "waraq/qml/StageBadgeValidation.qml")))
    assert engine.rootObjects(), messages
    window = engine.rootObjects()[0]
    cases = 0
    for dark in (True, False):
        window.setProperty("darkMode", dark)
        for scale in (.8, 1., 1.4):
            window.setProperty("uiScale", scale)
            QTest.qWait(80)
            for row in rows:
                def locate(item, name):
                    if item.objectName() == name:
                        return item
                    for child in item.childItems():
                        result = locate(child, name)
                        if result is not None:
                            return result
                    return None
                badge = locate(window.contentItem(), row["name"] + "-" + row["status"])
                assert badge is not None
                theme = badge.property("theme")
                expected_role = {"download": "accent", "deepgram": "info", "opencode": "gold"}[row["name"]]
                assert badge.property("stageColor") == theme.property(expected_role)
                if row["status"] in ("failed", "blocked", "uncertain"):
                    assert badge.property("statusColor") == theme.property("danger")
                for role in ("stageColor", "statusColor"):
                    assert contrast(badge.property(role), badge.property("color")) >= 4.5, (dark, row, role)
                assert badge.property("hazardous") == (row["status"] in ("failed", "blocked", "uncertain"))
                assert badge.property("statusLabel") and badge.property("description")
                for item in badge.findChildren(QObject):
                    if item.metaObject().indexOfProperty("font") >= 0:
                        font = item.property("font")
                        assert font.weight() == QFont.Weight.Bold
                        assert font.family() == "Segoe UI"
                    if item.metaObject().indexOfProperty("source") >= 0:
                        assert (ROOT / "waraq/qml" / item.property("source").toString()).is_file(), (row, item.property("source"))
                cases += 1
            assert window.grabWindow().save(str(evidence / f"stage-states-{'dark' if dark else 'light'}-{round(scale*100)}.png"))
    window.close()
    failures = [message for message in messages if message.startswith("file:") or "error" in message.lower() or "failed" in message.lower()]
    assert not failures, failures
    print(f"StageBadge acceptance passed: {cases} stage/state/theme/scale combinations; contrast >=4.5, Bold text, local SVGs.")


if __name__ == "__main__":
    main()
