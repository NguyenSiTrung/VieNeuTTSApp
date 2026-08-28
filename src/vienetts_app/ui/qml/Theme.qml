// Design tokens for the VieNeuTTS UI shell — single source of truth (FR-2.4 & FR-UX-1).
// Dynamically resolves color tokens based on bridge.effectiveTheme ("dark" vs "light").
//
// "Signal" system: slate-neutral surfaces + a signal-teal accent that reads as
// audio/voice rather than the generic AI indigo, with success shifted green-ward
// so status never collides with brand. Typography is Be Vietnam Pro (OFL — see
// ../fonts/OFL.txt), a family drawn for Vietnamese diacritics; every token falls
// back to the system stack when the font files are absent.
pragma Singleton
import QtQuick

QtObject {
    // Dynamic theme resolution based on bridge.effectiveTheme
    readonly property bool isDark: (typeof bridge !== "undefined" && bridge !== null ? bridge.effectiveTheme : "dark") !== "light"

    // --- Bundled type family (weights registered on load; name is NOTIFY) ---
    readonly property FontLoader _fontRegular: FontLoader { source: "fonts/BeVietnamPro-Regular.ttf" }
    readonly property FontLoader _fontMedium: FontLoader { source: "fonts/BeVietnamPro-Medium.ttf" }
    readonly property FontLoader _fontSemiBold: FontLoader { source: "fonts/BeVietnamPro-SemiBold.ttf" }
    readonly property FontLoader _fontBold: FontLoader { source: "fonts/BeVietnamPro-Bold.ttf" }

    // --- Core Surfaces ---
    readonly property color bg: isDark ? "#0f1117" : "#f6f8fa"
    readonly property color surface: isDark ? "#171a23" : "#ffffff"
    readonly property color surfaceAlt: isDark ? "#1f2430" : "#f1f5f9"
    readonly property color surfaceHover: isDark ? "#282e3e" : "#e2e8f0"
    readonly property color surfaceCard: isDark ? "#1a1d27" : "#ffffff"
    readonly property color surfaceCardAlt: isDark ? "#141720" : "#f8fafc"

    // --- Borders & Separators ---
    readonly property color border: isDark ? "#282e3e" : "#dbe3ec"
    readonly property color borderSubtle: isDark ? "#1f232f" : "#e8eef5"
    readonly property color borderFocus: isDark ? "#2dd4bf" : "#0f766e"

    // --- Text & Typography Colors ---
    readonly property color text: isDark ? "#f8fafc" : "#0f172a"
    readonly property color textMuted: isDark ? "#94a3b8" : "#64748b"
    readonly property color textSubtle: isDark ? "#64748b" : "#94a3b8"

    // --- Accent / Brand (Signal Teal) ---
    readonly property color accent: isDark ? "#2dd4bf" : "#0f766e"
    readonly property color accentHover: isDark ? "#5eead4" : "#115e59"
    readonly property color accentSubtle: isDark ? "#0f2e2a" : "#ccfbf1"
    readonly property color accentText: isDark ? "#052e2b" : "#ffffff"

    // --- Interactive Controls ---
    // Disabled controls deliberately retain readable foreground contrast. They
    // signal a missing prerequisite, not an unavailable or broken feature.
    readonly property color controlDisabledBg: isDark ? "#202532" : "#e7edf3"
    readonly property color controlDisabledText: isDark ? "#a7b2c2" : "#5f6e80"
    readonly property color controlDisabledBorder: isDark ? "#30394a" : "#ccd7e3"

    // --- Status / Feedback Colors ---
    readonly property color success: isDark ? "#4ade80" : "#16a34a"
    readonly property color successSubtle: isDark ? "#0e2f1c" : "#dcfce7"
    readonly property color successText: isDark ? "#86efac" : "#14532d"

    readonly property color warning: isDark ? "#fbbf24" : "#d97706"
    readonly property color warningSubtle: isDark ? "#362710" : "#fef3c7"
    readonly property color warningText: isDark ? "#fde68a" : "#92400e"

    readonly property color error: isDark ? "#f87171" : "#dc2626"
    readonly property color errorSubtle: isDark ? "#381919" : "#fee2e2"
    readonly property color errorText: isDark ? "#fca5a5" : "#991b1b"

    // --- Shadows (consumed by AppCard/AppButton elevation) ---
    readonly property color shadowColor: isDark ? "#50000000" : "#16233d24"
    readonly property color shadowSubtle: isDark ? "#28000000" : "#0d162019"

    // --- Spacing scale (px) ---
    readonly property int spacingXxs: 2
    readonly property int spacingXs: 4
    readonly property int spacingSm: 8
    readonly property int spacingMd: 12
    readonly property int spacingLg: 16
    readonly property int spacingXl: 24
    readonly property int spacingXxl: 32

    // --- Corner Radii ---
    readonly property int radiusSm: 6
    readonly property int radiusMd: 10
    readonly property int radiusLg: 14
    readonly property int radiusXl: 18
    readonly property int radiusPill: 9999

    // --- Control sizing ---
    readonly property int controlHeightSm: 32
    readonly property int controlHeightMd: 40
    readonly property int controlHeightLg: 44
    readonly property int controlHitTarget: 40
    readonly property int popupMaxHeight: 320

    // --- Typography ---
    // Empty family string = Qt default system stack (safe fallback).
    readonly property string fontFamily: _fontRegular.name !== "" ? _fontRegular.name : ""
    readonly property string fontFamilyMono: ""
    readonly property int fontSizeXs: 10
    readonly property int fontSizeSm: 12
    readonly property int fontSizeBase: 14
    readonly property int fontSizeMd: 16
    readonly property int fontSizeLg: 18
    readonly property int fontSizeXl: 22
    readonly property int fontSizeXxl: 28
    readonly property int fontWeightRegular: Font.Normal
    readonly property int fontWeightNormal: Font.Normal
    readonly property int fontWeightMedium: Font.Medium
    readonly property int fontWeightHeading: Font.DemiBold
    readonly property int fontWeightBold: Font.Bold

    // --- Micro-typography (px of letter spacing) ---
    readonly property real trackingWide: 1.2   // uppercase micro-labels
    readonly property real trackingTight: -0.2 // large display headings

    // --- Focus & Motion ---
    readonly property int focusRingWidth: 2
    readonly property int durationFast: 110
    readonly property int durationBase: 170
}
