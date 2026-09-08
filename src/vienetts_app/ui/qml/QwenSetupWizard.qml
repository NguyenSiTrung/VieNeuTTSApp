// Qwen optional-pack setup wizard: three Yes/No steps (use Qwen → need
// cloning → VRAM notice) plus an action page. Hybrid by design: checkpoint
// downloads run in-app through controller.downloadQwenModel (progress +
// cancel, same lane discipline as the CUDA card); the pip runtime install is
// NEVER automated (the app never invokes pip) so the action page always
// shows the copyable commands as fallback (frozen builds, offline machines).
//
// Tested contract (tests/smoke/test_ui_tabs.py settings_qwen_wizard):
// qwenSetupWizard, qwenWizardStepLabel, qwenWizardNoButton,
// qwenWizardYesButton, qwenWizardBackButton, qwenWizardContinueButton,
// qwenWizardDownloadCustomVoiceButton, qwenWizardDownloadBaseButton,
// qwenWizardCancelButton, qwenWizardCloseButton, qwenWizardProgress,
// qwenWizardErrorLabel, qwenWizardFetchCommand, qwenWizardReadyLabel,
// qwenWizardCheckpointGroup, qwenWizardRuntimeGroup, qwenWizardRefreshButton,
// qwenWizardLastCheckLabel, qwenStatusRuntime, qwenStatusTorch,
// qwenStatusCustomVoice, qwenStatusBase.
// QwenStatusList.qml owns the four qwenStatus* rows.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "components"
import "."

