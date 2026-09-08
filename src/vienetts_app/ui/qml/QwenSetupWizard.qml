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
// qwenWizardErrorLabel, qwenWizardFetchCommand.
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
    width: 520
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
    // 0 = use Qwen?, 1 = need cloning?, 2 = VRAM notice, 3 = action page.
    property int step: 0
    property bool needCloning: false

    // Fetch commands mirror core/qwen_models.wizard_fetch_command — QML
    // cannot import Python constants, keep the two in sync.
    readonly property string customVoiceRepo: "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    readonly property string baseRepo: "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    readonly property string runtimeCommand: "pip install \"vienetts-app[qwen]\""
    readonly property string fetchCommand: needCloning
        ? ("python scripts/fetch_qwen_models.py --repo \"" + customVoiceRepo + "\" --repo \"" + baseRepo + "\"")
        : ("python scripts/fetch_qwen_models.py --repo \"" + customVoiceRepo + "\"")

    onOpened: {
        root.step = 0;
        root.needCloning = false;
    }

    contentItem: ColumnLayout {
        spacing: Theme.spacingMd

        Label {
            id: qwenWizardStepLabel
            objectName: "qwenWizardStepLabel"
            Layout.fillWidth: true
            text: {
                if (root.step === 0)
                    return qsTr("Bước 1/4 — Dùng Qwen cho đa ngữ?");
                if (root.step === 1)
                    return qsTr("Bước 2/4 — Cần nhân bản giọng nói?");
                if (root.step === 2)
                    return qsTr("Bước 3/4 — Lưu ý phần cứng");
                return qsTr("Bước 4/4 — Cài đặt");
            }
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        // -- Step 0: use Qwen? --
        Label {
            Layout.fillWidth: true
            visible: root.step === 0
            text: qsTr("Qwen3-TTS đọc 10 ngôn ngữ (en, zh, ja, ko, de, fr, ru, es, it, pt) — không có tiếng Việt (giữ VieNeu cho tiếng Việt). Cần cài runtime và tải checkpoint (mỗi bản ~1–2 GB). Bạn có muốn dùng Qwen không?")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
            lineHeight: 1.3
        }

        // -- Step 1: need cloning? --
        Label {
            Layout.fillWidth: true
            visible: root.step === 1
            text: qsTr("CustomVoice đủ cho đọc đa ngữ (9 giọng cố định). Base thêm nhân bản từ clip mẫu 3–8 giây kèm bản ghi — và phải tải thêm một checkpoint. Bạn có cần nhân bản không?")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
            lineHeight: 1.3
        }

        // -- Step 2: hardware notice --
        Label {
            Layout.fillWidth: true
            visible: root.step === 2
            text: qsTr("Checkpoint 0.6B cần ~4 GB VRAM trên CUDA để chạy thoải mái; chạy CPU vẫn được nhưng chậm, không cam kết thời gian thực. Tiếp tục chứ?")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
            lineHeight: 1.3
        }

        // -- Step 3: action page --
        ColumnLayout {
            Layout.fillWidth: true
            visible: root.step === 3
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

            Label {
                Layout.fillWidth: true
                text: qsTr("Cách 1 — tải trong app (khuyên dùng khi có mạng):")
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
                    enabled: controller ? controller.qwenModelState !== "downloading" : false
                    onClicked: controller.downloadQwenModel("qwen_customvoice")
                }

                AppButton {
                    id: qwenWizardDownloadBaseButton
                    objectName: "qwenWizardDownloadBaseButton"
                    variant: "primary"
                    size: "sm"
                    text: qsTr("Tải Base")
                    visible: root.needCloning
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
                visible: controller
                    ? (controller.qwenModelState === "downloading" || controller.qwenModelState === "ready")
                    : false
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
                text: qsTr("Cách 2 — tự chạy lệnh (bản đóng gói, máy ngoại tuyến):")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
            }

            Label {
                Layout.fillWidth: true
                text: root.runtimeCommand
                color: Theme.text
                font.family: "monospace"
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }

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

        // -- Footer: Yes/No navigation --
        RowLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignRight
            spacing: Theme.spacingSm

            AppButton {
                id: qwenWizardBackButton
                objectName: "qwenWizardBackButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("Quay lại")
                visible: root.step === 2 || root.step === 3
                onClicked: root.step = root.step - 1
            }

            AppButton {
                id: qwenWizardNoButton
                objectName: "qwenWizardNoButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("Không")
                visible: root.step === 0 || root.step === 1
                onClicked: {
                    if (root.step === 0)
                        root.close();
                    else {
                        root.needCloning = false;
                        root.step = 2;
                    }
                }
            }

            AppButton {
                id: qwenWizardYesButton
                objectName: "qwenWizardYesButton"
                variant: "primary"
                size: "sm"
                text: qsTr("Có")
                visible: root.step === 0 || root.step === 1
                onClicked: {
                    if (root.step === 0)
                        root.step = 1;
                    else {
                        root.needCloning = true;
                        root.step = 2;
                    }
                }
            }

            AppButton {
                id: qwenWizardContinueButton
                objectName: "qwenWizardContinueButton"
                variant: "primary"
                size: "sm"
                text: qsTr("Tiếp tục")
                visible: root.step === 2
                onClicked: root.step = 3
            }

            AppButton {
                id: qwenWizardCloseButton
                objectName: "qwenWizardCloseButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("Đóng")
                visible: root.step === 3
                onClicked: root.close()
            }
        }
    }
}
