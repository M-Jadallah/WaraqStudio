import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Keeps fields alive and lets long forms scroll at 140% / minimum window size.
ScrollView {
    id: control
    default property alias bodyData: body.data
    property alias bodySpacing: body.spacing
    clip: true
    contentWidth: availableWidth
    implicitHeight: body.implicitHeight
    ColumnLayout {
        id: body
        width: control.availableWidth
        height: Math.max(implicitHeight, control.availableHeight)
        spacing: 12
    }
}
