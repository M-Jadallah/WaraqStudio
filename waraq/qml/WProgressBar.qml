import QtQuick
import QtQuick.Controls

ProgressBar {
    id: control
    property QtObject theme: fallback
    Theme { id: fallback }
    implicitHeight: 8
    implicitWidth: 60
    background: Rectangle { implicitHeight: 8; radius: 4; color: control.theme.inset; border.color: control.theme.line }
    contentItem: Item {
        clip: true
        Rectangle {
            height: parent.height; radius: 4; color: control.theme.accent
            width: control.indeterminate ? parent.width * 0.3 : parent.width * control.visualPosition
            x: control.indeterminate ? (parent.width - width) / 2 : 0
            // Honest unknown progress, without a new perpetual animation/timer.
            opacity: control.indeterminate ? 0.65 : 1
        }
    }
}
