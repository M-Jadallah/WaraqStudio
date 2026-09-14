import QtQuick
import QtQuick.Controls

TextField {
    id: control
    property color fieldBackground: "#0f192a"
    property color fieldText: "#e5edf8"
    implicitHeight: 44
    font.family: "Segoe UI"
    font.pixelSize: 15
    color: fieldText
    placeholderTextColor: "#8698b0"
    selectionColor: "#218b7b"
    selectByMouse: true
    leftPadding: 12
    rightPadding: 12
    background: Rectangle {
        color: control.fieldBackground
        radius: 8
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? "#42d9b6" : "#35445a"
    }
}
