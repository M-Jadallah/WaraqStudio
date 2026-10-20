import QtQuick
import QtQuick.Controls

TextField {
    id: control
    property QtObject theme: fallback
    Theme { id: fallback }
    property color fieldBackground: theme.inset
    property color fieldText: theme.ink
    property real uiScale: 1.0
    implicitHeight: Math.round(36 * uiScale)
    font.family: control.theme.fontFamily
    font.weight: control.theme.fontWeight
    font.pixelSize: Math.max(12, Math.round(14 * uiScale))
    color: fieldText
    placeholderTextColor: theme.muted
    selectionColor: theme.accent
    selectedTextColor: theme.accentInk
    selectByMouse: true
    leftPadding: Math.round(12 * uiScale)
    rightPadding: Math.round(12 * uiScale)
    background: Rectangle {
        color: control.fieldBackground
        radius: 6
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? control.theme.accent : control.theme.line
    }
}
