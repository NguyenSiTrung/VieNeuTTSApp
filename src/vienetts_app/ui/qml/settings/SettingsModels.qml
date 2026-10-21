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
            title, "Whisper ASR diarization Nemotron audio.cpp Vulkan LLM Ollama OpenAI subtitle video dubbing"
        ])

        objectName: "settingsAiDubbingCard"
        visible: matches
        Layout.fillWidth: true
        title: qsTr("AI Phụ đề & Lồng tiếng Video (ASR / Diarization / LLM)")
        subtitle: qsTr("Cấu hình nhận diện giọng nói (Whisper), phân tách người nói (Nemotron-3 GGUF / Âm học) và LLM tinh chỉnh phụ đề")

        readonly property string diarRuntimeLabel: (controller && controller.audiocppInstalled)
            ? qsTr("audio.cpp: %1").arg(controller.audiocppRuntimeVariant || "cpu")
            : qsTr("audio.cpp: chưa cài")

        readonly property var asrBackendOptions: [
            { value: "auto", label: qsTr("Tự động (Local Whisper / API)") },
            { value: "faster_whisper", label: qsTr("On-device faster-whisper") },
            { value: "whisper_cpp", label: qsTr("On-device whisper.cpp") },
            { value: "openai_api", label: qsTr("Máy chủ Whisper API (OpenAI)") }
        ]

        readonly property var asrModelOptions: [
            { value: "tiny", label: qsTr("Whisper Tiny (75 MB — Siêu nhanh)") },
            { value: "base", label: qsTr("Whisper Base (142 MB — Mặc định)") },
            { value: "small", label: qsTr("Whisper Small (466 MB — Chính xác cao)") },
            { value: "medium", label: qsTr("Whisper Medium Q5_0 (514 MB)") },
            { value: "large-v3-turbo", label: qsTr("Whisper Large-v3-Turbo Q5_0 (547 MB)") }
        ]

        readonly property var diarBackendOptions: [
            { value: "auto", label: qsTr("Tự động (Nemotron-3 GGUF / Âm học)") },
            { value: "nemotron_gguf", label: qsTr("Nemotron-3 GGUF (audio.cpp — CPU/GPU)") },
            { value: "acoustic", label: qsTr("Phân cụm âm học Offline (dự phòng, kém chính xác)") }
        ]

        readonly property var diarDeviceOptions: {
            const all = [
                { value: "auto", label: qsTr("Tự động (runtime chọn backend tốt nhất)") },
                { value: "cpu", label: qsTr("CPU (mọi máy, ổn định nhất)") },
                { value: "vulkan", label: qsTr("Vulkan GPU (nhanh hơn, cần runtime Vulkan)") },
                { value: "metal", label: qsTr("Metal GPU (macOS Apple Silicon)") }
            ];
            if (!controller || typeof controller.audiocppAvailableVariants === "undefined")
                return all;
            const available = controller.audiocppAvailableVariants || [];
            return all.filter(function (option) {
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

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Bộ nhận diện giọng nói (ASR)")
                description: qsTr("Tạo phụ đề tự động khi video chưa có tệp .srt")
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
                            (controller && typeof controller.asrModel === "string") ? controller.asrModel : "base")
                        : 1
                    onActivated: function (index) {
                        if (controller && typeof controller.asrModel !== "undefined")
                            controller.asrModel = settingsAiDubbingCard.asrModelOptions[index].value;
                    }
                }
            }

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

                Label {
                    objectName: "asrInstallStatusLabel"
                    Layout.fillWidth: true
                    text: {
                        if (!controller)
                            return "";
                        if (controller.asrDownloadStatus && controller.asrDownloadStatus !== "")
                            return controller.asrDownloadStatus;
                        return controller.asrModelInstalled
                            ? qsTr("Đã cài đặt — sẵn sàng chạy ngoại tuyến.")
                            : qsTr("Chưa tải về máy.");
                    }
                    color: (controller && controller.asrModelInstalled) ? Theme.accent : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    elide: Text.ElideRight
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
        }
    }
}
