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
    /// The positive form of the same fact, for a surface that must combine it
    /// with its own state. An instance-level `enabled` binding REPLACES the
    /// picker's own (SubtitleCard adds available/busy/exporting), so such a
    /// surface re-reads the capability from here instead of re-deriving it.
    readonly property bool hasVoices: !hasNoVoices
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

    // ── app-wide settings this engine owns ────────────────────────────────

    /// True when the ACTIVE profile is the one the app-wide "default voice"
    /// setting belongs to. Only the VieNeu catalog feeds it: a Qwen profile's
    /// voice is chosen per submission (a pinned speaker or an enrolled clone),
    /// so a Qwen voice picked there would be persisted as a value VieNeu can
    /// never serve. A host without the capability seam keeps the VieNeu-shaped
    /// default, like every other flag here.
    readonly property bool defaultVoiceApplies: voicesSource === "vieneu_catalog"
    /// What replaces the control where it does not apply ("" = it does).
    readonly property string defaultVoiceNote: defaultVoiceApplies ? "" : qsTr(
        "Giọng mặc định là thiết lập của VieNeu-TTS — với %1, giọng đọc được chọn trên từng tab tổng hợp.").arg(profileLabel)

    /// True when the active profile's engine samples with the Settings
    /// temperature value. The pinned 0.6B Qwen host does not: it samples with
    /// its own fixed settings (`core/qwen_engine.py` documents the ignored
    /// parameter), so the field must not read as a control there. Derived from
    /// the capability table's `generationControls`, never from the profile id.
    readonly property bool supportsTemperature: generationControl("temperature")
    /// What replaces the field's explanation where it does not apply ("" = it does).
    readonly property string temperatureNote: supportsTemperature ? "" : qsTr(
        "%1 tự lấy mẫu với thiết lập riêng — Temperature chỉ áp dụng cho VieNeu-TTS.").arg(profileLabel)

    /// Whether the active profile declares one generation control. Absent
    /// capability data (a bare host) answers `true`: the VieNeu-shaped default.
    /// `generationControls` is a QVariantList, so it is read by index/length —
    /// `Array.isArray` is false for a wrapped list and would misread a profile
    /// that declares nothing as one that declares everything.
    function generationControl(name) {
        const profile = activeProfile;
        if (!profile)
            return true;
        const controls = profile.generationControls;
        if (!controls)
            return true;
        for (let i = 0; i < controls.length; i++)
            if (controls[i] === name)
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

    // ── expressiveness ────────────────────────────────────────────────────

    /// True when the active profile's engine interprets the inline emotion
    /// tags the Text tab inserts. VieNeu's SDK owns that vocabulary
    /// (`[cười] [thở dài] [hắng giọng]`); a Qwen profile reads the brackets as
    /// literal characters, so the chips must not be offered there. A host
    /// without the capability seam keeps the VieNeu-shaped default, like every
    /// other flag here.
    readonly property bool supportsEmotionTags: activeProfile
        ? activeProfile.supportsEmotionTags === true : true

    /// What replaces the tag chips when the active profile has no tag
    /// vocabulary: where that engine's expression actually comes from, so the
    /// absent control reads as a capability boundary rather than an oversight
    /// ("" = the chips are offered).
    readonly property string expressivenessNote: {
        if (supportsEmotionTags)
            return "";
        if (voicesSource === "enrollment_only")
            return qsTr("%1 nhận biểu cảm từ đoạn âm thanh mẫu — hãy chọn giọng đã sao chép phù hợp.").arg(profileLabel);
        return qsTr("%1 không nhận thẻ biểu cảm — biểu cảm do người nói và nội dung câu quyết định.").arg(profileLabel);
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
    /// host re-resolves the device when it loads). `metal` is the GGUF
    /// runtime's Apple vocabulary (the PyTorch build calls the same hardware
    /// `mps`) — the readout shows the word the selected engine actually uses.
    function deviceName(device) {
        switch (device) {
        case "cpu":
            return "CPU";
        case "cuda":
            return "CUDA";
        case "mps":
            return "MPS";
        case "metal":
            return "Metal";
        case "":
        case "checking":
            return qsTr("đang kiểm tra…");
        default:
            return device;
        }
    }

    readonly property string deviceLabel: host
        ? qsTr("Thiết bị: %1").arg(deviceName(host.engineDevice)) : ""

    /// The armed weight variant for a managed profile, e.g. "GGUF Q4_K_M ·
    /// qwentts.cpp" — "" for VieNeu (its engine is in-process and has no
    /// variant axis). One derivation so every surface embedding the picker
    /// reports the same engine the next job will actually run on.
    readonly property string variantLabel: {
        if (!host || host.engineProfileIsQwen !== true)
            return "";
        const engine = String(host.qwenEngineLabel || "");
        if (String(host.qwenModelFormat || "") === "gguf")
            //: %1 = GGUF quantization (Q8_0/Q4_K_M), %2 = engine (qwentts.cpp).
            return qsTr("GGUF %1 · %2").arg(host.qwenGgufQuantization).arg(engine);
        //: %1 = engine name (PyTorch) — the official full-weight variant.
        return qsTr("Trọng lượng đầy đủ (%1)").arg(engine);
    }
}