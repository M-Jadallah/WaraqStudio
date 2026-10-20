import QtQuick
import QtQuick.Controls

CheckBox {
    id: control
    property QtObject theme: fallback
    Theme { id: fallback }
    font.family: control.theme.fontFamily
    font.weight: control.theme.fontWeight
    font.pixelSize: theme.fs(13)
    implicitHeight: Math.max(30, contentItem.implicitHeight + 8)
    spacing: 8
    leftPadding: 6
    rightPadding: 6
    focusPolicy: Qt.StrongFocus
    Accessible.name: text
    indicator: Rectangle {
        implicitWidth: 18; implicitHeight: 18
        x: control.width - width - control.rightPadding
        y: (control.height - height) / 2
        radius: 4
        color: control.checked ? control.theme.accent : control.theme.inset
        border.color: control.activeFocus ? control.theme.accent : control.theme.line
        border.width: control.activeFocus ? 2 : 1
        Item {
            anchors.centerIn: parent; width: 12; height: 10; visible: control.checked
            Rectangle { x: 1; y: 5; width: 5; height: 2; rotation: 45; color: control.theme.accentInk; visible: control.checkState !== Qt.PartiallyChecked }
            Rectangle { x: 4; y: 4; width: 9; height: 2; rotation: -45; color: control.theme.accentInk; visible: control.checkState !== Qt.PartiallyChecked }
            Rectangle { anchors.centerIn: parent; width: 8; height: 2; color: control.theme.accentInk; visible: control.checkState === Qt.PartiallyChecked }
        }
    }
    contentItem: Text {
        text: control.text; font: control.font; color: control.theme.ink
        rightPadding: control.indicator.width + control.spacing
        verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignRight
        wrapMode: Text.WordWrap
        opacity: control.enabled ? 1 : 0.55
    }
    background: Rectangle { radius: 6; color: control.hovered ? control.theme.hover : "transparent"; border.color: control.activeFocus ? control.theme.accent : "transparent" }
}
