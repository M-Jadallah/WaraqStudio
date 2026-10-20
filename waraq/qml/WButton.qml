import QtQuick
import QtQuick.Controls

Button {
    id: control
    property bool primary: false
    property bool danger: false
    property bool gold: false
    property QtObject theme: fallback
    Theme { id: fallback }
    property real uiScale: 1.0
    property color foreground: theme.ink
    property color surface: theme.inset
    implicitHeight: Math.round(36 * uiScale)
    implicitWidth: Math.max(Math.round(76 * uiScale), contentItem.implicitWidth + Math.round(22 * uiScale))
    font.family: control.theme.fontFamily
    font.weight: control.theme.fontWeight
    font.pixelSize: Math.max(12, Math.round(14 * uiScale))

    focusPolicy: Qt.StrongFocus
    Accessible.name: text
    background: Rectangle {
        radius: 6
        color: control.primary || control.gold ? (control.gold ? control.theme.gold : control.theme.accent) : (control.hovered ? control.theme.hover : control.surface)
        scale: control.down ? 0.98 : 1
        opacity: control.enabled ? 1 : 0.4
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? control.theme.accent : (control.danger ? control.theme.danger : control.theme.line)
        Behavior on color { ColorAnimation { duration: 110 } }
    }
    contentItem: Text {
        text: control.text
        font: control.font
        color: control.danger ? control.theme.danger : control.gold ? control.theme.goldInk : control.primary ? control.theme.accentInk : control.foreground
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        opacity: control.enabled ? 1 : 0.5
    }
    ToolTip.visible: hovered && text.length > 0 && width < contentItem.implicitWidth + 12
    ToolTip.text: text
    ToolTip.delay: 500
}
