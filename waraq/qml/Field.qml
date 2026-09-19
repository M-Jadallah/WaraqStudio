import QtQuick
import QtQuick.Controls

TextField {
    id: control
    property color fieldBackground: "#0f192a"
    property color fieldText: "#e5edf8"
    property real uiScale: 1.0
    implicitHeight: Math.round(38 * uiScale)
    font.family: "Segoe UI"
    font.pixelSize: Math.round(15 * uiScale)
    color: fieldText
    placeholderTextColor: "#8698b0"
    selectionColor: "#218b7b"
    selectByMouse: true
    leftPadding: Math.round(12 * uiScale)
    rightPadding: Math.round(12 * uiScale)
    background: Rectangle {
        color: control.fieldBackground
        radius: 8
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? "#42d9b6" : "#35445a"
    }
}
