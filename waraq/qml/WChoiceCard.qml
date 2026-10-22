import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A radio choice with a complete label, explanation and a separate focus ring.
RadioButton {
    id: control
    property QtObject theme: fallback
    property string description: ""
    signal stepRequested()
    Theme { id: fallback }
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    padding: Math.round(18 * theme.scale)
    spacing: Math.round(12 * theme.scale)
    implicitWidth: Math.round(320 * theme.scale)
    implicitHeight: Math.max(Math.round(116 * theme.scale), contentItem.implicitHeight + topPadding + bottomPadding)
    Accessible.name: text
    Accessible.description: description
    font.family: theme.fontFamily
    font.weight: theme.fontWeight
    font.pixelSize: theme.fs(16)
    indicator: Item {}
    background: Rectangle {
        radius: 10
        color: control.checked || control.hovered ? control.theme.hover : control.theme.inset
        border.color: control.checked ? control.theme.accent : control.theme.line
        border.width: control.checked ? 2 : 1
        Rectangle {
            anchors.fill: parent
            anchors.margins: -3
            radius: 13
            color: "transparent"
            border.color: control.theme.accent
            border.width: 2
            visible: control.activeFocus
        }
    }
    contentItem: RowLayout {
        layoutDirection: Qt.RightToLeft
        spacing: control.spacing
        Rectangle {
            Layout.alignment: Qt.AlignTop
            Layout.topMargin: Math.round(3 * control.theme.scale)
            implicitWidth: Math.round(20 * control.theme.scale)
            implicitHeight: implicitWidth
            radius: width / 2
            color: control.theme.panel
            border.width: control.checked ? 2 : 1
            border.color: control.checked ? control.theme.accent : control.theme.muted
            Rectangle {
                anchors.centerIn: parent
                width: parent.width / 2
                height: width
                radius: width / 2
                color: control.theme.accent
                visible: control.checked
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: Math.round(7 * control.theme.scale)
            Text {
                Layout.fillWidth: true
                text: control.text
                font: control.font
                color: control.theme.ink
                horizontalAlignment: Text.AlignRight
                wrapMode: Text.WordWrap
            }
            Text {
                Layout.fillWidth: true
                text: control.description
                font.family: control.theme.fontFamily
                font.weight: control.theme.fontWeight
                font.pixelSize: control.theme.fs(13)
                color: control.theme.muted
                horizontalAlignment: Text.AlignRight
                wrapMode: Text.WordWrap
            }
        }
    }
    Keys.onPressed: function(event) {
        if ([Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down].indexOf(event.key) >= 0) {
            stepRequested()
            event.accepted = true
        }
    }
}
