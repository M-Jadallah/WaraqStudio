import QtQuick
import QtQuick.Controls

Button {
    id: control
    property bool primary: false
    property bool danger: false
    property color foreground: "#dce6f4"
    property color surface: "#1c2a40"
    implicitHeight: 42
    implicitWidth: Math.max(88, contentItem.implicitWidth + 30)
    font.family: "Segoe UI"
    font.pixelSize: 14
    font.weight: Font.DemiBold
    focusPolicy: Qt.StrongFocus
    Accessible.name: text
    background: Rectangle {
        radius: 8
        color: control.primary ? (control.down ? "#28b499" : "#42d9b6") : (control.hovered ? Qt.lighter(control.surface, 1.17) : control.surface)
        opacity: control.enabled ? 1 : 0.4
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? "#42d9b6" : (control.primary ? "transparent" : "#35445a")
    }
    contentItem: Text {
        text: control.text
        font: control.font
        color: control.primary ? "#09261f" : (control.danger ? "#ff9696" : control.foreground)
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        opacity: control.enabled ? 1 : 0.5
    }
}
