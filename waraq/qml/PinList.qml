import QtQuick
import QtQuick.Controls

// Plain clipped list. Scroll preservation is handled by the backend models
// (DictListModel updates rows in place instead of resetting the model), so
// the old scroll-pinning workaround is no longer needed here.
ListView {
    clip: true
}
