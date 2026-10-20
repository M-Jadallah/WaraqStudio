import QtQuick
import QtQuick.Controls

Dialog {
    id: control
    property QtObject theme: fallback
    property Item returnFocus: null
    Theme { id: fallback }
    padding: 16
    topPadding: 12
    modal: true
    focus: true
    font.family: "Segoe UI"
    font.pixelSize: theme.fs(14)
    palette.window: theme.panel
    palette.windowText: theme.ink
    palette.text: theme.ink
    palette.base: theme.inset
    palette.button: theme.inset
    palette.buttonText: theme.ink
    palette.highlight: theme.accent
    palette.highlightedText: theme.accentInk
    background: Rectangle { color: control.theme.panel; border.color: control.theme.line; radius: 10 }
    header: Label {
        text: control.title; visible: control.title.length > 0
        color: control.theme.ink; font.family: "Segoe UI"; font.pixelSize: control.theme.fs(19); font.bold: true
        horizontalAlignment: Text.AlignRight; wrapMode: Text.WordWrap
        padding: 18
        background: Rectangle { color: control.theme.inset; radius: 10 }
    }
    footer: DialogButtonBox {
        visible: count > 0
        standardButtons: control.standardButtons
        padding: 12; spacing: 8
        delegate: WButton { theme: control.theme; uiScale: control.theme.scale }
        background: Rectangle { color: control.theme.panel }
        onAccepted: control.accept()
        onRejected: control.reject()
    }
    Overlay.modal: Rectangle { color: "#B0082026" }
    onAboutToShow: {
        var owner = control.parent ? control.parent.Window.window : null
        returnFocus = owner ? owner.activeFocusItem : null
    }
    onClosed: if (returnFocus) returnFocus.forceActiveFocus()
}
