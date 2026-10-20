import QtQuick
import QtQuick.Controls

Switch {
    id: control
    property QtObject theme: fallback
    Theme { id: fallback }
    font.family: "Segoe UI"
    font.pixelSize: theme.fs(13)
    implicitHeight: Math.max(30, contentItem.implicitHeight + 8)
    spacing: 8
    leftPadding: 6; rightPadding: 6
    focusPolicy: Qt.StrongFocus
    Accessible.name: text
    indicator: Rectangle {
        implicitWidth: 36; implicitHeight: 20
        x: control.width - width - control.rightPadding
        y: (control.height - height) / 2
        radius: 10
        color: control.checked ? control.theme.accent : control.theme.inset
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? control.theme.ink : control.theme.line
        Rectangle {
            x: control.checked ? parent.width - width - 3 : 3
            y: 3; width: 14; height: 14; radius: 7
            color: control.checked ? control.theme.accentInk : control.theme.muted
        }
    }
    contentItem: Text {
        text: control.text; font: control.font; color: control.theme.ink
        rightPadding: control.indicator.width + control.spacing
        verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignRight
        wrapMode: Text.WordWrap
        opacity: control.enabled ? 1 : 0.55
    }
    background: Rectangle { radius: 6; color: control.hovered ? control.theme.hover : "transparent" }
}
