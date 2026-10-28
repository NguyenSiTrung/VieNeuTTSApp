import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import ".."
import "SettingsFilter.js" as Filter

// Cài đặt › Mô hình (FR-4.2): where the active engine family's weights come
// from. VieNeu: the official baseline or a custom Hugging Face repo
// (settingsModelSourceCard). A Qwen profile: the shared variant picker
// (format → quantization) over its managed runtime + checkpoint cards
// (components/QwenInstallCards.qml). Visibility binds to
// page.qwenProfileActive — switch profile first (Engine & phần cứng), then
// install here.
//
// objectNames (tested contract): settingsSection_models,
// settingsModelSourceCard, officialRepoChip, customRepoChip, modelRepoField,
// modelRepoResetButton, openHfButton, modelSourceDetail,
// settingsModelDirLabel, settingsModelDirCopyButton,
// settingsModelDirOpenButton, settingsQwenModelsCard (hosts
// qwenVariantPicker), and every name QwenInstallCards documents.
// AI dubbing card (FR-7.1): settingsFfmpegRuntimeRow (settingsFfmpegStatusLabel,
// settingsFfmpegInstallButton, settingsFfmpegRemoveButton,
// settingsFfmpegRefreshButton, settingsFfmpegGuidanceLabel,
// settingsFfmpegMessageLabel, settingsFfmpegProgressBar,
// settingsFfmpegLicenseLink), asrBackendCombo (asrBackendDescription),
// asrCloudFallbackToggle, asrApiBaseUrlDescription, and the optional audio
// model rows settingsWhisperVadRow / settingsSileroVadRow /
// settingsSpeakerEmbeddingRow (each with <row>Download, <row>Remove,
// <row>Description) over settingsAudioModelStatusLabel and
// settingsAudioModelProgressBar.
SettingsSection {
    id: section

    readonly property bool qwenProfileActive: section.page ? section.page.qwenProfileActive : false
    // Verified repo override vs. the official baseline. `customRepoRequested`
    // is the chip the user pressed; the repo field appears in custom mode
    // only, so the official id is not repeated by chip + field + feedback
    // line at once.
    property bool customRepoRequested: false
    readonly property bool customRepoMode: customRepoRequested
        || (controller ? controller.modelRepo !== "" : false)

    sectionId: "models"
    title: qsTr("Mô hình")
    hasMatch: anyMatch([settingsModelSourceCard, qwenModelsCard, qwenInstall, settingsAiDubbingCard])

    // One optional audio model (FR-7.1): what it adds, its size, licence and
    // install state, and download/remove buttons keyed by the catalog id
    // (controller.audioModels). Buttons are named <row objectName>Download /
    // <row objectName>Remove; one audio-model operation runs at a time.
    component AudioModelRow: SettingRow {
        id: modelRow

        property string modelId: ""
        property string purpose: ""
        readonly property var info: {
            const rows = (controller && controller.audioModels) ? controller.audioModels : [];
            for (let i = 0; i < rows.length; ++i) {
                if (rows[i].id === modelRow.modelId)
                    return rows[i];
            }
            return null;
        }
        readonly property bool installed: Boolean(modelRow.info && modelRow.info.installed)
        readonly property bool anyBusy: Boolean(controller && controller.audioModelBusyId)
        readonly property string busyReason: modelRow.anyBusy
            ? qsTr("Đang tải hoặc gỡ một mô hình âm thanh — chờ xong rồi thử lại.") : ""

        shown: modelRow.info !== null
        descriptionObjectName: modelRow.objectName + "Description"
        description: modelRow.info
            ? qsTr("%1 (%2 MB · %3 · %4)")
                .arg(modelRow.purpose)
                .arg((Number(modelRow.info.sizeBytes) / 1000000).toFixed(1))
                .arg(modelRow.info.license)
                .arg(modelRow.installed ? qsTr("đã cài") : qsTr("chưa cài"))
            : modelRow.purpose

        AppButton {
            objectName: modelRow.objectName + "Download"
            variant: "secondary"
            size: "sm"
            iconKind: "download"
            text: Boolean(modelRow.info && modelRow.info.busy)
                ? qsTr("Đang xử lý…")
                : (modelRow.installed ? qsTr("Tải lại") : qsTr("Tải về"))
            accessibleLabel: text + " — " + modelRow.label
            enabled: Boolean(controller && modelRow.info && !modelRow.anyBusy)
            disabledReason: modelRow.busyReason
            onClicked: {
                if (controller && typeof controller.downloadAudioModel === "function")
                    controller.downloadAudioModel(modelRow.modelId);
            }
        }

        AppButton {
            objectName: modelRow.objectName + "Remove"
            variant: "secondary"
            size: "sm"
            visible: modelRow.installed
            text: qsTr("Gỡ")
            accessibleLabel: text + " — " + modelRow.label
            enabled: Boolean(controller && !modelRow.anyBusy)
            disabledReason: modelRow.busyReason
            onClicked: {
                if (controller && typeof controller.removeAudioModel === "function")
                    controller.removeAudioModel(modelRow.modelId);
            }
        }
    }

    AppCard {
        id: settingsModelSourceCard

        // One filter row: the card's title + the official repo id.
        property bool shown: !section.qwenProfileActive
        readonly property bool matches: Filter.matches(section.filter, [
            title, "Hugging Face repo VieNeu-TTS model"
        ])

        objectName: "settingsModelSourceCard"
        visible: shown && matches
        Layout.fillWidth: true
        title: qsTr("Nguồn mô hình (Hugging Face)")
        subtitle: qsTr("Dùng mô hình gốc chính thức hoặc trỏ tới repository Hugging Face tùy chỉnh")

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            // Preset Quick Selector Chips
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                AppButton {
                    id: officialRepoChip
                    objectName: "officialRepoChip"
                    variant: "chip"
                    checked: !section.customRepoMode
                    size: "sm"
                    iconKind: "check"
                    text: qsTr("pnnbao-ump/VieNeu-TTS-v3-Turbo (mặc định)")
                    accessibleLabel: qsTr("Chọn mô hình chính thức mặc định")
                    onClicked: {
                        section.customRepoRequested = false;
                        controller.modelRepo = "";
                        modelRepoField.text = "";
                    }
                }

                AppButton {
                    id: customRepoChip
                    objectName: "customRepoChip"
                    variant: "chip"
                    checked: section.customRepoMode
                    size: "sm"
                    iconKind: "settings"
                    text: qsTr("Repo tùy chỉnh")
                    accessibleLabel: qsTr("Nhập repository tùy chỉnh")
                    onClicked: {
                        section.customRepoRequested = true;
                        modelRepoField.forceActiveFocus();
                        modelRepoField.selectAll();
                    }
                }
            }

            // Custom repository input — only while a custom repo is in
            // effect or being entered.
            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs
                visible: section.customRepoMode

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: inputRow.implicitHeight + Theme.spacingSm * 2
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: modelRepoField.activeFocus ? Theme.borderFocus : Theme.borderSubtle
                    border.width: 1

                    Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

                    RowLayout {
                        id: inputRow
                        anchors.fill: parent
                        anchors.margins: Theme.spacingSm
                        spacing: Theme.spacingSm

                        // Prefix badge: "hf.co/"
                        Rectangle {
                            implicitHeight: 32
                            implicitWidth: prefixLabel.implicitWidth + Theme.spacingMd
                            radius: Theme.radiusSm
                            color: Theme.surface
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignVCenter

                            Label {
                                id: prefixLabel
                                anchors.centerIn: parent
                                text: "hf.co/"
                                color: Theme.textMuted
                                font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                                font.weight: Theme.fontWeightMedium
                            }
                        }

                        TextField {
                            id: modelRepoField
                            objectName: "modelRepoField"
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            placeholderText: "pnnbao-ump/VieNeu-TTS-v3-Turbo"
                            placeholderTextColor: Theme.textSubtle
                            color: Theme.text
                            selectedTextColor: Theme.accentText
                            selectionColor: Theme.accent
                            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            implicitHeight: 36
                            leftPadding: Theme.spacingSm
                            rightPadding: Theme.spacingSm
                            selectByMouse: true
                            Accessible.name: qsTr("Nguồn mô hình")

                            background: Rectangle {
                                color: "transparent"
                            }

                            text: controller.modelRepo
                            // Commit on focus-loss/Enter only — never per keystroke
                            onEditingFinished: controller.modelRepo = text
                        }

                        // Quick Reset Button
                        AppIconButton {
                            id: modelRepoResetButton
                            objectName: "modelRepoResetButton"
                            size: "sm"
                            iconKind: "reset"
                            tooltipText: qsTr("Khôi phục repo chính thức mặc định")
                            accessibleLabel: qsTr("Khôi phục repo chính thức mặc định")
                            visible: (controller && controller.modelRepo !== "") || (modelRepoField.text.trim() !== "")
                            Layout.alignment: Qt.AlignVCenter
                            onClicked: {
                                section.customRepoRequested = false;
                                controller.modelRepo = "";
                                modelRepoField.text = "";
                            }
                        }

                        // Open on Hugging Face Button
                        AppButton {
                            id: openHfButton
                            objectName: "openHfButton"
                            variant: "secondary"
                            size: "sm"
                            iconKind: "externalLink"
                            text: qsTr("Hugging Face")
                            tooltipText: qsTr("Mở trang mô hình trên Hugging Face")
                            accessibleLabel: qsTr("Mở trang mô hình trên Hugging Face")
                            Layout.alignment: Qt.AlignVCenter
                            onClicked: {
                                const repo = (controller && controller.modelRepo !== "") ? controller.modelRepo : "pnnbao-ump/VieNeu-TTS-v3-Turbo";
                                Qt.openUrlExternally("https://huggingface.co/" + repo);
                            }
                        }
                    }
                }

                // Contextual format feedback note
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingXs

                    readonly property string currentText: modelRepoField.text.trim()
                    readonly property bool isValidRepo: /^[^\s/]+\/[^\s/]+$/.test(currentText)

                    AppIcon {
                        width: 14
                        height: 14
                        kind: parent.isValidRepo ? "check" : "close"
                        iconColor: parent.isValidRepo ? Theme.accent : Theme.error
                        Layout.alignment: Qt.AlignVCenter
                    }

                    Label {
                        Layout.fillWidth: true
                        text: parent.currentText === ""
                            ? qsTr("Để trống để dùng mô hình chính thức, hoặc nhập dạng 'tác_giả/tên_repo'")
                            : (parent.isValidRepo
                                ? qsTr("Repository hợp lệ: huggingface.co/%1 (sẽ tự động tải khi khởi động engine)").arg(parent.currentText)
                                : qsTr("Định dạng chưa đúng: cần có dạng 'tác_giả/tên_repo' (ví dụ: username/custom-model, không có khoảng trắng)"))
                        color: parent.currentText === "" || parent.isValidRepo ? Theme.textMuted : Theme.errorText
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        wrapMode: Text.Wrap
                        lineHeight: 1.2
                    }
                }
            }

            // Capability of the official baseline. The removed feedback
            // row restated the chip and the folder path, but these two
            // facts (48 kHz, both shipped languages) exist nowhere else on
            // this card.
            Label {
                Layout.fillWidth: true
                visible: !section.customRepoMode
                text: qsTr("Mô hình gốc 48 kHz, hỗ trợ tiếng Việt và tiếng Anh.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.Wrap
            }

            // Model state (managed install health) — independent of which
            // source is selected.
            Label {
                objectName: "modelSourceDetail"
                Layout.fillWidth: true
                text: {
                    if (!controller)
                        return "";
                    if (controller.modelRepo !== "")
                        return qsTr("Nguồn tùy chỉnh nâng cao — bản tải chính thức không áp dụng.");
                    switch (controller.modelState) {
                    case "ready":
                        return qsTr("Baseline chính thức đã xác thực, sẵn sàng ngoại tuyến.");
                    case "downloading":
                        return qsTr("Đang tải baseline chính thức...");
                    case "validating":
                        return qsTr("Đang xác thực baseline chính thức...");
                    case "failed":
                        return qsTr("Baseline chính thức lỗi — xem màn hình thiết lập.");
                    default:
                        return qsTr("Baseline chính thức được quản lý tại thư mục dữ liệu.");
                    }
                }
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.Wrap
                lineHeight: 1.2
            }

            // Local location of the official weights. Hidden in custom
            // mode: `modelDir` is the managed baseline dir, and showing it
            // next to "custom source — the official download does not
            // apply" reads as a contradiction.
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs
                visible: !section.customRepoMode

                Label {
                    text: qsTr("Thư mục:")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }
                Label {
                    id: settingsModelDirLabel
                    objectName: "settingsModelDirLabel"
                    Layout.fillWidth: true
                    text: controller ? controller.modelDir : ""
                    elide: Text.ElideMiddle
                    color: Theme.text
                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }
                AppIconButton {
                    id: settingsModelDirCopyButton
                    objectName: "settingsModelDirCopyButton"
                    size: "sm"
                    iconKind: "copy"
                    tooltipText: qsTr("Sao chép đường dẫn thư mục mô hình")
                    accessibleLabel: qsTr("Sao chép đường dẫn thư mục mô hình")
                    onClicked: controller.copyModelDir()
                }
                AppIconButton {
                    id: settingsModelDirOpenButton
                    objectName: "settingsModelDirOpenButton"
                    size: "sm"
                    iconKind: "folder"
                    tooltipText: qsTr("Mở thư mục mô hình")
                    accessibleLabel: qsTr("Mở thư mục mô hình")
                    onClicked: controller.openModelDir()
                }
            }
        }
    }

    // ── Qwen: variant choice + install cards ─────────────────────────────
    AppCard {
        id: qwenModelsCard

        property bool shown: section.qwenProfileActive
        readonly property bool matches: Filter.matches(section.filter, [
            title, "Qwen GGUF quantization Q8_0 Q4_K_M"
        ])

        objectName: "settingsQwenModelsCard"
        visible: shown && matches
        Layout.fillWidth: true
        title: qsTr("Biến thể mô hình Qwen")

        QwenVariantPicker {
            Layout.fillWidth: true
        }
    }

    // The managed runtime card and the per-variant model matrix for the
    // SELECTED format, with copy that never describes a PyTorch bundle under
    // GGUF.
    QwenInstallCards {
        id: qwenInstall

        property bool shown: section.qwenProfileActive
        readonly property bool matches: Filter.matches(section.filter, [
            qsTr("Runtime Qwen"), qsTr("Mô hình Qwen"), "Qwen runtime install download"
        ])

        visible: shown && matches
        Layout.fillWidth: true
        isCompact: section.compact
    }

    // ── AI Dubbing: Whisper ASR, Speaker Diarization, and LLM Provider ───
    AppCard {
        id: settingsAiDubbingCard

        readonly property bool matches: Filter.matches(section.filter, [
            title, "Whisper ASR diarization Nemotron audio.cpp Vulkan CUDA LLM Ollama OpenAI subtitle video dubbing FFmpeg ffprobe VAD Silero CAM++ speaker embedding cloud fallback"
        ])

        objectName: "settingsAiDubbingCard"
        visible: matches
        Layout.fillWidth: true
        title: qsTr("AI Phụ đề & Lồng tiếng Video (ASR / Diarization / LLM)")
        subtitle: qsTr("Cấu hình nhận diện giọng nói (Whisper), phân tách người nói (Nemotron-3 GGUF / Âm học) và LLM tinh chỉnh phụ đề")

        readonly property string asrRuntimeLabel: (controller && controller.whisperCppInstalled)
            ? qsTr("whisper.cpp: %1").arg(controller.whisperCppRuntimeVariant || "cpu")
            : qsTr("whisper.cpp: chưa cài")

        readonly property string diarRuntimeLabel: (controller && controller.audiocppInstalled)
            ? qsTr("audio.cpp: %1").arg(controller.audiocppRuntimeVariant || "cpu")
            : qsTr("audio.cpp: chưa cài")

        // FR-7.1: only the backends this build can run (controller.asrBackends —
        // faster-whisper is listed only when it is installed).
        readonly property var asrBackendOptions: {
            const labels = {
                "auto": qsTr("Tự động (trên máy; API chỉ khi cho phép)"),
                "faster_whisper": qsTr("On-device faster-whisper"),
                "whisper_cpp": qsTr("On-device whisper.cpp"),
                "openai_api": qsTr("Máy chủ Whisper API (OpenAI)")
            };
            const ids = (controller && controller.asrBackends && controller.asrBackends.length > 0)
                ? controller.asrBackends : ["auto", "whisper_cpp", "openai_api"];
            const options = [];
            for (let i = 0; i < ids.length; ++i)
                options.push({ value: ids[i], label: labels[ids[i]] || ids[i] });
            return options;
        }

        readonly property var asrModelOptions: [
            { value: "tiny", label: qsTr("Whisper Tiny (75 MB — Siêu nhanh)") },
            { value: "base", label: qsTr("Whisper Base (142 MB — Cân bằng)") },
            { value: "small", label: qsTr("Whisper Small (466 MB — Chính xác cao)") },
            { value: "medium", label: qsTr("Whisper Medium Q5_0 (514 MB)") },
            // The Settings default (Settings.asr_model) and the recommended pick.
            { value: "large-v3-turbo", label: qsTr("Whisper Large-v3-Turbo Q5_0 (547 MB — Khuyên dùng · Mặc định)") }
        ]

        readonly property var asrLanguageOptions: [
            { value: "auto", label: qsTr("Tự động nhận diện ngôn ngữ") },
            { value: "vi", label: qsTr("Tiếng Việt (vi)") },
            { value: "en", label: qsTr("Tiếng Anh (en)") },
            { value: "zh", label: qsTr("Tiếng Trung (zh)") },
            { value: "ja", label: qsTr("Tiếng Nhật (ja)") },
            { value: "ko", label: qsTr("Tiếng Hàn (ko)") },
            { value: "fr", label: qsTr("Tiếng Pháp (fr)") },
            { value: "de", label: qsTr("Tiếng Đức (de)") },
            { value: "es", label: qsTr("Tiếng Tây Ban Nha (es)") },
            { value: "ru", label: qsTr("Tiếng Nga (ru)") }
        ]

        readonly property var asrDeviceOptions: {
            const all = [
                { value: "auto", label: qsTr("Tự động (ưu tiên GPU nếu khả dụng)") },
                { value: "cpu", label: qsTr("CPU (mọi máy, ổn định nhất)") },
                { value: "cuda", label: qsTr("CUDA GPU (NVIDIA)") },
                { value: "metal", label: qsTr("Metal GPU (macOS Apple Silicon)") }
            ];
            if (!controller || typeof controller.whisperCppAvailableVariants === "undefined")
                return all;
            const available = controller.whisperCppAvailableVariants || [];
            return all.filter(function (option) {
                return option.value === "auto" || option.value === "cpu"
                    || available.indexOf(option.value) >= 0;
            });
        }
        readonly property var diarBackendOptions: [
            { value: "auto", label: qsTr("Tự động (Nemotron-3 GGUF / Âm học)") },
            { value: "nemotron_gguf", label: qsTr("Nemotron-3 GGUF (audio.cpp — CPU/GPU)") },
            { value: "acoustic", label: qsTr("Phân cụm âm học Offline (dự phòng, kém chính xác)") }
        ]

        readonly property var diarDeviceOptions: {
            const all = [
                { value: "auto", label: qsTr("Tự động (runtime chọn backend tốt nhất)") },
                { value: "cpu", label: qsTr("CPU (mọi máy, ổn định nhất)") },
                { value: "cuda", label: qsTr("CUDA GPU (NVIDIA)") },
                { value: "vulkan", label: qsTr("Vulkan GPU (nhanh hơn, cần runtime Vulkan)") },
                { value: "metal", label: qsTr("Metal GPU (macOS Apple Silicon)") }
            ];
            if (!controller || typeof controller.audiocppAvailableVariants === "undefined")
                return all;
            // audio.cpp lists its CUDA builds by toolkit (cuda12.4, cuda13.3);
            // any of them offers the "cuda" device (the installer picks one).
            const available = controller.audiocppAvailableVariants || [];
            let hasCuda = false;
            for (let i = 0; i < available.length; ++i)
                hasCuda = hasCuda || String(available[i]).indexOf("cuda") === 0;
            return all.filter(function (option) {
                if (option.value === "cuda")
                    return hasCuda;
                return option.value === "auto" || option.value === "cpu"
                    || available.indexOf(option.value) >= 0;
            });
        }

        readonly property var diarModelOptions: [
            {
                value: "nemotron-3-diarization-q8_0",
                label: qsTr("Nemotron-3 Diarization Q8_0 (102 MB — Nhanh, nhẹ)")
            },
            {
                value: "nemotron-3-diarization-bf16",
                label: qsTr("Nemotron-3 Diarization BF16 (190 MB — Chuẩn NeMo)")
            }
        ]

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            // FR-7.1/FR-2.3: ffmpeg + ffprobe for ASR audio and video export —
            // the app-managed pack, else a system copy. An unpublished pack
            // shows the host's Homebrew/apt/winget guidance instead.
            ColumnLayout {
                id: ffmpegBlock

                readonly property var status: (controller && controller.ffmpegRuntimeStatus)
                    ? controller.ffmpegRuntimeStatus : ({})
                readonly property bool busy: Boolean(controller && controller.ffmpegRuntimeBusy)
                readonly property bool missing: ffmpegBlock.status.available === false
                readonly property string guidance: (ffmpegBlock.missing && !ffmpegBlock.status.downloadable)
                    ? (ffmpegBlock.status.note || "") : ""
                readonly property string message: (controller && controller.ffmpegRuntimeMessage)
                    ? controller.ffmpegRuntimeMessage : ""

                objectName: "settingsFfmpegRuntimeRow"
                Layout.fillWidth: true
                spacing: Theme.spacingXs
                visible: typeof ffmpegBlock.status.available === "boolean"

                SettingRow {
                    compact: section.compact
                    label: qsTr("FFmpeg (âm thanh và video)")
                    descriptionObjectName: "settingsFfmpegStatusLabel"
                    descriptionColor: ffmpegBlock.missing ? Theme.warningText : Theme.textMuted
                    description: {
                        const s = ffmpegBlock.status;
                        const name = s.version ? "ffmpeg " + s.version : "ffmpeg";
                        if (ffmpegBlock.missing)
                            return qsTr("Chưa tìm thấy ffmpeg/ffprobe — cần để nhận dạng phụ đề và xuất video.");
                        if (s.managed)
                            return qsTr("Đang dùng %1 do ứng dụng cài: %2").arg(name).arg(s.ffmpeg || "");
                        return qsTr("Đang dùng %1 của hệ thống: %2").arg(name).arg(s.ffmpeg || "");
                    }

                    AppButton {
                        objectName: "settingsFfmpegInstallButton"
                        variant: ffmpegBlock.missing ? "primary" : "secondary"
                        size: "sm"
                        iconKind: "download"
                        text: ffmpegBlock.busy
                            ? qsTr("Đang xử lý…")
                            : (ffmpegBlock.status.managed ? qsTr("Cài lại ffmpeg") : qsTr("Cài ffmpeg"))
                        enabled: Boolean(controller && ffmpegBlock.status.downloadable && !ffmpegBlock.busy)
                        disabledReason: ffmpegBlock.busy
                            ? qsTr("Đang cài hoặc gỡ ffmpeg.")
                            : (ffmpegBlock.status.downloadable ? ""
                                : (ffmpegBlock.status.note
                                    || qsTr("Chưa có gói ffmpeg dựng sẵn cho hệ điều hành này.")))
                        onClicked: {
                            if (controller && typeof controller.installFfmpegRuntime === "function")
                                controller.installFfmpegRuntime();
                        }
                    }

                    AppButton {
                        objectName: "settingsFfmpegRemoveButton"
                        variant: "secondary"
                        size: "sm"
                        visible: ffmpegBlock.status.managed === true
                        text: qsTr("Gỡ ffmpeg")
                        accessibleLabel: qsTr("Gỡ ffmpeg do ứng dụng cài")
                        enabled: Boolean(controller && !ffmpegBlock.busy)
                        disabledReason: qsTr("Đang cài hoặc gỡ ffmpeg.")
                        onClicked: {
                            if (controller && typeof controller.removeFfmpegRuntime === "function")
                                controller.removeFfmpegRuntime();
                        }
                    }

                    AppButton {
                        objectName: "settingsFfmpegRefreshButton"
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Kiểm tra lại")
                        tooltipText: qsTr("Tìm lại ffmpeg sau khi cài bằng Homebrew, apt hoặc winget")
                        accessibleLabel: qsTr("Kiểm tra lại ffmpeg")
                        enabled: Boolean(controller && !ffmpegBlock.busy)
                        disabledReason: qsTr("Đang cài hoặc gỡ ffmpeg.")
                        onClicked: {
                            if (controller && typeof controller.refreshFfmpegRuntimeStatus === "function")
                                controller.refreshFfmpegRuntimeStatus();
                        }
                    }
                }

                ProgressBar {
                    objectName: "settingsFfmpegProgressBar"
                    Layout.fillWidth: true
                    from: 0
                    to: 1
                    value: (controller && typeof controller.ffmpegRuntimeProgress === "number")
                        ? Math.max(0, controller.ffmpegRuntimeProgress) : 0
                    visible: ffmpegBlock.busy
                    Accessible.name: qsTr("Tiến trình cài ffmpeg")
                }

                Label {
                    objectName: "settingsFfmpegGuidanceLabel"
                    Layout.fillWidth: true
                    visible: ffmpegBlock.guidance !== ""
                    text: ffmpegBlock.guidance
                    textFormat: Text.PlainText
                    color: Theme.warningText
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                Label {
                    objectName: "settingsFfmpegMessageLabel"
                    Layout.fillWidth: true
                    visible: ffmpegBlock.message !== "" && ffmpegBlock.message !== ffmpegBlock.guidance
                    text: ffmpegBlock.message
                    textFormat: Text.PlainText
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                AppButton {
                    objectName: "settingsFfmpegLicenseLink"
                    variant: "quiet"
                    size: "sm"
                    iconKind: "externalLink"
                    visible: Boolean(ffmpegBlock.status.license)
                    text: qsTr("Giấy phép %1").arg(ffmpegBlock.status.license || "")
                    tooltipText: ffmpegBlock.status.source || ""
                    accessibleLabel: qsTr("Mở nguồn và giấy phép ffmpeg (%1)").arg(ffmpegBlock.status.license || "")
                    enabled: Boolean(ffmpegBlock.status.source)
                    disabledReason: qsTr("Chưa có nguồn ffmpeg cho hệ điều hành này.")
                    onClicked: Qt.openUrlExternally(ffmpegBlock.status.source)
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Bộ nhận diện giọng nói (ASR)")
                // A stored backend this build cannot run says what runs instead.
                description: (controller && controller.asrBackendNote)
                    ? controller.asrBackendNote
                    : qsTr("Tạo phụ đề tự động khi video chưa có tệp .srt")
                descriptionObjectName: "asrBackendDescription"
                descriptionColor: (controller && controller.asrBackendNote)
                    ? Theme.warningText : Theme.textMuted
                control: AppCombo {
                    objectName: "asrBackendCombo"
                    comboWidth: 320
                    model: settingsAiDubbingCard.asrBackendOptions
                    textRole: "label"
                    currentIndex: section.page
                        ? section.page.valueIndex(
                            settingsAiDubbingCard.asrBackendOptions,
                            (controller && typeof controller.asrBackend === "string") ? controller.asrBackend : "auto")
                        : 0
                    onActivated: function (index) {
                        if (controller && typeof controller.asrBackend !== "undefined")
                            controller.asrBackend = settingsAiDubbingCard.asrBackendOptions[index].value;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Ngôn ngữ nhận diện (ASR)")
                description: qsTr("Cố định ngôn ngữ nguồn giúp nhận diện nhanh và chính xác hơn tự động dò")
                control: AppCombo {
                    objectName: "asrLanguageCombo"
                    comboWidth: 320
                    model: settingsAiDubbingCard.asrLanguageOptions
                    textRole: "label"
                    currentIndex: section.page
                        ? section.page.valueIndex(
                            settingsAiDubbingCard.asrLanguageOptions,
                            (controller && typeof controller.asrLanguage === "string") ? controller.asrLanguage : "auto")
                        : 0
                    onActivated: function (index) {
                        if (controller && typeof controller.asrLanguage !== "undefined")
                            controller.asrLanguage = settingsAiDubbingCard.asrLanguageOptions[index].value;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Thiết bị chạy ASR")
                description: qsTr("Tự động ưu tiên GPU nếu runtime hỗ trợ, ngược lại chạy CPU.")
                control: AppCombo {
                    objectName: "asrDeviceCombo"
                    comboWidth: 320
                    model: settingsAiDubbingCard.asrDeviceOptions
                    textRole: "label"
                    currentIndex: section.page
                        ? section.page.valueIndex(
                            settingsAiDubbingCard.asrDeviceOptions,
                            (controller && typeof controller.asrDevice === "string") ? controller.asrDevice : "auto")
                        : 0
                    onActivated: function (index) {
                        if (controller && typeof controller.asrDevice !== "undefined")
                            controller.asrDevice = settingsAiDubbingCard.asrDeviceOptions[index].value;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Số luồng CPU cho ASR")
                description: qsTr("0 = tự động (tối đa 16 luồng). Tăng giúp CPU nhận diện nhanh hơn.")
                control: AppNumberField {
                    objectName: "asrThreadsField"
                    decimals: 0
                    scaleFactor: 1
                    from: 0
                    to: 16
                    stepSize: 1
                    value: (controller && typeof controller.asrThreads === "number")
                        ? controller.asrThreads : 0
                    onValueModified: {
                        if (controller && typeof controller.asrThreads !== "undefined")
                            controller.asrThreads = value;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Mô hình Whisper (GGUF)")
                description: qsTr("Chọn và tải mô hình Whisper để nhận diện phụ đề ngoại tuyến trên CPU/GPU")
                control: AppCombo {
                    objectName: "asrModelField"
                    comboWidth: 320
                    model: settingsAiDubbingCard.asrModelOptions
                    textRole: "label"
                    currentIndex: section.page
                        ? section.page.valueIndex(
                            settingsAiDubbingCard.asrModelOptions,
                            (controller && typeof controller.asrModel === "string")
                                ? controller.asrModel : "large-v3-turbo")
                        : 4
                    onActivated: function (index) {
                        if (controller && typeof controller.asrModel !== "undefined")
                            controller.asrModel = settingsAiDubbingCard.asrModelOptions[index].value;
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        objectName: "downloadAsrModelButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "download"
                        text: (controller && controller.asrDownloadBusy)
                            ? qsTr("Đang tải ASR…")
                            : ((controller && controller.asrModelInstalled)
                                ? qsTr("Tải lại mô hình ASR")
                                : qsTr("Tải mô hình ASR"))
                        enabled: Boolean(controller && !controller.asrDownloadBusy)
                        onClicked: {
                            if (controller && typeof controller.downloadAsrModel === "function")
                                controller.downloadAsrModel();
                        }
                    }

                    AppButton {
                        objectName: "downloadWhisperCppRuntimeButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "download"
                        text: (controller && controller.asrDownloadBusy)
                            ? qsTr("Đang cài…")
                            : ((controller && controller.whisperCppInstalled)
                                ? qsTr("Tải lại runtime")
                                : qsTr("Cài runtime whisper.cpp"))
                        enabled: Boolean(controller && !controller.asrDownloadBusy)
                        onClicked: {
                            if (controller && typeof controller.downloadWhisperCppRuntime === "function")
                                controller.downloadWhisperCppRuntime();
                        }
                    }

                    AppButton {
                        objectName: "removeAsrModelButton"
                        variant: "secondary"
                        size: "sm"
                        visible: Boolean(controller && controller.asrModelInstalled)
                        enabled: Boolean(controller && !controller.asrDownloadBusy)
                        text: qsTr("Xóa")
                        onClicked: {
                            if (controller && typeof controller.removeAsrModel === "function")
                                controller.removeAsrModel();
                        }
                    }

                    AppButton {
                        objectName: "removeWhisperCppRuntimeButton"
                        variant: "secondary"
                        size: "sm"
                        visible: Boolean(controller && controller.whisperCppInstalled)
                        enabled: Boolean(controller && !controller.asrDownloadBusy)
                        text: qsTr("Xóa runtime")
                        onClicked: {
                            if (controller && typeof controller.removeWhisperCppRuntime === "function")
                                controller.removeWhisperCppRuntime();
                        }
                    }
                }

                ProgressBar {
                    objectName: "asrDownloadProgressBar"
                    Layout.fillWidth: true
                    from: 0
                    to: 1
                    value: (controller && typeof controller.asrDownloadProgress === "number")
                        ? controller.asrDownloadProgress : 0
                    visible: Boolean(controller && controller.asrDownloadBusy)
                    Accessible.name: qsTr("Tiến trình tải mô hình và runtime ASR")
                }

                Label {
                    objectName: "asrInstallStatusLabel"
                    Layout.fillWidth: true
                    text: {
                        if (!controller)
                            return "";
                        if (controller.asrDownloadStatus && controller.asrDownloadStatus !== "")
                            return controller.asrDownloadStatus;
                        if (controller.asrModelInstalled && controller.whisperCppInstalled)
                            return qsTr("Đã cài đặt mô hình Whisper + %1 (sẵn sàng chạy ngoại tuyến).")
                                .arg(settingsAiDubbingCard.asrRuntimeLabel);
                        if (controller.asrModelInstalled)
                            return qsTr("Đã tải mô hình Whisper nhưng chưa có runtime whisper.cpp — hãy bấm 'Cài runtime whisper.cpp'.");
                        return qsTr("Chưa tải về máy.");
                    }
                    color: (controller && controller.asrModelInstalled && controller.whisperCppInstalled)
                        ? Theme.accent : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                Label {
                    objectName: "asrDownloadPercentLabel"
                    Layout.fillWidth: true
                    visible: Boolean(controller && controller.asrDownloadBusy)
                    text: qsTr("Đã tải %1%").arg(Math.round(
                        (controller ? controller.asrDownloadProgress : 0) * 100))
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }
            }

            // FR-3.4: auto mode uploads audio only behind this opt-in, and only
            // when no local ASR (runtime + model) is available.
            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Dự phòng đám mây cho ASR")
                description: qsTr("Chỉ ở chế độ Tự động: khi máy chưa có ASR cục bộ (whisper.cpp hoặc faster-whisper kèm mô hình), âm thanh được gửi lên Máy chủ Whisper API bên dưới. Tắt thì âm thanh không rời khỏi máy.")

                AppToggle {
                    objectName: "asrCloudFallbackToggle"
                    text: qsTr("Cho phép dự phòng đám mây")
                    accessibleLabel: qsTr("Cho phép gửi âm thanh lên Whisper API khi không có ASR cục bộ")
                    checked: Boolean(controller && controller.asrCloudFallback === true)
                    onToggled: {
                        if (controller && typeof controller.asrCloudFallback !== "undefined")
                            controller.asrCloudFallback = checked;
                    }
                }
            }

            // FR-7.1: Separate Whisper API group (URL / model / API key).
            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Máy chủ Whisper API (URL)")
                description: qsTr("Để trống để dùng chung máy chủ LLM chỉ khi Bộ nhận diện đặt là Máy chủ Whisper API. Chế độ Tự động chỉ gửi âm thanh lên URL này khi bật “Cho phép dự phòng đám mây” và máy chưa có ASR cục bộ.")
                descriptionObjectName: "asrApiBaseUrlDescription"
                control: AppTextField {
                    objectName: "asrApiBaseUrlField"
                    implicitWidth: 260
                    accessibleLabel: qsTr("Máy chủ Whisper API (URL)")
                    placeholderText: "https://api.openai.com/v1"
                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                    text: (controller && typeof controller.asrApiBaseUrl === "string")
                        ? controller.asrApiBaseUrl : ""
                    onEditingFinished: {
                        if (controller && typeof controller.asrApiBaseUrl !== "undefined")
                            controller.asrApiBaseUrl = text;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Tên mô hình Whisper API")
                description: qsTr("Tên mô hình gửi lên trường model của /audio/transcriptions (mặc định whisper-1)")
                control: AppTextField {
                    objectName: "asrApiModelField"
                    implicitWidth: 220
                    accessibleLabel: qsTr("Tên mô hình Whisper API")
                    placeholderText: "whisper-1"
                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                    text: (controller && typeof controller.asrApiModel === "string")
                        ? controller.asrApiModel : ""
                    onEditingFinished: {
                        if (controller && typeof controller.asrApiModel !== "undefined")
                            controller.asrApiModel = text;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Whisper API Key (tùy chọn)")
                description: qsTr("Chỉ gửi tới máy chủ Whisper API đã cấu hình; khóa LLM không bao giờ tự gửi sang máy chủ ASR khác")
                control: AppTextField {
                    objectName: "asrApiKeyField"
                    implicitWidth: 220
                    accessibleLabel: qsTr("Whisper API Key")
                    echoMode: TextInput.Password
                    placeholderText: "sk-..."
                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                    text: (controller && typeof controller.asrApiKey === "string")
                        ? controller.asrApiKey : ""
                    onEditingFinished: {
                        if (controller && typeof controller.asrApiKey !== "undefined")
                            controller.asrApiKey = text;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Phân tách người nói (Diarization)")
                description: qsTr("Gán nhãn từng người nói (tối đa 8 người) để tự động phân vai giọng đọc")
                control: AppCombo {
                    objectName: "diarizationBackendCombo"
                    comboWidth: 320
                    model: settingsAiDubbingCard.diarBackendOptions
                    textRole: "label"
                    currentIndex: section.page
                        ? section.page.valueIndex(
                            settingsAiDubbingCard.diarBackendOptions,
                            (controller && typeof controller.diarizationBackend === "string") ? controller.diarizationBackend : "auto")
                        : 0
                    onActivated: function (index) {
                        if (controller && typeof controller.diarizationBackend !== "undefined")
                            controller.diarizationBackend = settingsAiDubbingCard.diarBackendOptions[index].value;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Thiết bị chạy Diarization")
                description: qsTr("Tự động dùng backend GPU nếu runtime hỗ trợ, ngược lại CPU. Đổi thiết bị cần cài lại runtime tương ứng.")
                control: AppCombo {
                    objectName: "diarizationDeviceCombo"
                    comboWidth: 320
                    model: settingsAiDubbingCard.diarDeviceOptions
                    textRole: "label"
                    currentIndex: section.page
                        ? section.page.valueIndex(
                            settingsAiDubbingCard.diarDeviceOptions,
                            (controller && typeof controller.diarizationDevice === "string") ? controller.diarizationDevice : "auto")
                        : 0
                    onActivated: function (index) {
                        if (controller && typeof controller.diarizationDevice !== "undefined")
                            controller.diarizationDevice = settingsAiDubbingCard.diarDeviceOptions[index].value;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Số luồng CPU cho Diarization")
                description: qsTr("0 = tự động (tối đa 16 luồng). Tăng giúp CPU chạy nhanh hơn; GPU bỏ qua giá trị này.")
                control: AppNumberField {
                    objectName: "diarizationThreadsField"
                    decimals: 0
                    scaleFactor: 1
                    from: 0
                    to: 16
                    stepSize: 1
                    value: (controller && typeof controller.diarizationThreads === "number")
                        ? controller.diarizationThreads : 0
                    onValueModified: {
                        if (controller && typeof controller.diarizationThreads !== "undefined")
                            controller.diarizationThreads = value;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Mô hình Nemotron-3 Diarization (GGUF)")
                description: qsTr("audio-cpp/Nemotron-3-Diarization-GGUF chạy trên CPU/Vulkan/Metal qua audio.cpp")
                control: AppCombo {
                    objectName: "diarizationModelCombo"
                    comboWidth: 320
                    model: settingsAiDubbingCard.diarModelOptions
                    textRole: "label"
                    currentIndex: section.page
                        ? section.page.valueIndex(
                            settingsAiDubbingCard.diarModelOptions,
                            (controller && typeof controller.diarizationModel === "string")
                                ? controller.diarizationModel : "nemotron-3-diarization-q8_0")
                        : 0
                    onActivated: function (index) {
                        if (controller && typeof controller.diarizationModel !== "undefined")
                            controller.diarizationModel = settingsAiDubbingCard.diarModelOptions[index].value;
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        objectName: "downloadDiarizationModelButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "download"
                        text: (controller && controller.diarizationDownloadBusy)
                            ? qsTr("Đang tải…")
                            : ((controller && controller.diarizationModelInstalled)
                                ? qsTr("Tải lại Nemotron-3 GGUF")
                                : qsTr("Tải mô hình Nemotron-3 GGUF"))
                        enabled: Boolean(controller && !controller.diarizationDownloadBusy)
                        onClicked: {
                            if (controller && typeof controller.downloadDiarizationModel === "function")
                                controller.downloadDiarizationModel();
                        }
                    }

                    AppButton {
                        objectName: "downloadAudiocppRuntimeButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "download"
                        text: (controller && controller.diarizationDownloadBusy)
                            ? qsTr("Đang cài…")
                            : ((controller && controller.audiocppInstalled)
                                ? qsTr("Tải lại runtime")
                                : qsTr("Cài runtime audio.cpp"))
                        enabled: Boolean(controller && !controller.diarizationDownloadBusy)
                        onClicked: {
                            if (controller && typeof controller.downloadAudiocppRuntime === "function")
                                controller.downloadAudiocppRuntime();
                        }
                    }

                    AppButton {
                        objectName: "removeDiarizationModelButton"
                        variant: "secondary"
                        size: "sm"
                        visible: Boolean(controller && controller.diarizationModelInstalled)
                        enabled: Boolean(controller && !controller.diarizationDownloadBusy)
                        text: qsTr("Xóa mô hình")
                        onClicked: {
                            if (controller && typeof controller.removeDiarizationModel === "function")
                                controller.removeDiarizationModel();
                        }
                    }

                    AppButton {
                        objectName: "removeAudiocppRuntimeButton"
                        variant: "secondary"
                        size: "sm"
                        visible: Boolean(controller && controller.audiocppInstalled)
                        enabled: Boolean(controller && !controller.diarizationDownloadBusy)
                        text: qsTr("Xóa runtime")
                        onClicked: {
                            if (controller && typeof controller.removeAudiocppRuntime === "function")
                                controller.removeAudiocppRuntime();
                        }
                    }
                }

                ProgressBar {
                    objectName: "diarizationDownloadProgressBar"
                    Layout.fillWidth: true
                    from: 0
                    to: 1
                    value: (controller && typeof controller.diarizationDownloadProgress === "number")
                        ? controller.diarizationDownloadProgress : 0
                    visible: Boolean(controller && controller.diarizationDownloadBusy)
                    Accessible.name: qsTr("Tiến trình tải mô hình và runtime Diarization")
                }

                Label {
                    objectName: "diarizationInstallStatusLabel"
                    Layout.fillWidth: true
                    text: {
                        if (!controller)
                            return "";
                        if (controller.diarizationDownloadStatus && controller.diarizationDownloadStatus !== "")
                            return controller.diarizationDownloadStatus;
                        if (controller.diarizationModelInstalled && controller.audiocppInstalled)
                            return qsTr("Đã cài đặt Nemotron-3 GGUF + %1 (sẵn sàng chạy ngoại tuyến).")
                                .arg(settingsAiDubbingCard.diarRuntimeLabel);
                        if (controller.diarizationModelInstalled)
                            return qsTr("Đã tải GGUF nhưng chưa có runtime audio.cpp — hãy bấm 'Cài runtime audio.cpp'.");
                        return qsTr("Chưa tải (đang dùng Phân cụm âm học Offline dự phòng).");
                    }
                    color: (controller && controller.diarizationModelInstalled && controller.audiocppInstalled)
                        ? Theme.accent : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                Label {
                    objectName: "diarizationDownloadPercentLabel"
                    Layout.fillWidth: true
                    visible: Boolean(controller && controller.diarizationDownloadBusy)
                    text: qsTr("Đã tải %1%").arg(Math.round(
                        (controller ? controller.diarizationDownloadProgress : 0) * 100))
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Số người nói tối đa (0 = tự động)")
                description: qsTr("Giới hạn trên, không phải số lượng cố định. Nemotron-3 hỗ trợ tối đa 8 người nói.")
                control: AppNumberField {
                    objectName: "diarizationMaxSpeakersField"
                    decimals: 0
                    scaleFactor: 1
                    from: 0
                    to: 8
                    stepSize: 1
                    value: (controller && typeof controller.diarizationMaxSpeakers === "number")
                        ? controller.diarizationMaxSpeakers : 0
                    onValueModified: {
                        if (controller && typeof controller.diarizationMaxSpeakers !== "undefined")
                            controller.diarizationMaxSpeakers = value;
                    }
                }
            }

            // FR-7.1: optional audio models — each improves one step when
            // installed; without them the built-in fallbacks run.
            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm
                visible: Boolean(controller && controller.audioModels && controller.audioModels.length > 0)

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Mô hình âm thanh bổ sung (tùy chọn)")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    font.weight: Theme.fontWeightMedium
                    wrapMode: Text.Wrap
                }

                AudioModelRow {
                    objectName: "settingsWhisperVadRow"
                    compact: section.compact
                    modelId: "whisper-vad-silero-v6.2.0"
                    label: qsTr("Whisper VAD (lọc đoạn không lời)")
                    purpose: qsTr("Dùng cho whisper.cpp: bỏ qua nhạc và khoảng lặng để giảm câu bịa khi nhận dạng")
                }

                AudioModelRow {
                    objectName: "settingsSileroVadRow"
                    compact: section.compact
                    modelId: "silero-vad"
                    label: qsTr("Silero VAD (ranh giới lời nói)")
                    purpose: qsTr("Căn đầu và cuối phụ đề theo lời nói thật, chọn đoạn mẫu giọng sạch hơn")
                }

                AudioModelRow {
                    objectName: "settingsSpeakerEmbeddingRow"
                    compact: section.compact
                    modelId: "speaker-embedding"
                    label: qsTr("Nhận diện giọng người nói (CAM++)")
                    purpose: qsTr("3D-Speaker CAM++: khi đã cài, giúp phân biệt người nói chính xác hơn khi phân tách người nói")
                }

                ProgressBar {
                    objectName: "settingsAudioModelProgressBar"
                    Layout.fillWidth: true
                    from: 0
                    to: 1
                    value: (controller && typeof controller.audioModelProgress === "number")
                        ? controller.audioModelProgress : 0
                    visible: Boolean(controller && controller.audioModelBusyId)
                    Accessible.name: qsTr("Tiến trình tải mô hình âm thanh")
                }

                Label {
                    objectName: "settingsAudioModelStatusLabel"
                    Layout.fillWidth: true
                    visible: text !== ""
                    text: (controller && controller.audioModelStatus) ? controller.audioModelStatus : ""
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Máy chủ LLM (OpenAI-compatible URL)")
                description: qsTr("Hỗ trợ Ollama (http://localhost:11434/v1), LM Studio, vLLM hoặc OpenAI")
                control: TextField {
                    objectName: "llmBaseUrlField"
                    implicitWidth: 260
                    implicitHeight: 36
                    leftPadding: Theme.spacingSm
                    rightPadding: Theme.spacingSm
                    text: (controller && typeof controller.llmBaseUrl === "string")
                        ? controller.llmBaseUrl : "http://localhost:11434/v1"
                    placeholderText: "http://localhost:11434/v1"
                    placeholderTextColor: Theme.textSubtle
                    color: Theme.text
                    selectedTextColor: Theme.accentText
                    selectionColor: Theme.accent
                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    background: Rectangle {
                        radius: Theme.radiusMd
                        color: Theme.surfaceAlt
                        border.width: 1
                        border.color: parent.activeFocus ? Theme.accent : Theme.borderSubtle
                    }
                    onEditingFinished: {
                        if (controller && typeof controller.llmBaseUrl !== "undefined")
                            controller.llmBaseUrl = text;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Tên mô hình LLM")
                description: qsTr("Mô hình dùng để làm sạch ASR, rút gọn khớp thời gian và dịch lồng tiếng")
                control: TextField {
                    objectName: "llmModelField"
                    implicitWidth: 220
                    implicitHeight: 36
                    leftPadding: Theme.spacingSm
                    rightPadding: Theme.spacingSm
                    text: (controller && typeof controller.llmModel === "string")
                        ? controller.llmModel : "qwen2.5:7b"
                    placeholderText: "qwen2.5:7b"
                    placeholderTextColor: Theme.textSubtle
                    color: Theme.text
                    selectedTextColor: Theme.accentText
                    selectionColor: Theme.accent
                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    background: Rectangle {
                        radius: Theme.radiusMd
                        color: Theme.surfaceAlt
                        border.width: 1
                        border.color: parent.activeFocus ? Theme.accent : Theme.borderSubtle
                    }
                    onEditingFinished: {
                        if (controller && typeof controller.llmModel !== "undefined")
                            controller.llmModel = text;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("LLM API Key (tùy chọn)")
                description: qsTr("Để trống nếu dùng Ollama / LM Studio nội bộ")
                control: TextField {
                    objectName: "llmApiKeyField"
                    implicitWidth: 220
                    implicitHeight: 36
                    leftPadding: Theme.spacingSm
                    rightPadding: Theme.spacingSm
                    echoMode: TextInput.Password
                    text: (controller && typeof controller.llmApiKey === "string")
                        ? controller.llmApiKey : ""
                    placeholderText: "sk-..."
                    placeholderTextColor: Theme.textSubtle
                    color: Theme.text
                    selectedTextColor: Theme.accentText
                    selectionColor: Theme.accent
                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    background: Rectangle {
                        radius: Theme.radiusMd
                        color: Theme.surfaceAlt
                        border.width: 1
                        border.color: parent.activeFocus ? Theme.accent : Theme.borderSubtle
                    }
                    onEditingFinished: {
                        if (controller && typeof controller.llmApiKey !== "undefined")
                            controller.llmApiKey = text;
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                AppButton {
                    objectName: "llmTestConnectionButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("Kiểm tra kết nối LLM")
                    enabled: Boolean(controller && !controller.llmTesting)
                    onClicked: {
                        if (controller && typeof controller.testLlmConnection === "function")
                            controller.testLlmConnection();
                    }
                }

                Label {
                    objectName: "llmTestStatusLabel"
                    Layout.fillWidth: true
                    text: (controller && typeof controller.llmTestStatus === "string")
                        ? controller.llmTestStatus : ""
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    elide: Text.ElideRight
                }
            }

            // FR-7.1: Collapsed-by-default "LLM advanced" disclosure.
            SettingsDisclosure {
                id: llmAdvancedDisclosure

                objectName: "llmAdvancedDisclosure"
                toggleObjectName: "llmAdvancedToggle"
                filter: section.filter
                title: qsTr("Tùy chọn LLM nâng cao")
                description: qsTr("Mô hình riêng cho từng tác vụ, số yêu cầu song song, thời gian chờ và kích thước lô")
                keywords: "LLM refine translate fit concurrency timeout batch"

                readonly property string mainLlmModel: (controller
                    && typeof controller.llmModel === "string"
                    && controller.llmModel !== "")
                    ? controller.llmModel : "qwen2.5:7b"

                SettingRow {
                    Layout.fillWidth: true
                    compact: section.compact
                    label: qsTr("Mô hình làm sạch ASR (tùy chọn)")
                    description: qsTr("Để trống để dùng tên mô hình LLM chính ở trên")
                    control: AppTextField {
                        objectName: "llmModelRefineField"
                        implicitWidth: 220
                        accessibleLabel: qsTr("Mô hình làm sạch ASR")
                        placeholderText: llmAdvancedDisclosure.mainLlmModel
                        font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                        text: (controller && typeof controller.llmModelRefine === "string")
                            ? controller.llmModelRefine : ""
                        onEditingFinished: {
                            if (controller && typeof controller.llmModelRefine !== "undefined")
                                controller.llmModelRefine = text;
                        }
                    }
                }

                SettingRow {
                    Layout.fillWidth: true
                    compact: section.compact
                    label: qsTr("Mô hình dịch lồng tiếng (tùy chọn)")
                    description: qsTr("Để trống để dùng tên mô hình LLM chính ở trên")
                    control: AppTextField {
                        objectName: "llmModelTranslateField"
                        implicitWidth: 220
                        accessibleLabel: qsTr("Mô hình dịch lồng tiếng")
                        placeholderText: llmAdvancedDisclosure.mainLlmModel
                        font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                        text: (controller && typeof controller.llmModelTranslate === "string")
                            ? controller.llmModelTranslate : ""
                        onEditingFinished: {
                            if (controller && typeof controller.llmModelTranslate !== "undefined")
                                controller.llmModelTranslate = text;
                        }
                    }
                }

                SettingRow {
                    Layout.fillWidth: true
                    compact: section.compact
                    label: qsTr("Mô hình rút gọn khớp thời gian (tùy chọn)")
                    description: qsTr("Để trống để dùng tên mô hình LLM chính ở trên")
                    control: AppTextField {
                        objectName: "llmModelFitField"
                        implicitWidth: 220
                        accessibleLabel: qsTr("Mô hình rút gọn khớp thời gian")
                        placeholderText: llmAdvancedDisclosure.mainLlmModel
                        font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                        text: (controller && typeof controller.llmModelFit === "string")
                            ? controller.llmModelFit : ""
                        onEditingFinished: {
                            if (controller && typeof controller.llmModelFit !== "undefined")
                                controller.llmModelFit = text;
                        }
                    }
                }

                SettingRow {
                    Layout.fillWidth: true
                    compact: section.compact
                    label: qsTr("Số yêu cầu LLM song song")
                    description: qsTr("0 = tự động (1 cho máy chủ nội bộ, 3 cho máy chủ đám mây; tối đa 8)")
                    control: AppNumberField {
                        objectName: "llmConcurrencyField"
                        accessibleLabel: qsTr("Số yêu cầu LLM song song")
                        decimals: 0
                        scaleFactor: 1
                        from: 0
                        to: 8
                        stepSize: 1
                        value: (controller && typeof controller.llmConcurrency === "number")
                            ? controller.llmConcurrency : 0
                        onValueModified: {
                            if (controller && typeof controller.llmConcurrency !== "undefined")
                                controller.llmConcurrency = value;
                        }
                    }
                }

                SettingRow {
                    Layout.fillWidth: true
                    compact: section.compact
                    label: qsTr("Thời gian chờ mỗi yêu cầu LLM (giây)")
                    description: qsTr("Giới hạn thời gian chờ phản hồi từ máy chủ LLM (15 – 600 giây, mặc định 120)")
                    control: AppNumberField {
                        objectName: "llmTimeoutField"
                        accessibleLabel: qsTr("Thời gian chờ mỗi yêu cầu LLM (giây)")
                        decimals: 0
                        scaleFactor: 1
                        from: 15
                        to: 600
                        stepSize: 15
                        value: (controller && typeof controller.llmTimeoutS === "number")
                            ? controller.llmTimeoutS : 120
                        onValueModified: {
                            if (controller && typeof controller.llmTimeoutS !== "undefined")
                                controller.llmTimeoutS = value;
                        }
                    }
                }

                SettingRow {
                    Layout.fillWidth: true
                    compact: section.compact
                    label: qsTr("Số câu phụ đề mỗi lô LLM")
                    description: qsTr("Số câu gửi trong một yêu cầu (5 – 60, mặc định 25; giảm nếu dùng Ollama 4k context)")
                    control: AppNumberField {
                        objectName: "llmBatchSizeField"
                        accessibleLabel: qsTr("Số câu phụ đề mỗi lô LLM")
                        decimals: 0
                        scaleFactor: 1
                        from: 5
                        to: 60
                        stepSize: 5
                        value: (controller && typeof controller.llmBatchSize === "number")
                            ? controller.llmBatchSize : 25
                        onValueModified: {
                            if (controller && typeof controller.llmBatchSize !== "undefined")
                                controller.llmBatchSize = value;
                        }
                    }
                }
            }
        }
    }
}
