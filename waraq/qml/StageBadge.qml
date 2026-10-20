import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Informational stage identity and execution state; never starts an action.
Rectangle {
    id: badge
    property QtObject theme: fallback
    property var step: ({name: "download", label: "التنزيل", status: "pending"})
    // Dense queue variants preserve the same state, accessible name and icons.
    property bool compact: false
    property bool mini: false
    Theme { id: fallback }
    readonly property string stageName: step.name || "download"
    readonly property string stageLabel: step.label || stageName
    readonly property string status: step.status || "pending"
    readonly property var statusLabels: ({pending: "انتظار", running: "جارٍ", completed: "مكتمل", skipped: "متجاوز", failed: "فشل", blocked: "يتطلب إجراء", uncertain: "غير مؤكدة", paused: "متوقف", cancelled: "ملغى"})
    readonly property string statusLabel: statusLabels[status] || status
    readonly property bool hazardous: ["failed", "blocked", "uncertain"].indexOf(status) >= 0
    readonly property color stageColor: stageName === "download" ? theme.accent : stageName === "deepgram" ? theme.info : theme.gold
    readonly property color statusColor: hazardous ? theme.danger : status === "completed" ? theme.success : status === "running" ? theme.info : status === "paused" ? theme.warning : theme.muted
    readonly property string iconSuffix: theme.dark ? "-dark.svg" : "-light.svg"
    readonly property string description: stageLabel + " — " + statusLabel
    implicitWidth: compact || mini ? compactLabels.implicitWidth + Math.round(12 * theme.scale) : Math.max(Math.round(118 * theme.scale), labels.implicitWidth + Math.round(16 * theme.scale))
    implicitHeight: compact || mini ? Math.round(26 * theme.scale) : labels.implicitHeight + Math.round(12 * theme.scale)
    width: implicitWidth
    height: implicitHeight
    radius: 6
    color: Qt.tint(theme.panel, Qt.rgba(stageColor.r, stageColor.g, stageColor.b, theme.stageTintOpacity))
    border.width: hazardous || status === "running" ? 2 : 1
    border.color: hazardous ? theme.danger : stageColor
    Accessible.role: Accessible.StaticText
    Accessible.name: description
    ColumnLayout {
        id: labels
        visible: !badge.compact && !badge.mini
        anchors.centerIn: parent
        spacing: Math.round(3 * badge.theme.scale)
        RowLayout {
            Layout.alignment: Qt.AlignRight
            spacing: Math.round(5 * badge.theme.scale)
            Text {
                text: badge.stageLabel
                color: badge.stageColor
                font.family: badge.theme.fontFamily
                font.weight: badge.theme.fontWeight
                font.pixelSize: badge.theme.fs(12)
                Accessible.ignored: true
            }
            Image {
                source: "icons/stage-" + badge.stageName + badge.iconSuffix
                Layout.preferredWidth: Math.round(15 * badge.theme.scale)
                Layout.preferredHeight: Layout.preferredWidth
                sourceSize.width: width; sourceSize.height: height
                Accessible.ignored: true
            }
        }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            spacing: Math.round(5 * badge.theme.scale)
            Text {
                text: badge.statusLabel
                color: badge.statusColor
                font.family: badge.theme.fontFamily
                font.weight: badge.theme.fontWeight
                font.pixelSize: badge.theme.fs(11)
                Accessible.ignored: true
            }
            Image {
                source: "icons/status-" + badge.status + badge.iconSuffix
                Layout.preferredWidth: Math.round(12 * badge.theme.scale)
                Layout.preferredHeight: Layout.preferredWidth
                sourceSize.width: width; sourceSize.height: height
                Accessible.ignored: true
            }
        }
    }
    RowLayout {
        id: compactLabels
        visible: badge.compact || badge.mini
        anchors.centerIn: parent
        spacing: Math.round(4 * badge.theme.scale)
        Image {
            source: "icons/status-" + badge.status + badge.iconSuffix
            Layout.preferredWidth: Math.round(12 * badge.theme.scale)
            Layout.preferredHeight: Layout.preferredWidth
            sourceSize.width: width; sourceSize.height: height
            Accessible.ignored: true
        }
        Text {
            visible: !badge.mini
            text: badge.stageLabel
            color: badge.stageColor
            font.family: badge.theme.fontFamily
            font.weight: badge.theme.fontWeight
            font.pixelSize: badge.theme.fs(11)
            Accessible.ignored: true
        }
        Image {
            source: "icons/stage-" + badge.stageName + badge.iconSuffix
            Layout.preferredWidth: Math.round(14 * badge.theme.scale)
            Layout.preferredHeight: Layout.preferredWidth
            sourceSize.width: width; sourceSize.height: height
            Accessible.ignored: true
        }
    }
    activeFocusOnTab: true
    HoverHandler { id: hover }
    ToolTip.visible: hover.hovered || activeFocus
    ToolTip.delay: 500
    ToolTip.text: description
}
