import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    property QtObject theme: fallback
    property string placeholderText: ""
    Theme { id: fallback }
    font.family: control.theme.fontFamily
    font.weight: control.theme.fontWeight
    font.pixelSize: theme.fs(14)
    implicitHeight: Math.round(36 * theme.scale)
    implicitWidth: 180
    leftPadding: 10; rightPadding: 30
    focusPolicy: Qt.StrongFocus
    palette.text: theme.ink
    palette.base: theme.inset
    palette.highlight: theme.accent
    palette.highlightedText: theme.accentInk
    background: Rectangle { radius: 6; color: control.theme.inset; border.width: control.activeFocus ? 2 : 1; border.color: control.activeFocus ? control.theme.accent : control.theme.line }
    indicator: Item {
        x: control.width - width - 10; y: (control.height - height) / 2; width: 12; height: 8
        Rectangle { x: 1; y: 3; width: 7; height: 2; rotation: 45; color: control.theme.muted }
        Rectangle { x: 5; y: 3; width: 7; height: 2; rotation: -45; color: control.theme.muted }
    }
    contentItem: TextField {
        text: control.editable ? control.editText : (control.currentIndex < 0 ? control.placeholderText : control.displayText)
        enabled: control.editable
        autoScroll: control.editable
        readOnly: control.down
        inputMethodHints: control.inputMethodHints
        validator: control.validator
        selectByMouse: true
        padding: 0
        font: control.font; color: !control.editable && control.currentIndex < 0 ? control.theme.muted : control.theme.ink
        selectionColor: control.theme.accent
        selectedTextColor: control.theme.accentInk
        verticalAlignment: Text.AlignVCenter; horizontalAlignment: control.editable ? Text.AlignLeft : Text.AlignRight
        background: Item {}
    }
    delegate: ItemDelegate {
        required property var model
        required property int index
        width: control.width
        implicitHeight: Math.max(34, label.implicitHeight + 16)
        highlighted: control.highlightedIndex === index
        contentItem: Text {
            id: label
            text: control.textRole ? model[control.textRole] : model.modelData
            font: control.font; color: highlighted ? control.theme.accentInk : control.theme.ink
            horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
            wrapMode: Text.WrapAnywhere
        }
        background: Rectangle { color: highlighted ? control.theme.accent : control.theme.panel }
    }
    popup: Popup {
        y: control.height + 4; width: control.width
        height: Math.min(contentItem.implicitHeight + 8, control.Window.window ? control.Window.window.height * 0.45 : 300)
        padding: 4
        contentItem: ListView {
            clip: true; implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
        background: Rectangle { radius: 6; color: control.theme.panel; border.color: control.theme.line }
    }
}
