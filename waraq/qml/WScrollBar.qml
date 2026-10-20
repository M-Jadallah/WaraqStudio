import QtQuick
import QtQuick.Controls

ScrollBar {
    id: control
    property QtObject theme: fallback
    Theme { id: fallback }
    minimumSize: 0.05
    padding: 2
    contentItem: Rectangle {
        implicitWidth: control.orientation === Qt.Vertical ? 9 : 36
        implicitHeight: control.orientation === Qt.Vertical ? 36 : 9
        radius: 4; color: control.pressed ? control.theme.accent : control.theme.line
        opacity: control.size < 1 ? 0.9 : 0
    }
}