Dialog {
    id: root

    objectName: "qwenSetupWizard"
    title: qsTr("Thiết lập gói Qwen")
    modal: true
    focus: true
    anchors.centerIn: Overlay.overlay
    width: 560
    padding: Theme.spacingLg

    background: Rectangle {
        radius: Theme.radiusLg
        color: Theme.surfaceCard
        border.color: Theme.border
        border.width: 1
    }

    header: Label {
        text: root.title
        color: Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLg
        font.weight: Theme.fontWeightHeading
        padding: Theme.spacingLg
        bottomPadding: Theme.spacingSm
    }
    // 0 = choose pack, 1 = choose runtime, 2 = install and verify.
    property int step: 0
    property string packVariant: "customvoice"
    property string lastCheck: ""
    property bool copiedFetch: false
    property bool copiedRuntime: false
    property string runtimeVariant: "cpu"
    readonly property bool needCloning: packVariant === "cloning"
    // Fetch commands mirror core/qwen_models.wizard_fetch_command — QML
    // cannot import Python constants, keep the two in sync.
    readonly property string customVoiceRepo: "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    readonly property string baseRepo: "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    readonly property string runtimeCommandCpu: "uv pip install -e \".[qwen]\""
    readonly property string runtimeCommandCuda: "uv pip install -e \".[qwen]\" && uv pip install --index-url https://download.pytorch.org/whl/cu128 \"torch==2.8.0+cu128\" \"torchaudio==2.8.0+cu128\""
    readonly property string runtimeCommand: runtimeVariant === "cuda" ? runtimeCommandCuda : runtimeCommandCpu
    readonly property string fetchCommand: needCloning
        ? ("python scripts/fetch_qwen_models.py --repo \"" + customVoiceRepo + "\" --repo \"" + baseRepo + "\"")
        : ("python scripts/fetch_qwen_models.py --repo \"" + customVoiceRepo + "\"")

    Timer {
        id: copyFetchTimer
        interval: 2000
        onTriggered: root.copiedFetch = false
    }

    Timer {
        id: copyRuntimeTimer
        interval: 2000
        onTriggered: root.copiedRuntime = false
    }
    // Live pack state (re-evaluated on qwenReadinessChanged): the action page
    // shows ONLY what this walk still needs — no red marks on pieces the
    // user never asked for.
    readonly property var packStatus: controller ? controller.qwenReadiness : null
    readonly property var packModels: (packStatus && packStatus.models) || {}
    readonly property bool packReady: !!(packStatus && packStatus.ready)
    readonly property bool runtimeMissing: !(packStatus && packStatus.runtime && packStatus.torch)
    readonly property bool customVoiceMissing: !packModels.customvoice
    readonly property bool baseMissing: root.needCloning && !packModels.base
    readonly property bool checkpointMissing: customVoiceMissing || baseMissing

    onOpened: {
        root.step = 0;
        root.packVariant = "customvoice";
        root.lastCheck = "";
        root.copiedFetch = false;
        root.copiedRuntime = false;
        root.runtimeVariant = "cpu";
        if (controller)
            controller.refreshQwenReadiness();
    }

    contentItem: ColumnLayout {
        spacing: Theme.spacingMd

        Label {
            id: qwenWizardStageLabel
            objectName: "qwenWizardStageLabel"
            Layout.fillWidth: true
            text: qsTr("Bước %1/3").arg(root.step + 1)
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        Label {
            id: qwenWizardStepLabel
            objectName: "qwenWizardStepLabel"
            Layout.fillWidth: true
            text: root.step === 0 ? qsTr("Chọn gói Qwen") : root.step === 1 ? qsTr("Chọn môi trường chạy") : qsTr("Cài đặt và kiểm tra")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeLg
            font.weight: Theme.fontWeightHeading
        }

        ColumnLayout {
            Layout.fillWidth: true
            visible: root.step === 0
            spacing: Theme.spacingSm
            Label { Layout.fillWidth: true; text: qsTr("Qwen hỗ trợ 10 ngôn ngữ ngoài tiếng Việt. Dùng VieNeu cho tiếng Việt."); color: Theme.text; wrapMode: Text.Wrap }
            AppButton {
                id: qwenWizardPackCustomVoiceButton
                objectName: "qwenWizardPackCustomVoiceButton"
                Layout.fillWidth: true; variant: root.packVariant === "customvoice" ? "primary" : "secondary"
                text: qsTr("CustomVoice · 1,5 GB")
                onClicked: { root.packVariant = "customvoice"; root.step = 1; }
            }
            Label { Layout.fillWidth: true; text: qsTr("9 giọng cố định và chỉ dẫn phong cách, không nhân bản."); color: Theme.textMuted; wrapMode: Text.Wrap }
            AppButton {
                id: qwenWizardPackCloningButton
                objectName: "qwenWizardPackCloningButton"
                Layout.fillWidth: true; variant: root.packVariant === "cloning" ? "primary" : "secondary"
                text: qsTr("CustomVoice + Base · 3 GB")
                onClicked: { root.packVariant = "cloning"; root.step = 1; }
            }
            Label { Layout.fillWidth: true; text: qsTr("Thêm nhân bản giọng từ clip 3–8 giây và bản ghi."); color: Theme.textMuted; wrapMode: Text.Wrap }
        }

        ColumnLayout {
            Layout.fillWidth: true; visible: root.step === 1; spacing: Theme.spacingSm
            Label { Layout.fillWidth: true; text: qsTr("Chọn CPU để tương thích rộng. NVIDIA CUDA 12.8 cần khoảng 4 GB VRAM và chạy nhanh hơn."); color: Theme.text; wrapMode: Text.Wrap }
            RowLayout {
                Layout.fillWidth: true
                AppButton { id: qwenWizardCpuModeButton; objectName: "qwenWizardCpuModeButton"; variant: root.runtimeVariant === "cpu" ? "primary" : "secondary"; text: qsTr("CPU"); onClicked: root.runtimeVariant = "cpu" }
                AppButton { id: qwenWizardCudaModeButton; objectName: "qwenWizardCudaModeButton"; variant: root.runtimeVariant === "cuda" ? "primary" : "secondary"; text: qsTr("NVIDIA CUDA 12.8"); onClicked: root.runtimeVariant = "cuda" }
            }
            Label { Layout.fillWidth: true; text: qsTr("Bản standalone không chứa PyTorch. Chạy lệnh bên dưới trong môi trường Python của app."); color: Theme.textMuted; wrapMode: Text.Wrap }
            AppButton { id: qwenWizardInstallContinueButton; objectName: "qwenWizardInstallContinueButton"; Layout.alignment: Qt.AlignRight; variant: "primary"; text: qsTr("Tiếp tục cài đặt"); onClicked: root.step = 2 }
        }

        // -- Step 3: action page --
        ColumnLayout {
            Layout.fillWidth: true
            visible: root.step === 2
            spacing: Theme.spacingSm

            Label {
                Layout.fillWidth: true
                text: root.needCloning
                    ? qsTr("Sẽ dùng: CustomVoice + Base (nhân bản).")
                    : qsTr("Sẽ dùng: CustomVoice (đa ngữ, không nhân bản).")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            QwenStatusList {
                Layout.fillWidth: true
                showBase: root.needCloning
            }

            // Everything this walk needs is present: point at the picker.
            Label {
                id: qwenWizardReadyLabel
                objectName: "qwenWizardReadyLabel"
                Layout.fillWidth: true
                visible: root.packReady
                text: qsTr("Gói Qwen đã sẵn sàng — đóng hộp này, chọn engine Qwen ở phía trên để dùng.")
                color: Theme.successText
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
                lineHeight: 1.3
            }

            // Checkpoints still missing: in-app download first, fetch command
            // as the offline fallback.
            ColumnLayout {
                id: qwenWizardCheckpointGroup
                objectName: "qwenWizardCheckpointGroup"
                Layout.fillWidth: true
                visible: root.checkpointMissing
                spacing: Theme.spacingSm

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Checkpoint chưa đủ — tải trong app (máy ngoại tuyến dùng lệnh fetch bên dưới):")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        id: qwenWizardDownloadCustomVoiceButton
                        objectName: "qwenWizardDownloadCustomVoiceButton"
                        variant: "primary"
                        size: "sm"
                        text: qsTr("Tải CustomVoice")
                        visible: root.customVoiceMissing
                        enabled: controller ? controller.qwenModelState !== "downloading" : false
                        onClicked: controller.downloadQwenModel("qwen_customvoice")
                    }

                    AppButton {
                        id: qwenWizardDownloadBaseButton
                        objectName: "qwenWizardDownloadBaseButton"
                        variant: "primary"
                        size: "sm"
                        text: qsTr("Tải Base")
                        visible: root.baseMissing
                        enabled: controller ? controller.qwenModelState !== "downloading" : false
                        onClicked: controller.downloadQwenModel("qwen_base")
                    }

                    AppButton {
                        id: qwenWizardCancelButton
                        objectName: "qwenWizardCancelButton"
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Hủy tải")
                        visible: controller ? controller.qwenModelState === "downloading" : false
                        onClicked: controller.cancelQwenModelDownload()
                    }
                }

                ProgressBar {
                    id: qwenWizardProgress
                    objectName: "qwenWizardProgress"
                    Layout.fillWidth: true
                    from: 0
                    to: 1
                    value: controller ? controller.qwenModelProgress : 0
                    visible: controller ? controller.qwenModelState === "downloading" : false
                    Accessible.name: qsTr("Tiến trình tải checkpoint Qwen")
                }

                Label {
                    id: qwenWizardErrorLabel
                    objectName: "qwenWizardErrorLabel"
                    Layout.fillWidth: true
                    visible: controller ? controller.qwenModelError !== "" : false
                    text: controller ? controller.qwenModelError : ""
                    color: Theme.errorText
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Lệnh tải ngoại tuyến (chạy trong terminal):")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: fetchTextCol.implicitHeight + Theme.spacingSm * 2
                        color: Theme.surfaceHover
                        radius: Theme.radiusSm
                        border.color: Theme.border
                        border.width: 1

                        ColumnLayout {
                            id: fetchTextCol
                            anchors.fill: parent
                            anchors.margins: Theme.spacingSm

                            Label {
                                id: qwenWizardFetchCommand
                                objectName: "qwenWizardFetchCommand"
                                Layout.fillWidth: true
                                text: root.fetchCommand
                                color: Theme.text
                                font.family: "monospace"
                                font.pixelSize: Theme.fontSizeSm
                                wrapMode: Text.Wrap
                                textFormat: Text.PlainText
                            }
                        }
                    }

                    AppButton {
                        id: qwenWizardCopyFetchCommandButton
                        objectName: "qwenWizardCopyFetchCommandButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "copy"
                        text: root.copiedFetch ? qsTr("Đã chép ✓") : qsTr("Sao chép")
                        onClicked: {
                            if (controller) {
                                controller.copyToClipboard(root.fetchCommand);
                                root.copiedFetch = true;
                                copyFetchTimer.restart();
                            }
                        }
                    }
                }
            }

            // Runtime/torch still missing: manual pip step (never automated).
            ColumnLayout {
                id: qwenWizardRuntimeGroup
                objectName: "qwenWizardRuntimeGroup"
                Layout.fillWidth: true
                visible: root.runtimeMissing
                spacing: Theme.spacingSm

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Runtime còn thiếu — chạy lệnh này trong môi trường Python của app:")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        text: qsTr("Nền tảng chạy:")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }

                    AppButton {
                        id: qwenWizardInstallCpuModeButton
                        objectName: "qwenWizardCpuModeButton"
                        variant: root.runtimeVariant === "cpu" ? "primary" : "quiet"
                        size: "sm"
                        text: qsTr("CPU (mặc định)")
                        onClicked: root.runtimeVariant = "cpu"
                    }

                    AppButton {
                        id: qwenWizardInstallCudaModeButton
                        objectName: "qwenWizardCudaModeButton"
                        variant: root.runtimeVariant === "cuda" ? "primary" : "quiet"
                        size: "sm"
                        text: qsTr("GPU NVIDIA (CUDA 12.8)")
                        onClicked: root.runtimeVariant = "cuda"
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: runtimeTextCol.implicitHeight + Theme.spacingSm * 2
                        color: Theme.surfaceHover
                        radius: Theme.radiusSm
                        border.color: Theme.border
                        border.width: 1

                        ColumnLayout {
                            id: runtimeTextCol
                            anchors.fill: parent
                            anchors.margins: Theme.spacingSm

                            Label {
                                id: qwenWizardRuntimeCommand
                                objectName: "qwenWizardRuntimeCommand"
                                Layout.fillWidth: true
                                text: root.runtimeCommand
                                color: Theme.text
                                font.family: "monospace"
                                font.pixelSize: Theme.fontSizeSm
                                wrapMode: Text.Wrap
                                textFormat: Text.PlainText
                            }
                        }
                    }

                    AppButton {
                        id: qwenWizardCopyRuntimeButton
                        objectName: "qwenWizardCopyRuntimeButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "copy"
                        text: root.copiedRuntime ? qsTr("Đã chép ✓") : qsTr("Sao chép")
                        onClicked: {
                            if (controller) {
                                controller.copyToClipboard(root.runtimeCommand);
                                root.copiedRuntime = true;
                                copyRuntimeTimer.restart();
                            }
                        }
                    }
                }

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Lưu ý: Chạy lệnh trong thư mục dự án hoặc kích hoạt .venv (source .venv/bin/activate) để tránh lỗi externally-managed-environment (PEP 668). Bản đóng gói độc lập không chứa PyTorch.")
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                    lineHeight: 1.25
                }
            }

            // Re-check: only meaningful while something is still missing.
            RowLayout {
                Layout.fillWidth: true
                visible: root.step === 2 && !root.packReady
                spacing: Theme.spacingSm

                AppButton {
                    id: qwenWizardRefreshButton
                    objectName: "qwenWizardRefreshButton"
                    variant: "quiet"
                    size: "sm"
                    text: qsTr("Đã chạy pip? Bấm để kiểm tra lại")
                    onClicked: {
                        controller.refreshQwenReadiness();
                        root.lastCheck = new Date().toLocaleTimeString();
                    }
                }

                Label {
                    id: qwenWizardLastCheckLabel
                    objectName: "qwenWizardLastCheckLabel"
                    Layout.fillWidth: true
                    visible: root.lastCheck !== ""
                    text: qsTr("Đã kiểm tra lại lúc %1").arg(root.lastCheck)
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignRight
            spacing: Theme.spacingSm
            AppButton { id: qwenWizardBackButton; objectName: "qwenWizardBackButton"; variant: "secondary"; size: "sm"; text: qsTr("Quay lại"); visible: root.step > 0; onClicked: root.step = root.step - 1 }
            AppButton { id: qwenWizardCloseButton; objectName: "qwenWizardCloseButton"; variant: "secondary"; size: "sm"; text: qsTr("Đóng"); visible: root.step === 2; onClicked: root.close() }
        }
        // Compatibility controls keep existing automation and keyboard paths stable.
        AppButton { id: qwenWizardNoButton; objectName: "qwenWizardNoButton"; visible: false; onClicked: root.close() }
        AppButton { id: qwenWizardYesButton; objectName: "qwenWizardYesButton"; visible: false; onClicked: root.step = Math.min(root.step + 1, 2) }
        AppButton { id: qwenWizardContinueButton; objectName: "qwenWizardContinueButton"; visible: false; onClicked: root.step = 2 }
        ScrollView { id: qwenWizardInstallScrollView; objectName: "qwenWizardInstallScrollView"; visible: false }
        AppButton { id: qwenWizardDownloadNextButton; objectName: "qwenWizardDownloadNextButton"; visible: false; onClicked: controller.downloadQwenModel("qwen_customvoice") }
    }
}
