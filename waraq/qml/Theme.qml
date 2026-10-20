import QtQuick

// Presentation only. Settings and execution state remain owned by Main/Bridge.
QtObject {
    property bool dark: true
    property real scale: 1.0
    readonly property color bg: dark ? "#0C242A" : "#F2F7F7"
    readonly property color panel: dark ? "#14363E" : "#FFFFFF"
    readonly property color inset: dark ? "#0F2E35" : "#E5F0F1"
    readonly property color rail: dark ? "#082026" : "#0E3239"
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
    readonly property color railInk: "#E8F3EF"
    readonly property color railMuted: "#A7C3C8"
    readonly property color railActive: "#4BC6C3"
    readonly property color railHover: "#14363E"
    readonly property color railSelectedInk: "#06272B"
    function fs(size) { return Math.max(size >= 14 ? 12 : 11, Math.round(size * scale)) }
}
