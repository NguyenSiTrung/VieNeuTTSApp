.pragma library

// Pure helpers behind StatusBar.qml (FR-2.1). No QML context lookups and no
// qsTr here: the inputs arrive as arguments, so tests/unit/test_theme.py can
// evaluate this file in a bare QJSEngine.

/// True when a readout string carries information: not empty and not the
/// pending placeholder the bridge shows until the hardware probe lands
/// (ENGINE_NOTE_PENDING is "…"; "..." is its ASCII spelling).
function isMeaningful(text) {
    if (text === undefined || text === null)
        return false;
    const value = String(text).trim();
    return value !== "" && value !== "…" && value !== "...";
}

/// The engine readout text: the engine note once it is known, else the model
/// state text. The status bar never shows an empty or "…" readout.
function readoutText(note, fallback) {
    if (isMeaningful(note))
        return String(note).trim();
    if (isMeaningful(fallback))
        return String(fallback).trim();
    return "—";
}

/// One readiness key for the status dot and label:
/// ready | busy | failed | unsupported | missing | checking.
/// A managed profile reports EngineState.readiness (both of its axes). VieNeu
/// reports the official model's own install state, which distinguishes
/// "still checking" from "missing".
function readinessKey(modelState, managedProfile, profileReadiness) {
    if (managedProfile) {
        switch (profileReadiness) {
        case "ready":
        case "busy":
        case "failed":
        case "unsupported":
        case "missing":
            return profileReadiness;
        default:
            return "checking";
        }
    }
    switch (modelState) {
    case "ready":
        return "ready";
    case "downloading":
    case "validating":
        return "busy";
    case "failed":
        return "failed";
    case "unavailable":
        return "missing";
    default:
        return "checking";
    }
}
