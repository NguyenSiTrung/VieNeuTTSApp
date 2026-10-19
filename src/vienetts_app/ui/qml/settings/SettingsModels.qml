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
            title, "Whisper ASR diarization NeMo LLM Ollama OpenAI subtitle video dubbing"
        ])

        objectName: "settingsAiDubbingCard"
        visible: matches
        Layout.fillWidth: true
        title: qsTr("AI Phụ đề & Lồng tiếng Video (ASR / Diarization / LLM)")
        subtitle: qsTr("Cấu hình nhận diện giọng nói (Whisper), phân tách người nói (NeMo / Âm học) và LLM tinh chỉnh phụ đề")

        readonly property var asrBackendOptions: [
            { value: "auto", label: qsTr("Tự động (Local Whisper / API)") },
            { value: "faster_whisper", label: qsTr("On-device faster-whisper") },
            { value: "whisper_cpp", label: qsTr("On-device whisper.cpp") },
            { value: "openai_api", label: qsTr("Máy chủ Whisper API (OpenAI)") }
        ]

        readonly property var diarBackendOptions: [
            { value: "auto", label: qsTr("Tự động (NeMo / Phân cụm âm học)") },
            { value: "acoustic", label: qsTr("Phân cụm âm học Offline (nhanh, sẵn có)") },
            { value: "nemo", label: qsTr("NVIDIA NeMo Diarizer") }
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
                label: qsTr("Mô hình Whisper")
                description: qsTr("Tên mô hình (tiny, base, small, medium, large-v3-turbo) hoặc đường dẫn tệp")
                control: TextField {
                    objectName: "asrModelField"
                    implicitWidth: 220
                    implicitHeight: 36
                    leftPadding: Theme.spacingSm
                    rightPadding: Theme.spacingSm
                    text: (controller && typeof controller.asrModel === "string") ? controller.asrModel : "base"
                    placeholderText: "base"
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
                        if (controller && typeof controller.asrModel !== "undefined")
                            controller.asrModel = text;
                    }
                }
            }

            SettingRow {
                Layout.fillWidth: true
                compact: section.compact
                label: qsTr("Phân tách người nói (Diarization)")
                description: qsTr("Gán nhãn từng người nói để tự động phân vai giọng đọc")
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
                label: qsTr("Số người nói tối đa (0 = tự động)")
                control: AppNumberField {
                    objectName: "diarizationMaxSpeakersField"
                    decimals: 0
                    scaleFactor: 1
                    from: 0
                    to: 16
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
