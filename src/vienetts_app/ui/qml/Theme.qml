// Design tokens for the VieNeuTTS UI shell — single source of truth (FR-2.4).
// Dark palette is the default per PROJECT_PLAN.md §8; token *names* are the
// stable contract for all QML files (Main.qml and future tab content).
pragma Singleton
import QtQuick

QtObject {
    // --- Color tokens (dark default) ---
    readonly property color bg: "#12141a"          // window background
    readonly property color surface: "#1a1d25"     // cards / panes
    readonly property color surfaceAlt: "#232733"  // raised surface / inputs
    readonly property color border: "#2c313d"      // hairlines / separators
    readonly property color text: "#e9ecf2"        // primary text
    readonly property color textMuted: "#a0a8b8"   // secondary / hint text
    readonly property color accent: "#8a7dff"      // primary action indigo
    readonly property color accentHover: "#9d92ff"
    readonly property color accentText: "#12141a"  // text on accent fills
    readonly property color success: "#4ade80"
    readonly property color warning: "#fbbf24"
    readonly property color error: "#f87171"

    // --- Spacing scale (px) ---
    readonly property int spacingXs: 4
    readonly property int spacingSm: 8
    readonly property int spacingMd: 12
    readonly property int spacingLg: 16
    readonly property int spacingXl: 24

    // --- Typography ---
    // Typography: empty string defaults to the system UI font (.AppleSystemUIFont on
    // macOS, Segoe UI on Windows, system font on Linux) with zero alias lookup overhead.
    readonly property string fontFamily: ""
    readonly property string fontFamilyMono: ""
    readonly property int fontSizeSm: 12
    readonly property int fontSizeBase: 14
    readonly property int fontSizeLg: 18
    readonly property int fontSizeXl: 24
    readonly property int fontWeightHeading: Font.DemiBold
}
