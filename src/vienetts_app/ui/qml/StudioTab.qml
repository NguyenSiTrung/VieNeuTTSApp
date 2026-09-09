// Studio tab — mini audio studio: offline polish + single-segment re-gen.
// Feeder tabs (Text / Paragraph / Audiobook) route their artifact here via
// controller.openInStudio / openChapterInStudio, then flip to this tab.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// studioTab, studioWaveform, studioOpStack, studioClipList,
// studioPreviewButton, studioExportButton, studioGainApply, studioRegenButton,
// studioOpenButton. Feeder buttons use "studioButton" scoped to each tab.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "components"

Pane {
    id: root

    objectName: "studioTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.bg
    }

    // QUrl → local path string (same shape as TextTab).
    function toLocalPath(url) {
        const s = url.toString();
        if (!s.startsWith("file://"))
            return s;
        let path = decodeURIComponent(s.substring(7));
        if (/^\/[A-Za-z]:\//.test(path))
            path = path.substring(1);
        return path;
    }

    function openExportDialog() {
        if (typeof controller !== "undefined" && controller && typeof controller.pathToUrl === "function") {
            const dir = controller.outputDir || "";
            if (dir !== "") {
                const folder = root.toFolderUrl(dir);
                if (folder !== "")
                    exportDialog.currentFolder = folder;
            }
        }
        exportDialog.open();
    }

    function toFolderUrl(path) {
        if (!path || path.trim() === "")
            return "";
        if (typeof controller !== "undefined" && controller && typeof controller.pathToUrl === "function") {
            const u = controller.pathToUrl(path);
            if (u !== "")
                return u;
        }
        return "";
    }

    function exportPathForFilter(url, filter) {
        const path = root.toLocalPath(url);
        const lower = path.toLowerCase();
        if (lower.endsWith(".wav") || lower.endsWith(".mp3"))
            return path;
        const f = String(filter || "");
        if (f.indexOf("*.wav") !== -1 && f.indexOf("*.mp3") === -1)
            return path + ".wav";
        if (f.indexOf("*.mp3") !== -1 && f.indexOf("*.wav") === -1)
            return path + ".mp3";
        return path;
    }

    function regenVoice() {
        if (regenVoicePicker.selectedVoice !== "")
            return regenVoicePicker.selectedVoice;
        return controller.defaultVoice;
    }

    FileDialog {
        id: exportDialog
    Dialog {
        id: regenDialog
        objectName: "studioRegenDialog"

        property string clipId: ""
        property string clipLabel: ""
        property string clipText: ""
        property string clipDuration: ""

        anchors.centerIn: parent
        modal: true
        title: qsTr("Tạo lại đoạn #%1").arg(clipLabel)

        background: Rectangle {
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
            radius: Theme.radiusLg
        }

        contentItem: ColumnLayout {
            spacing: Theme.spacingMd
            width: Math.min(520, root.width - Theme.spacingLg * 2)

            Label {
                text: qsTr("Tổng hợp lại đoạn âm thanh này bằng giọng đọc khác mà không ảnh hưởng đến phần còn lại:")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: Math.min(100, Math.max(48, excerptLabel.implicitHeight + Theme.spacingMd * 2))
                color: Theme.surfaceAlt
                border.color: Theme.borderSubtle
                border.width: 1
                radius: Theme.radiusMd

                ScrollView {
                    anchors.fill: parent
                    anchors.margins: Theme.spacingSm
                    clip: true

                    Label {
                        id: excerptLabel
                        width: parent.width
                        text: regenDialog.clipText !== "" ? regenDialog.clipText : regenDialog.clipLabel
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.WordWrap
                    }
                }
            }

            VoicePicker {
                id: regenVoicePicker
                Layout.fillWidth: true
                purpose: "regen"
                fieldLabel: qsTr("Giọng đọc mới")
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Item { Layout.fillWidth: true }

                AppButton {
                    variant: "quiet"
                    text: qsTr("Giữ nguyên")
                    onClicked: regenDialog.close()
                }

                AppButton {
                    objectName: "studioRegenConfirmButton"
                    variant: "primary"
                    text: qsTr("Tạo lại đoạn")
                    iconKind: "wave"
                    enabled: controller.hasStudioProject && !controller.busy
                    onClicked: {
                        controller.studioRegenClip(regenDialog.clipId, root.regenVoice());
                        regenDialog.close();
                    }
                }
            }
        }
    }


        fileMode: FileDialog.SaveFile
        title: qsTr("Xuất âm thanh")
        nameFilters: ["Âm thanh (*.wav *.mp3)", "WAV (*.wav)", "MP3 (*.mp3)"]
        defaultSuffix: controller.exportFormat
        onAccepted: controller.studioExport(root.exportPathForFilter(exportDialog.selectedFile, exportDialog.selectedNameFilter))
    }

    PageShell {
        anchors.fill: parent
        maxWidth: 960

        // ── Studio Header ───────────────────────────────────────────────
        PageHeader {
            Layout.fillWidth: true
            iconKind: "studio"
            title: qsTr("Studio Âm thanh")
            subtitle: qsTr("Tinh chỉnh hiệu ứng, sắp xếp các đoạn và hoàn thiện âm thanh trước khi xuất.")
        }

        // ── Empty State Card ────────────────────────────────────────────
        AppCard {
            id: emptyStateCard

            Layout.fillWidth: true
            title: qsTr("Dự án Studio")
            subtitle: qsTr("Chỉnh sửa và hoàn thiện âm thanh trước khi xuất tệp")
            visible: !controller.hasStudioProject

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                Label {
                    Layout.fillWidth: true
                    text: controller.hasArtifact
                        ? qsTr("Âm thanh vừa tạo đã sẵn sàng. Nhấn nút bên dưới để mở vào Studio và tinh chỉnh hiệu ứng, cắt ghép hoặc sắp xếp lại các đoạn.")
                        : qsTr("Chưa có âm thanh để chỉnh sửa. Hãy tạo âm thanh ở tab Văn bản, Đoạn văn hoặc Sách nói trước, sau đó nhấn nút Studio để mở tại đây.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.WordWrap
                }

                AppButton {
                    id: openCurrentBtn

                    objectName: "studioOpenButton"
                    variant: "primary"
                    size: "lg"
                    iconKind: "wave"
                    text: qsTr("Mở âm thanh vừa tạo")
                    visible: controller.hasArtifact
                    enabled: controller.hasArtifact && !controller.busy
                    onClicked: controller.openInStudio("text", "")
                }
            }
        }

        // ── 1. Waveform & Export Card ───────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Dạng sóng & Nghe thử")
            subtitle: qsTr("Biểu đồ biên độ âm thanh tổng hợp và thao tác nghe thử, xuất tệp")
            visible: controller.hasStudioProject

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                PlaybackWaveform {
                    id: studioWaveform

                    objectName: "studioWaveform"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 56
                    envelope: controller.studioEnvelope
                    position: controller.replayPosition
                    active: controller.replayActive
                    durationMs: controller.replayDurationMs
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        id: previewBtn

                        objectName: "studioPreviewButton"
                        variant: controller.replayActive ? "primary" : "secondary"
                        size: "lg"
                        text: controller.replayActive ? qsTr("Dừng") : qsTr("Nghe thử")
                        iconKind: controller.replayActive ? "stop" : "play"
                        enabled: controller.hasStudioProject
                        onClicked: {
                            if (controller.replayActive)
                                controller.stopReplay();
                            else
                                controller.studioPreview();
                        }
                    }

                    AppButton {
                        id: studioExportBtn

                        objectName: "studioExportButton"
                        variant: "primary"
                        size: "lg"
                        text: qsTr("Xuất âm thanh")
                        iconKind: "download"
                        enabled: controller.hasStudioProject && controller.exporting !== true
                        busy: controller.exporting === true
                        onClicked: root.openExportDialog()
                    }

                    Item {
                        Layout.fillWidth: true
                    }
                }

                Label {
                    Layout.fillWidth: true
                    visible: controller.errorText !== ""
                    text: controller.errorText
                    color: Theme.error
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── 2. Op Stack Card ────────────────────────────────────────────
        AppCard {
            id: opStackCard

            objectName: "studioOpStack"
            Layout.fillWidth: true
            title: qsTr("Tinh chỉnh âm thanh")
            subtitle: qsTr("Áp dụng hiệu ứng khuếch đại, mờ dần, tốc độ, khoảng lặng và chuẩn hóa")
            visible: controller.hasStudioProject

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        Layout.preferredWidth: 160
                        text: qsTr("Khuếch đại: %1 dB").arg(gainSlider.value.toFixed(1))
                        color: Theme.text
                    }

                    AppSlider {
                        id: gainSlider

                        Layout.fillWidth: true
                        from: -20
                        to: 12
                        stepSize: 0.5
                        value: 0
                        enabled: controller.hasStudioProject && !controller.busy
                        accessibleLabel: qsTr("Khuếch đại")
                    }

                    AppButton {
                        objectName: "studioGainApply"
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Áp dụng")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioPushGain(gainSlider.value)
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        Layout.preferredWidth: 160
                        text: qsTr("Mờ dần: %1 ms").arg(fadeSlider.value)
                        color: Theme.text
                    }

                    AppSlider {
                        id: fadeSlider

                        Layout.fillWidth: true
                        from: 0
                        to: 1000
                        stepSize: 50
                        value: 200
                        enabled: controller.hasStudioProject && !controller.busy
                        accessibleLabel: qsTr("Mờ dần")
                    }

                    AppButton {
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Vào")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioPushFade("in", fadeSlider.value)
                    }

                    AppButton {
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Ra")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioPushFade("out", fadeSlider.value)
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        Layout.preferredWidth: 160
                        text: qsTr("Tốc độ: %1×").arg(speedSlider.value.toFixed(2))
                        color: Theme.text
                    }

                    AppSlider {
                        id: speedSlider

                        Layout.fillWidth: true
                        from: 0.5
                        to: 2.0
                        stepSize: 0.05
                        value: 1.0
                        enabled: controller.hasStudioProject && !controller.busy
                        accessibleLabel: qsTr("Tốc độ")
                    }

                    AppButton {
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Áp dụng")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioPushSpeed(speedSlider.value)
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        Layout.preferredWidth: 160
                        text: qsTr("Khoảng lặng: %1 ms").arg(gapSlider.value)
                        color: Theme.text
                    }

                    AppSlider {
                        id: gapSlider

                        Layout.fillWidth: true
                        from: 0
                        to: 2000
                        stepSize: 100
                        value: 500
                        enabled: controller.hasStudioProject && !controller.busy
                        accessibleLabel: qsTr("Khoảng lặng giữa đoạn")
                    }

                    AppButton {
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Áp dụng")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioPushGap(gapSlider.value)
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Chuẩn hóa")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioPushNormalize()
                    }

                    AppButton {
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Cắt lặng")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioPushSilenceTrim()
                    }

                    AppButton {
                        variant: "quiet"
                        size: "sm"
                        text: qsTr("Hoàn tác")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: controller.studioUndo()
                    }

                    AppButton {
                        objectName: "studioResetButton"
                        variant: "quiet"
                        size: "sm"
                        iconKind: "refresh"
                        text: qsTr("Đặt lại mặc định")
                        enabled: controller.hasStudioProject && !controller.busy
                        onClicked: {
                            gainSlider.value = 0;
                            fadeSlider.value = 200;
                            speedSlider.value = 1.0;
                            gapSlider.value = 500;
                            controller.studioReset();
                        }
                    }
                }
            }
        }

        // ── 3. Clip List Card ───────────────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Đoạn âm thanh")
            subtitle: qsTr("Quản lý danh sách các đoạn, thay đổi thứ tự và tạo lại từng đoạn")
            visible: controller.hasStudioProject

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                ColumnLayout {
                    id: clipList

                    objectName: "studioClipList"
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Repeater {
                        model: controller.studioClips

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: rowLayout.implicitHeight + Theme.spacingMd * 2
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            radius: Theme.radiusMd

                            required property var modelData
                            required property int index

                            RowLayout {
                                id: rowLayout
                                anchors.fill: parent
                                anchors.margins: Theme.spacingMd
                                spacing: Theme.spacingMd

                                // Clip number badge
                                Rectangle {
                                    Layout.preferredWidth: 32
                                    Layout.preferredHeight: 24
                                    radius: Theme.radiusSm
                                    color: Theme.accentSubtle

                                    Label {
                                        anchors.centerIn: parent
                                        text: "#" + (index + 1)
                                        color: Theme.accent
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                        font.bold: true
                                    }
                                }

                                // Duration pill
                                Rectangle {
                                    Layout.preferredWidth: 46
                                    Layout.preferredHeight: 24
                                    radius: Theme.radiusSm
                                    color: Theme.surfaceCard
                                    border.color: Theme.borderSubtle
                                    border.width: 1
                                    visible: Boolean(modelData.duration_str)

                                    Label {
                                        anchors.centerIn: parent
                                        text: modelData.duration_str || ""
                                        color: Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                    }
                                }

                                // Text excerpt
                                Label {
                                    Layout.fillWidth: true
                                    text: modelData.text || modelData.label || ""
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSm
                                    elide: Text.ElideRight
                                    maximumLineCount: 2
                                    wrapMode: Text.Wrap
                                }

                                // Reorder actions (only visible when > 1 clip)
                                AppButton {
                                    variant: "quiet"
                                    size: "sm"
                                    text: qsTr("↑")
                                    accessibleLabel: qsTr("Chuyển lên")
                                    visible: Boolean(controller.studioClips && controller.studioClips.length > 1)
                                    enabled: controller.hasStudioProject && !controller.busy && index > 0
                                    onClicked: controller.studioMoveClip(modelData.id, index - 1)
                                }

                                AppButton {
                                    variant: "quiet"
                                    size: "sm"
                                    text: qsTr("↓")
                                    accessibleLabel: qsTr("Chuyển xuống")
                                    visible: Boolean(controller.studioClips && controller.studioClips.length > 1)
                                    enabled: controller.hasStudioProject && !controller.busy && index < controller.studioClips.length - 1
                                    onClicked: controller.studioMoveClip(modelData.id, index + 1)
                                }

                                AppButton {
                                    objectName: "studioRegenButton"
                                    variant: "secondary"
                                    size: "sm"
                                    text: qsTr("↻ Tạo lại")
                                    enabled: controller.hasStudioProject && !controller.busy
                                    onClicked: {
                                        regenDialog.clipId = modelData.id;
                                        regenDialog.clipLabel = String(index + 1);
                                        regenDialog.clipText = modelData.text || modelData.label || "";
                                        regenDialog.clipDuration = modelData.duration_str || "";
                                        regenDialog.open();
                                    }
                                }
                            }
                        }
                    }
                }

                // Single clip hint
                Label {
                    Layout.fillWidth: true
                    visible: Boolean(controller.studioClips && controller.studioClips.length === 1)
                    text: qsTr("Âm thanh hiện tại gồm 1 đoạn duy nhất. Bấm Tạo lại để thay đổi giọng đọc cho đoạn này.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.WordWrap
                }
            }
        }
    }
}
