// Design tokens for the VieNeuTTS UI shell — single source of truth (FR-2.4 & FR-UX-1).
// Dynamically resolves color tokens based on bridge.effectiveTheme ("dark" vs "light").
pragma Singleton
import QtQuick

QtObject {
    // Dynamic theme resolution based on bridge.effectiveTheme
    readonly property bool isDark: (typeof bridge !== "undefined" && bridge !== null ? bridge.effectiveTheme : "dark") !== "light"

    // --- Core Surfaces ---
    readonly property color bg: isDark ? "#0f1117" : "#f8fafc"
    readonly property color surface: isDark ? "#171a23" : "#ffffff"
    readonly property color surfaceAlt: isDark ? "#1f2430" : "#f1f5f9"
    readonly property color surfaceHover: isDark ? "#282e3e" : "#e2e8f0"
    readonly property color surfaceCard: isDark ? "#1a1d27" : "#ffffff"
    readonly property color surfaceCardAlt: isDark ? "#141720" : "#f8fafc"

    // --- Borders & Separators ---
    readonly property color border: isDark ? "#282e3e" : "#e2e8f0"
    readonly property color borderSubtle: isDark ? "#1f232f" : "#edf2f7"
    readonly property color borderFocus: isDark ? "#818cf8" : "#6366f1"

    // --- Text & Typography Colors ---
    readonly property color text: isDark ? "#f8fafc" : "#0f172a"
    readonly property color textMuted: isDark ? "#94a3b8" : "#64748b"
    readonly property color textSubtle: isDark ? "#64748b" : "#94a3b8"

    // --- Accent / Brand (Indigo / Violet) ---
    readonly property color accent: isDark ? "#818cf8" : "#6366f1"
    readonly property color accentHover: isDark ? "#9da5fb" : "#4f46e5"
    readonly property color accentSubtle: isDark ? "#232642" : "#e0e7ff"
    readonly property color accentText: "#ffffff"

    // --- Status / Feedback Colors ---
    readonly property color success: isDark ? "#34d399" : "#10b981"
    readonly property color successSubtle: isDark ? "#143026" : "#d1fae5"
    readonly property color successText: isDark ? "#6ee7b7" : "#065f46"

    readonly property color warning: isDark ? "#fbbf24" : "#f59e0b"
    readonly property color warningSubtle: isDark ? "#362710" : "#fef3c7"
    readonly property color warningText: isDark ? "#fde68a" : "#92400e"

    readonly property color error: isDark ? "#f87171" : "#ef4444"
    readonly property color errorSubtle: isDark ? "#381919" : "#fee2e2"
    readonly property color errorText: isDark ? "#fca5a5" : "#991b1b"

    // --- Shadows ---
    readonly property color shadowColor: isDark ? "#40000000" : "#14000000"
    readonly property color shadowSubtle: isDark ? "#20000000" : "#0a000000"

    // --- Spacing scale (px) ---
    readonly property int spacingXxs: 2
    readonly property int spacingXs: 4
    readonly property int spacingSm: 8
    readonly property int spacingMd: 12
    readonly property int spacingLg: 16
    readonly property int spacingXl: 24
    readonly property int spacingXxl: 32

    // --- Corner Radii ---
    readonly property int radiusSm: 4
    readonly property int radiusMd: 8
    readonly property int radiusLg: 12
    readonly property int radiusXl: 16
    readonly property int radiusPill: 9999

    // --- Typography ---
    readonly property string fontFamily: ""
    readonly property string fontFamilyMono: ""
    readonly property int fontSizeXs: 10
    readonly property int fontSizeSm: 12
    readonly property int fontSizeBase: 14
    readonly property int fontSizeMd: 16
    readonly property int fontSizeLg: 18
    readonly property int fontSizeXl: 22
    readonly property int fontSizeXxl: 28
    readonly property int fontWeightNormal: Font.Normal
    readonly property int fontWeightMedium: Font.Medium
    readonly property int fontWeightHeading: Font.DemiBold
    readonly property int fontWeightBold: Font.Bold
}
