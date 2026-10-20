import QtQuick
import QtQuick.Controls

Slider {
    id: control
    property QtObject theme: fallback
    Theme { id: fallback }
    implicitHeight: 36
    background: Rectangle {
        x: control.leftPadding; y: (control.height - height) / 2
        width: control.availableWidth; height: 5; radius: 3; color: control.theme.line
        Rectangle { width: control.visualPosition * parent.width; height: parent.height; radius: 3; color: control.theme.accent }
    }
    handle: Rectangle {
        x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
        y: (control.height - height) / 2
        implicitWidth: 20; implicitHeight: 20; radius: 10
        color: control.theme.accent; border.width: control.activeFocus ? 3 : 1
        border.color: control.activeFocus ? control.theme.ink : control.theme.line
    }
}
