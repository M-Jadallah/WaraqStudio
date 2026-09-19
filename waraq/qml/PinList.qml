import QtQuick
import QtQuick.Controls

// ListView that keeps the user's scroll position when the backend republishes
// its model array wholesale (progress ticks, log lines and worker elapsed
// seconds all arrive that way and would otherwise reset the view to the top).
ListView {
    id: pin
    property real pinnedY: 0
    property bool repopulating: false
    clip: true
    // Sampling beats per-change capture: a model reset zeroes contentY before
    // our modelChanged handler can run, so only scrollable states are sampled.
    Timer {
        interval: 150
        running: pin.visible && pin.contentHeight > pin.height
        repeat: true
        onTriggered: pin.pinnedY = pin.contentY
    }
    onModelChanged: { repopulating = true; Qt.callLater(pin.restore) }
    onCountChanged: if (repopulating) Qt.callLater(pin.restore)
    onContentHeightChanged: if (repopulating && contentHeight > 0) Qt.callLater(pin.restore)
    function restore() {
        if (!repopulating || contentHeight <= 0 || count === 0) return
        repopulating = false
        // Pin only when the reset actually landed us at the top; a scroll the
        // user managed during the rebuild window wins over the saved position.
        if (contentY <= 0 && pinnedY > 0)
            contentY = Math.min(pinnedY, contentHeight - height)
    }
}
