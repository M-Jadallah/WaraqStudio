import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A quota is unknown unless that individual window has a valid measured value.
Item {
    id: cell
    required property var theme
    property real uiScale: 1
    property var windowData: ({})
    property string label: ""
    property string resetText: "غير معروف"
    readonly property bool known: windowData !== null
        && ["available", "stale"].indexOf(windowData.status) >= 0
        && typeof windowData.remainingPercent === "number"
        && isFinite(windowData.remainingPercent)
        && windowData.remainingPercent >= 0 && windowData.remainingPercent <= 100
    readonly property bool stale: windowData !== null && windowData.status === "stale"
    readonly property real remaining: known ? windowData.remainingPercent : 0
    readonly property string percentText: known ? (Math.round(remaining * 10) / 10).toString() + "٪" : "غير معروف"
    readonly property bool attention: known && remaining <= 15
    Accessible.role: Accessible.StaticText
    Accessible.name: label + ": " + percentText + (stale ? " · قراءة قديمة" : "") + " · التجدد (توقيت الجهاز): " + resetText
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 10 * cell.uiScale; spacing: 4 * cell.uiScale
        Text {
            text: cell.label; color: cell.theme.muted
            font.family: cell.theme.fontFamily; font.weight: cell.theme.fontWeight; font.pixelSize: cell.theme.fs(11)
            Layout.fillWidth: true; horizontalAlignment: Text.AlignRight
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 4
            Text {
                visible: cell.stale; text: "قديمة"; color: cell.theme.warning
                font.family: cell.theme.fontFamily; font.weight: cell.theme.fontWeight; font.pixelSize: cell.theme.fs(10)
            }
            Item { Layout.fillWidth: true }
            Text {
                objectName: "quotaRemainingNumber"; text: cell.percentText
                color: cell.attention ? cell.theme.warning : cell.known ? cell.theme.ink : cell.theme.muted
                font.family: cell.theme.fontFamily; font.weight: cell.theme.fontWeight; font.pixelSize: cell.theme.fs(cell.known ? 23 : 14)
            }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 5 * cell.uiScale; radius: height / 2; color: cell.theme.line
            Rectangle {
                anchors.right: parent.right; height: parent.height; radius: parent.radius
                width: cell.known ? parent.width * cell.remaining / 100 : 0
                color: cell.attention ? cell.theme.warning : cell.theme.accent
            }
        }
        Text {
            text: "التجدد: " + cell.resetText; color: cell.theme.muted
            font.family: cell.theme.fontFamily; font.weight: cell.theme.fontWeight; font.pixelSize: cell.theme.fs(10)
            Layout.fillWidth: true; horizontalAlignment: Text.AlignRight; wrapMode: Text.WordWrap
        }
        Text {
            visible: cell.attention; text: "متبقٍ منخفض"; color: cell.theme.warning
            font.family: cell.theme.fontFamily; font.weight: cell.theme.fontWeight; font.pixelSize: cell.theme.fs(10)
            Layout.fillWidth: true; horizontalAlignment: Text.AlignRight
        }
        Item { Layout.fillHeight: true }
    }
}
