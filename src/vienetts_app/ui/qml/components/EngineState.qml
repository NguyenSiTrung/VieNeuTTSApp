pragma Singleton
import QtQuick

// Capability-derived state of the ACTIVE engine profile (Phase 6 Task 6.2).
//
// Every synthesis surface asks the same three questions before it offers a
// control, and each answer must come from the capability table rather than
// from a per-tab guess:
//
//   * which voices may this profile offer?  -> voiceGroups
//   * does its engine take a language?      -> languageTakesParameter
//   * can it synthesize at all right now?   -> blocked / blockerReason
//
// It reads only the controller's capability seam — engineProfiles (the
// capability table itself: runtime, voicesSource, isActive), profileVoices /
// profileClones (the ACTIVE profile's catalogs), profileLanguages /
// synthesisLanguage, and the model/runtime readiness axes — so a control can
// never offer a combination the controller would refuse at submission time.
//
// The profile readiness word and its sentences live here because two views
// need them: EngineProfilePicker renders them, and the synthesis surfaces use
// them as the disabled reason on their primary actions. One derivation, one
// copy — a control and the picker can never disagree.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py); this
// singleton owns no visual items.
QtObject {
    // The context property is absent in a few harnesses (a bare controller),
    // so every read goes through this guard: `typeof` on an unresolved
    // identifier is safe, a direct read is a ReferenceError.
    readonly property var host: (typeof controller !== "undefined" && controller) ? controller : null

    // ── active profile ────────────────────────────────────────────────────

    /// The capability entry of the active profile (null before the controller
    /// publishes one, or when the host has no capability seam at all).
    readonly property var activeProfile: {
        if (!host)
            return null;
        const profiles = host.engineProfiles;
        for (let i = 0; i < profiles.length; i++)
            if (profiles[i].isActive)
                return profiles[i];
        return null;
    }

    readonly property string profileId: host ? host.engineProfile : ""
    readonly property string profileLabel: activeProfile ? activeProfile.label : ""
    /// How the profile's voices are sourced: "vieneu_catalog" | "pinned" |
    /// "enrollment_only". The picker's shape follows this, not the profile id.
    readonly property string voicesSource: activeProfile
        ? activeProfile.voicesSource : "vieneu_catalog"
    /// True when the profile is served by the managed model host, i.e. it is
    /// unusable until its runtime and model are installed. VieNeu's in-process
    /// worker is always present, so its readiness is published by the
    /// pre-existing model-state flow and is not a gate here.
    readonly property bool needsManagedInstall: activeProfile
        ? activeProfile.runtime === "qwen_host" : false

    // ── voices ────────────────────────────────────────────────────────────

    /// The active profile's compatible voices, in the grouped shape
    /// VoicePicker renders (`{id,label,voices:[{id,label,…}]}`).
    ///
    /// VieNeu keeps its region-grouped SDK catalog (its clones live inside it,
    /// as they always have). A Qwen profile gets the catalog the capability
    /// table declares for it: CustomVoice's pinned speakers, Base's enrolled
    /// clones — never both engines' catalogs mixed.
    readonly property var voiceGroups: {
        if (!host)
            return [];
        if (voicesSource === "vieneu_catalog")
            return host.voices;
        const groups = [];
        const presets = host.profileVoices;
        if (presets.length > 0)
            groups.push({
                "id": "presets",
                "label": qsTr("Người nói cố định"),
                "voices": presetRows(presets)
            });
        const clones = host.profileClones;
        if (clones.length > 0)
            groups.push({
                "id": "cloned",
                "label": qsTr("Giọng đã sao chép"),
                "voices": cloneRows(clones)
            });
        return groups;
    }

    /// True when the active profile offers no selectable voice at all — Base
    /// before its first enrollment. The picker shows `noVoicesReason`.
    readonly property bool hasNoVoices: voiceGroups.length === 0
    readonly property string noVoicesReason: qsTr(
        "Hồ sơ này chỉ tổng hợp bằng giọng đã sao chép — hãy tạo một giọng trong tab Sao chép.")

    /// One preset row per pinned speaker. The native language rides in the
    /// region slot so the picker's existing pill shows it; the speaker name is
    /// the label, exactly as the model card reports it.
    function presetRows(presets) {
        const rows = [];
        for (let i = 0; i < presets.length; i++) {
            const voice = presets[i];
            rows.push({
                "id": voice.id,
                "label": voice.label,
                "region": voice.nativeLanguage || ""
            });
        }
        return rows;
    }

    function cloneRows(clones) {
        const rows = [];
        for (let i = 0; i < clones.length; i++)
            rows.push({"id": clones[i].id, "label": clones[i].label});
        return rows;
    }

    // ── cloning ───────────────────────────────────────────────────────────

    /// True when the active profile's engine can enroll voice clones at all.
    /// A fixed-speaker profile (Qwen CustomVoice) cannot: the Cloning tab
    /// states why instead of offering a flow its engine would refuse.
    readonly property bool supportsCloning: activeProfile
        ? activeProfile.supportsCloning === true : true
    /// What an enrollment must provide, from the capability table:
    /// "reference_clip", "transcript" and/or "consent". Base needs the
    /// reference transcript; VieNeu's SDK does not.
    readonly property var cloneRequirements: activeProfile
        ? activeProfile.cloneRequirements : []
    readonly property bool requiresTranscript: hasCloneRequirement("transcript")

    /// True when reference cleanup (denoise) is available: it is the
    /// VieNeu profile's own operation — a Qwen enrollment stores the reference
    /// as given, so the control is not offered there.
    readonly property bool supportsReferenceCleanup: !needsManagedInstall

    /// The sentence the Cloning tab shows when the active profile cannot
    /// enroll clones ("" = it can).
    readonly property string cloningBlockedReason: supportsCloning ? "" : qsTr(
        "%1 dùng giọng cố định nên không thể sao chép giọng — hãy chuyển sang hồ sơ hỗ trợ sao chép.").arg(profileLabel)

    function hasCloneRequirement(requirement) {
        const requirements = cloneRequirements;
        for (let i = 0; i < requirements.length; i++)
            if (requirements[i] === requirement)
                return true;
        return false;
    }

    // ── language ──────────────────────────────────────────────────────────

    /// True when the active profile's engine consumes a language argument.
    ///
    /// VieNeu's SDK takes none, so a language control there would imply the
    /// choice changes the audio when the engine ignores it (the spec forbids
    /// silently ignored values). Its declared languages stay visible in
    /// Settings, and the control appears once a language is actually in
    /// effect — i.e. once the user has chosen one explicitly.
    readonly property bool languageTakesParameter: {
        if (!host)
            return false;
        if (String(host.synthesisLanguage || "") !== "")
            return true;
        const languages = host.profileLanguages;
        for (let i = 0; i < languages.length; i++)
            if (languages[i].isAuto === true)
                return true;
        return false;
    }

    // ── readiness ─────────────────────────────────────────────────────────

    /// ready | busy | failed | unsupported | missing — derived from BOTH axes:
    /// a profile is usable only when its model AND its runtime are ready.
    readonly property string readiness: {
        if (!host)
            return "checking";
        if (host.profileReady)
            return "ready";
        const model = host.profileModelState;
        const runtime = host.profileRuntimeState;
        if (model === "failed" || runtime === "failed")
            return "failed";
        if (runtime === "unsupported")
            return "unsupported";
        if (model === "downloading" || model === "validating" || runtime === "downloading"
                || runtime === "validating")
            return "busy";
        return "missing";
    }

    /// The "missing" sentence names the axis that still needs an install —
    /// a ready model with an absent runtime must not read as "install both".
    /// (VieNeu's runtime is in-process and always ready, so for it this
    /// correctly reduces to "install the model".)
    readonly property string missingText: {
        if (!host)
            return "";
        const model = host.profileModelState;
        const runtime = host.profileRuntimeState;
        if (model === "ready")
            return qsTr("Mô hình đã sẵn sàng — hãy cài runtime Qwen trong Cài đặt.");
        if (runtime === "ready")
            return qsTr("Cần cài mô hình trong Cài đặt trước khi dùng engine này.");
        return qsTr("Cần cài mô hình và runtime trong Cài đặt trước khi dùng engine này.");
    }

    /// The sentence describing the current readiness (the profile's own failure
    /// reason when it has one, so a corrupt install is not summarized away).
    readonly property string statusText: {
        if (!host)
            return "";
        switch (readiness) {
        case "ready":
            return qsTr("Mô hình và runtime đã sẵn sàng cho engine này.");
        case "busy":
            return qsTr("Đang chuẩn bị mô hình/runtime cho engine này…");
        case "failed":
            // Whichever axis failed carries the reason: a runtime that cannot
            // import its stack has a message the model error cannot express.
            // Both reads are string-guarded, so a host without the runtime
            // error seam falls back to the generic sentence rather than to "".
            if (host.profileRuntimeState === "failed"
                    && String(host.profileRuntimeError || "") !== "")
                return host.profileRuntimeError;
            return host.profileModelError !== ""
                ? host.profileModelError
                : qsTr("Không thể chuẩn bị engine này. Mở Cài đặt để sửa hoặc cài lại.");
        case "unsupported":
            return qsTr("Máy này không có runtime cho engine đã chọn.");
        default:
            return missingText;
        }
    }

    /// True when synthesis cannot start on the active profile — a Qwen profile
    /// whose runtime/model is not installed yet, is being installed, or failed.
    /// VieNeu is never gated here (see `needsManagedInstall`).
    readonly property bool blocked: blockerReason !== ""

    /// The disabled reason the primary synthesis actions carry ("" = go).
    /// Actionable, never the raw error: the profile card shows that.
    readonly property string blockerReason: {
        if (!needsManagedInstall || !host || host.profileReady) {
            // A profile with nothing to pick (Base before its first
            // enrollment) cannot start either: the voice is a required input,
            // exactly like an empty editor.
            return hasNoVoices && voicesSource === "enrollment_only" ? noVoicesReason : "";
        }
        switch (readiness) {
        case "failed":
            return qsTr("Không thể chuẩn bị engine này. Mở Cài đặt để sửa hoặc cài lại.");
        case "unsupported":
            return qsTr("Máy này không có runtime cho engine đã chọn.");
        case "busy":
            return qsTr("Đang chuẩn bị mô hình/runtime cho engine này…");
        default:
            return missingText;
        }
    }

    // ── device readout ────────────────────────────────────────────────────

    /// "checking" is the honest pre-inspection state — never a guess (the model
    /// host re-resolves the device when it loads).
    function deviceName(device) {
        switch (device) {
        case "cpu":
            return "CPU";
        case "cuda":
            return "CUDA";
        case "mps":
            return "MPS";
        case "":
        case "checking":
            return qsTr("đang kiểm tra…");
        default:
            return device;
        }
    }

    readonly property string deviceLabel: host
        ? qsTr("Thiết bị: %1").arg(deviceName(host.engineDevice)) : ""
}