import QtQuick

// Presentation only. Settings and execution state remain owned by Main/Bridge.
QtObject {
    property bool dark: true
    property real scale: 1.0
    readonly property string fontFamily: "Segoe UI"
    readonly property string monoFontFamily: "Consolas"
    readonly property int fontWeight: Font.Bold
    readonly property color stageDownloadCompleted: dark ? "#2AA8A6" : "#0B7478"
    readonly property color stageTranscriptCompleted: dark ? "#5C9DCF" : "#1B62A0"
    readonly property color stageWordCompleted: dark ? "#BA8C44" : "#815A1F"
    readonly property color stageCompletedInk: dark ? "#06272B" : "#FFFFFF"
    readonly property color bg: dark ? "#0C242A" : "#F2F7F7"
    readonly property color panel: dark ? "#14363E" : "#FFFFFF"
    readonly property color inset: dark ? "#0F2E35" : "#E5F0F1"
    readonly property color rail: dark ? "#082026" : "#E5F0F1"
    readonly property color ink: dark ? "#E8F3EF" : "#183C43"
    readonly property color muted: dark ? "#A7C3C8" : "#4D6E75"
    readonly property color accent: dark ? "#4BC6C3" : "#0B7478"
    readonly property color accentInk: dark ? "#06272B" : "#FFFFFF"
    readonly property color gold: dark ? "#D8AB64" : "#815A1F"
    readonly property color goldInk: dark ? "#06272B" : "#FFFFFF"
    readonly property color line: dark ? "#39646C" : "#9FB8BD"
    readonly property color hover: dark ? "#1C4650" : "#DCECED"
    readonly property color success: dark ? "#6BD6AD" : "#126B4C"
    readonly property color warning: dark ? "#D8AB64" : "#815A1F"
    readonly property color danger: dark ? "#FF9D9D" : "#B42332"
    readonly property color info: dark ? "#8FCBFF" : "#1B62A0"
    readonly property color railInk: dark ? "#E8F3EF" : "#183C43"
    readonly property color railMuted: dark ? "#A7C3C8" : "#4D6E75"
    readonly property color railActive: dark ? "#4BC6C3" : "#0B7478"
    readonly property color railHover: dark ? "#14363E" : "#DCECED"
    readonly property color railSelectedInk: dark ? "#06272B" : "#FFFFFF"
    function fs(size) { return Math.max(size >= 14 ? 12 : 11, Math.round(size * scale)) }
}
