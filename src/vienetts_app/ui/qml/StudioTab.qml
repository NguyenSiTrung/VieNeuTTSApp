// Studio tab — pro-audio studio: offline polish + single-segment re-gen.
// Feeder tabs (Text / Paragraph / Audiobook) route their artifact here via
// controller.openInStudio / openChapterInStudio, then flip to this tab.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// studioTab, studioWaveform, studioOpStack, studioClipList,
// studioPreviewButton, studioExportButton, studioGainApply, studioRegenButton,
// studioOpenButton, studioRegenConfirmButton, studioResetButton.
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

    function formatTime(ms) {
        if (!ms || ms <= 0) return "0:00";
        const s = Math.max(0, Math.round(ms / 1000));
        const m = Math.floor(s / 60);
        return ("%1:%2").arg(m).arg(String(s % 60).padStart(2, "0"));
    }

    function syncControls() {
        const values = controller.studioControls || {};
        gainSlider.value = typeof values.gain === "number" ? values.gain : 0;
        fadeSlider.value = typeof values.fade === "number" ? values.fade : 200;
        speedSlider.value = typeof values.speed === "number" ? values.speed : 1.0;
        gapSlider.value = typeof values.gap === "number" ? values.gap : 500;
    }

    Connections {
        target: controller
        function onStudioControlsChanged() { root.syncControls(); }
    }

    Component.onCompleted: root.syncControls()

    readonly property int effectiveTotalMs: {
        if (controller.replayDurationMs > 0)
            return controller.replayDurationMs;
        if (typeof controller.studioDurationMs !== "undefined" && controller.studioDurationMs > 0)
            return controller.studioDurationMs;
        let sumMs = 0;
        const clips = controller.studioClips || [];
        for (let i = 0; i < clips.length; i++) {
            sumMs += Math.round((clips[i].duration || 0) * 1000);
        }
        return sumMs;
    }

    FileDialog {
        id: exportDialog

        fileMode: FileDialog.SaveFile
        title: qsTr("Xuất âm thanh")
        nameFilters: ["Âm thanh (*.wav *.mp3)", "WAV (*.wav)", "MP3 (*.mp3)"]
        defaultSuffix: controller.exportFormat
        onAccepted: controller.studioExport(root.exportPathForFilter(exportDialog.selectedFile, exportDialog.selectedNameFilter))
    }

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
            width: Math.min(560, root.width - Theme.spacingLg * 2)

            Label {
                text: qsTr("Tổng hợp lại đoạn âm thanh này bằng giọng đọc khác hoặc chỉnh sửa lại câu từ mà không ảnh hưởng đến các đoạn còn lại:")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        text: qsTr("Nội dung đoạn văn:")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                    }
                    Item { Layout.fillWidth: true }
                    Label {
                        text: qsTr("%1 ký tự").arg(regenTextArea.text.length)
                        color: Theme.textSubtle
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: Math.min(130, Math.max(72, regenTextArea.implicitHeight + Theme.spacingMd * 2))
                    color: Theme.surfaceAlt
                    border.color: regenTextArea.activeFocus ? Theme.borderFocus : Theme.borderSubtle
                    border.width: 1
                    radius: Theme.radiusMd

                    ScrollView {
                        anchors.fill: parent
                        anchors.margins: Theme.spacingSm
                        clip: true

                        TextArea {
                            id: regenTextArea
                            text: regenDialog.clipText !== "" ? regenDialog.clipText : regenDialog.clipLabel
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            wrapMode: Text.WordWrap
                            background: null
                            selectByMouse: true
                        }
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
                    id: regenConfirmBtn
                    objectName: "studioRegenConfirmButton"
                    variant: "primary"
                    text: qsTr("Tạo lại đoạn")
                    iconKind: "wave"
                    enabled: controller.hasStudioProject && !controller.busy
                    onClicked: {
                        controller.studioRegenClip(regenDialog.clipId, root.regenVoice(), regenTextArea.text);
                        regenDialog.close();
                    }
                }
            }
        }
    }

    PageShell {
        anchors.fill: parent
        maxWidth: 960

        // ── Studio Header ───────────────────────────────────────────────
        PageHeader {
            Layout.fillWidth: true
            iconKind: "studio"
            title: qsTr("Studio Âm thanh")
            subtitle: qsTr("Tinh chỉnh hiệu ứng hậu kỳ, sắp xếp các đoạn và hoàn thiện âm thanh trước khi xuất.")

            trailing: RowLayout {
                spacing: Theme.spacingSm
                visible: controller.hasStudioProject

                // Duration badge
                Rectangle {
                    radius: Theme.radiusPill
                    color: Theme.surfaceCard
                    border.color: Theme.borderSubtle
                    border.width: 1
                    implicitHeight: 28
                    implicitWidth: durationLabel.implicitWidth + Theme.spacingMd * 2

                    Label {
                        id: durationLabel
                        anchors.centerIn: parent
                        text: qsTr("Thời lượng: %1").arg(root.formatTime(root.effectiveTotalMs))
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                    }
                }

                // Clip count badge
                Rectangle {
                    radius: Theme.radiusPill
                    color: Theme.accentSubtle
                    implicitHeight: 28
                    implicitWidth: clipCountLabel.implicitWidth + Theme.spacingMd * 2

                    Label {
                        id: clipCountLabel
                        anchors.centerIn: parent
                        text: qsTr("%1 đoạn").arg(controller.studioClips ? controller.studioClips.length : 0)
                        color: Theme.accent
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                    }
                }
            }
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
                spacing: Theme.spacingLg

                // Has artifact ready to open
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd
                    visible: controller.hasArtifact

                    Rectangle {
                        Layout.fillWidth: true
                        radius: Theme.radiusMd
                        color: Theme.surfaceAlt
                        border.color: Theme.borderSubtle
                        border.width: 1
                        implicitHeight: readyRow.implicitHeight + Theme.spacingLg * 2

                        RowLayout {
                            id: readyRow
                            anchors.fill: parent
                            anchors.margins: Theme.spacingLg
                            spacing: Theme.spacingMd

                            AppIcon {
                                kind: "wave"
                                iconColor: Theme.accent
                                Layout.preferredWidth: 32
                                Layout.preferredHeight: 32
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingXxs

                                Label {
                                    text: qsTr("Âm thanh vừa tạo đã sẵn sàng để tinh chỉnh!")
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: Theme.fontWeightHeading
                                }

                                Label {
                                    text: qsTr("Bấm nút bên dưới để mở vào Studio và áp dụng các hiệu ứng khuếch đại, chuẩn hóa, điều chỉnh tốc độ, hoặc tạo lại từng câu.")
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSm
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }

                    AppButton {
                        id: openCurrentBtn
                        objectName: "studioOpenButton"
                        variant: "primary"
                        size: "lg"
                        iconKind: "studio"
                        text: qsTr("Mở âm thanh vừa tạo vào Studio")
                        enabled: controller.hasArtifact && !controller.busy
                        onClicked: controller.openInStudio("text", "")
                    }
                }

                // Zero artifact workflow guide
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd
                    visible: !controller.hasArtifact

                    Label {
                        Layout.fillWidth: true
                        text: qsTr("Chưa có âm thanh trong bộ nhớ đệm. Bạn có thể bắt đầu tạo âm thanh từ một trong các tab bên dưới, sau đó bấm nút Studio… để chuyển sang đây:")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.WordWrap
                    }

                    // 3 quick navigation cards
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        AppCard {
                            Layout.fillWidth: true
                            elevation: 0
                            cardColor: Theme.surfaceAlt
                            cardBorderColor: Theme.borderSubtle
                            cardRadius: Theme.radiusMd

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingSm

                                RowLayout {
                                    spacing: Theme.spacingSm
                                    AppIcon { kind: "text"; iconColor: Theme.accent }
                                    Label {
                                        text: qsTr("Tab Văn bản")
                                        color: Theme.text
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBase
                                        font.weight: Theme.fontWeightHeading
                                    }
                                }

                                Label {
                                    text: qsTr("Soạn thảo tự do, gán cảm xúc và tạo nhanh câu đơn.")
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }

                                AppButton {
                                    variant: "secondary"
                                    size: "sm"
                                    text: qsTr("Đến Tab Văn bản")
                                    onClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("text")
                                }
                            }
                        }

                        AppCard {
                            Layout.fillWidth: true
                            elevation: 0
                            cardColor: Theme.surfaceAlt
                            cardBorderColor: Theme.borderSubtle
                            cardRadius: Theme.radiusMd

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingSm

                                RowLayout {
                                    spacing: Theme.spacingSm
                                    AppIcon { kind: "paragraph"; iconColor: Theme.accent }
                                    Label {
                                        text: qsTr("Tab Đoạn văn")
                                        color: Theme.text
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBase
                                        font.weight: Theme.fontWeightHeading
                                    }
                                }

                                Label {
                                    text: qsTr("Nhập tệp tài liệu lớn, tự động chia đoạn và xếp hàng.")
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }

                                AppButton {
                                    variant: "secondary"
                                    size: "sm"
                                    text: qsTr("Đến Tab Đoạn văn")
                                    onClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("paragraph")
                                }
                            }
                        }

                        AppCard {
                            Layout.fillWidth: true
                            elevation: 0
                            cardColor: Theme.surfaceAlt
                            cardBorderColor: Theme.borderSubtle
                            cardRadius: Theme.radiusMd

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingSm

                                RowLayout {
                                    spacing: Theme.spacingSm
                                    AppIcon { kind: "audiobook"; iconColor: Theme.accent }
                                    Label {
                                        text: qsTr("Tab Sách nói")
                                        color: Theme.text
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBase
                                        font.weight: Theme.fontWeightHeading
                                    }
                                }

                                Label {
                                    text: qsTr("Nhập sách EPUB, tổng hợp từng chương và đồng bộ chữ.")
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }

                                AppButton {
                                    variant: "secondary"
                                    size: "sm"
                                    text: qsTr("Đến Tab Sách nói")
                                    onClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("audiobook")
                                }
                            }
                        }
                    }
                }
            }
        }

        // ── 1. Master Monitor & Waveform Deck ───────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Giám sát & Dạng sóng Master")
            subtitle: qsTr("Biểu đồ biên độ âm thanh thời gian thực và bàn điều khiển nghe thử, xuất tệp")
            badgeText: root.formatTime(root.effectiveTotalMs)
            visible: controller.hasStudioProject

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // Telemetry summary bar
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    // Status dot + label
                    RowLayout {
                        spacing: Theme.spacingXs
                        Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            color: controller.replayActive ? Theme.accent : Theme.success
                        }
                        Label {
                            text: controller.replayActive ? qsTr("Đang phát nghe thử") : qsTr("Sẵn sàng")
                            color: controller.replayActive ? Theme.accent : Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                        }
                    }

                    Label { text: "·"; color: Theme.textSubtle }

                    Label {
                        text: qsTr("Tần số: 48 kHz Master (32-bit float)")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }

                    Label { text: "·"; color: Theme.textSubtle }

                    Label {
                        text: qsTr("Định dạng xuất: %1").arg(controller.exportFormat.toUpperCase())
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }

                    Item { Layout.fillWidth: true }
                }

                // Waveform Overview (Taller, 72px)
                PlaybackWaveform {
                    id: studioWaveform

                    objectName: "studioWaveform"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 72
                    envelope: controller.studioEnvelope
                    position: controller.replayPosition
                    active: controller.replayActive
                    durationMs: (controller.replayDurationMs > 0 ? controller.replayDurationMs : (controller.studioDurationMs || root.effectiveTotalMs))
                    seekable: true
                    onSeekRequested: (fraction) => controller.seekReplay(fraction)
                }

                // Transport action row
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        id: previewBtn

                        objectName: "studioPreviewButton"
                        variant: controller.replayActive && !controller.replayPaused ? "primary" : "secondary"
                        size: "lg"
                        text: controller.replayActive ? (controller.replayPaused ? qsTr("Tiếp tục") : qsTr("Tạm dừng")) : qsTr("Nghe thử")
                        iconKind: controller.replayActive ? (controller.replayPaused ? "play" : "pause") : "play"
                        enabled: controller.hasStudioProject && controller.studioBusy !== true
                        busy: controller.studioBusyKind === "preview"
                        tooltipText: controller.replayActive ? (controller.replayPaused ? qsTr("Phát tiếp từ vị trí đã dừng") : qsTr("Tạm dừng, giữ nguyên vị trí")) : qsTr("Nghe thử toàn bộ dự án")
                        onClicked: {
                            if (!controller.replayActive)
                                controller.studioPreview();
                            else if (controller.replayPaused)
                                controller.resumeReplay();
                            else
                                controller.pauseReplay();
                        }
                    }

                    AppButton {
                        variant: "quiet"
                        size: "lg"
                        iconKind: "reset"
                        text: qsTr("Phát lại từ đầu")
                        tooltipText: qsTr("Dừng và phát lại từ đầu dự án")
                        enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                        onClicked: {
                            controller.stopReplay();
                            controller.studioPreview();
                        }
                    }

                    // Digital time display
                    Rectangle {
                        radius: Theme.radiusSm
                        color: Theme.surfaceAlt
                        border.color: Theme.borderSubtle
                        border.width: 1
                        implicitHeight: 36
                        implicitWidth: digitalTimeLabel.implicitWidth + Theme.spacingMd * 2

                        Label {
                            id: digitalTimeLabel
                            anchors.centerIn: parent
                            text: {
                                const currentMs = controller.replayActive ? Math.round(controller.replayPosition * root.effectiveTotalMs) : 0;
                                return root.formatTime(currentMs) + " / " + root.formatTime(root.effectiveTotalMs);
                            }
                            color: controller.replayActive ? Theme.accent : Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            font.weight: Theme.fontWeightMedium
                        }
                    }

                    Item {
                        Layout.fillWidth: true
                    }

                    AppButton {
                        id: studioExportBtn

                        objectName: "studioExportButton"
                        variant: "primary"
                        size: "lg"
                        text: qsTr("Xuất âm thanh")
                        iconKind: "download"
                        enabled: controller.hasStudioProject && controller.exporting !== true && controller.studioBusy !== true
                        busy: controller.exporting === true
                        onClicked: root.openExportDialog()
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

        // ── 2. Op Stack Timeline Card ───────────────────────────────────
        AppCard {
            id: opTimelineCard

            Layout.fillWidth: true
            title: qsTr("Lịch sử hiệu ứng (Op Stack)")
            subtitle: qsTr("Chuỗi thao tác xử lý âm thanh không phá hủy — hoàn tác từng bước hoặc đặt lại gốc")
            visible: controller.hasStudioProject

            badgeText: (controller.studioOps && controller.studioOps.length > 0)
                ? qsTr("%1 hiệu ứng").arg(controller.studioOps.length)
                : qsTr("Gốc (chưa chỉnh sửa)")
            badgeColor: (controller.studioOps && controller.studioOps.length > 0)
                ? Theme.accentSubtle
                : Theme.surfaceAlt
            badgeTextColor: (controller.studioOps && controller.studioOps.length > 0)
                ? Theme.accent
                : Theme.textMuted

            headerAction: RowLayout {
                spacing: Theme.spacingSm

                AppButton {
                    id: undoBtn
                    variant: "quiet"
                    size: "sm"
                    iconKind: "reset"
                    text: {
                        const ops = controller.studioOps || [];
                        if (ops.length > 0)
                            return qsTr("Hoàn tác (%1)").arg(ops[ops.length - 1].name || "");
                        return qsTr("Hoàn tác");
                    }
                    enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true && Boolean(controller.studioOps && controller.studioOps.length > 0)
                    onClicked: controller.studioUndo()
                }

                AppButton {
                    id: resetBtn
                    objectName: "studioResetButton"
                    variant: "quiet"
                    size: "sm"
                    iconKind: "refresh"
                    text: qsTr("Đặt lại gốc")
                    enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true && Boolean(controller.studioOps && controller.studioOps.length > 0)
                    onClicked: controller.studioReset()
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                // Interactive Op Stack Breadcrumbs Flow
                Flow {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    // Base Master Node
                    Rectangle {
                        implicitHeight: 28
                        implicitWidth: baseLabel.implicitWidth + Theme.spacingMd * 2
                        radius: Theme.radiusPill
                        color: Theme.surfaceAlt
                        border.color: Theme.borderSubtle
                        border.width: 1

                        RowLayout {
                            id: baseLabel
                            anchors.centerIn: parent
                            spacing: Theme.spacingXs
                            AppIcon { kind: "check"; iconColor: Theme.accent; Layout.preferredWidth: 14; Layout.preferredHeight: 14 }
                            Label {
                                text: qsTr("Bản gốc")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightMedium
                            }
                        }
                    }

                    // Repeated op chips
                    Repeater {
                        model: controller.studioOps || []

                        RowLayout {
                            spacing: Theme.spacingSm

                            required property var modelData
                            required property int index

                            Label {
                                text: "➔"
                                color: Theme.textSubtle
                                font.pixelSize: Theme.fontSizeXs
                            }

                            Rectangle {
                                implicitHeight: 28
                                implicitWidth: opChipContent.implicitWidth + Theme.spacingMd * 2
                                radius: Theme.radiusPill
                                color: Theme.accentSubtle
                                border.color: Theme.accent
                                border.width: 1

                                RowLayout {
                                    id: opChipContent
                                    anchors.centerIn: parent
                                    spacing: Theme.spacingXs

                                    Label {
                                        text: (index + 1) + "."
                                        color: Theme.accent
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                        font.weight: Theme.fontWeightBold
                                    }

                                    Label {
                                        text: modelData.desc || modelData.name || ""
                                        color: Theme.accent
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                        font.weight: Theme.fontWeightMedium
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // ── 3. Studio FX Rack (Grouped Tools) ───────────────────────────
        AppCard {
            id: opStackCard

            objectName: "studioOpStack"
            Layout.fillWidth: true
            title: qsTr("Tinh chỉnh âm thanh")
            subtitle: qsTr("Cung cấp các công cụ xử lý âm lượng, tốc độ, khoảng lặng và chuyển tiếp mờ dần")
            visible: controller.hasStudioProject

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingLg

                // Module 1: Dynamics & Level
                Rectangle {
                    Layout.fillWidth: true
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1
                    implicitHeight: dynCol.implicitHeight + Theme.spacingMd * 2

                    ColumnLayout {
                        id: dynCol
                        anchors.fill: parent
                        anchors.margins: Theme.spacingMd
                        spacing: Theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: qsTr("ÂM LƯỢNG & ĐỘNG LỰC HỌC (DYNAMICS)")
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightBold
                                font.letterSpacing: Theme.trackingWide
                            }
                            Item { Layout.fillWidth: true }
                        }

                        // Gain Row with Slider + Quick Presets + Apply
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Label {
                                Layout.preferredWidth: 140
                                text: qsTr("Khuếch đại: %1 dB").arg(gainSlider.value.toFixed(1))
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                            }

                            AppSlider {
                                id: gainSlider
                                objectName: "studioGainSlider"
                                Layout.fillWidth: true
                                from: -20
                                to: 12
                                stepSize: 0.5
                                value: 0
                                enabled: controller.hasStudioProject && !controller.busy
                                accessibleLabel: qsTr("Khuếch đại")
                            }

                            // Preset chips
                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "-3 dB"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: gainSlider.value = -3.0
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "0 dB"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: gainSlider.value = 0.0
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "+3 dB"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: gainSlider.value = 3.0
                            }

                            AppButton {
                                objectName: "studioGainApply"
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Áp dụng")
                                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                                busy: controller.studioBusyKind === "gain"
                                onClicked: controller.studioPushGain(gainSlider.value)
                            }
                        }

                        // Quick Normalize & Silence Trim actions
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            AppButton {
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Chuẩn hóa đỉnh (0 dBFS)")
                                tooltipText: qsTr("Đưa âm lượng đỉnh cao nhất về mức tối đa mà không gây rè âm")
                                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                                busy: controller.studioBusyKind === "normalize"
                                onClicked: controller.studioPushNormalize()
                            }

                            AppButton {
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Cắt khoảng lặng thừa")
                                tooltipText: qsTr("Tự động cắt bỏ các đoạn im lặng thừa ở đầu và cuối tệp (-50 dB)")
                                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                                busy: controller.studioBusyKind === "silence"
                                onClicked: controller.studioPushSilenceTrim()
                            }

                            Item { Layout.fillWidth: true }
                        }
                    }
                }

                // Module 2: Tempo & Cadence
                Rectangle {
                    Layout.fillWidth: true
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1
                    implicitHeight: tempoCol.implicitHeight + Theme.spacingMd * 2

                    ColumnLayout {
                        id: tempoCol
                        anchors.fill: parent
                        anchors.margins: Theme.spacingMd
                        spacing: Theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: qsTr("TỐC ĐỘ & NHỊP ĐIỆU (TEMPO & CADENCE)")
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightBold
                                font.letterSpacing: Theme.trackingWide
                            }
                            Item { Layout.fillWidth: true }
                        }

                        // Speed Slider + Presets
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Label {
                                Layout.preferredWidth: 140
                                text: qsTr("Tốc độ: %1×").arg(speedSlider.value.toFixed(2))
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                            }

                            AppSlider {
                                id: speedSlider
                                objectName: "studioSpeedSlider"
                                Layout.fillWidth: true
                                from: 0.5
                                to: 2.0
                                stepSize: 0.05
                                value: 1.0
                                enabled: controller.hasStudioProject && !controller.busy
                                accessibleLabel: qsTr("Tốc độ")
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "0.85×"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: speedSlider.value = 0.85
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "1.0×"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: speedSlider.value = 1.0
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "1.15×"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: speedSlider.value = 1.15
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "1.25×"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: speedSlider.value = 1.25
                            }

                            AppButton {
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Áp dụng")
                                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                                busy: controller.studioBusyKind === "speed"
                                onClicked: controller.studioPushSpeed(speedSlider.value)
                            }
                        }

                        // Gap Slider + Presets
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Label {
                                Layout.preferredWidth: 140
                                text: qsTr("Khoảng lặng: %1 ms").arg(gapSlider.value)
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                            }

                            AppSlider {
                                id: gapSlider
                                objectName: "studioGapSlider"
                                Layout.fillWidth: true
                                from: 0
                                to: 2000
                                stepSize: 100
                                value: 500
                                enabled: controller.hasStudioProject && !controller.busy
                                accessibleLabel: qsTr("Khoảng lặng giữa đoạn")
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "200 ms"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: gapSlider.value = 200
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "500 ms"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: gapSlider.value = 500
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "1000 ms"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: gapSlider.value = 1000
                            }

                            AppButton {
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Áp dụng")
                                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                                busy: controller.studioBusyKind === "gap"
                                onClicked: controller.studioPushGap(gapSlider.value)
                            }
                        }
                    }
                }

                // Module 3: Transitions & Fades
                Rectangle {
                    Layout.fillWidth: true
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1
                    implicitHeight: transCol.implicitHeight + Theme.spacingMd * 2

                    ColumnLayout {
                        id: transCol
                        anchors.fill: parent
                        anchors.margins: Theme.spacingMd
                        spacing: Theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: qsTr("CHUYỂN TIẾP & MỜ DẦN (FADES & TRANSITIONS)")
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightBold
                                font.letterSpacing: Theme.trackingWide
                            }
                            Item { Layout.fillWidth: true }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Label {
                                Layout.preferredWidth: 140
                                text: qsTr("Thời gian: %1 ms").arg(fadeSlider.value)
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                            }

                            AppSlider {
                                id: fadeSlider
                                objectName: "studioFadeSlider"
                                Layout.fillWidth: true
                                from: 0
                                to: 1000
                                stepSize: 50
                                value: 200
                                enabled: controller.hasStudioProject && !controller.busy
                                accessibleLabel: qsTr("Mờ dần")
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "50 ms"
                                tooltipText: qsTr("Khử tiếng click đầu/cuối")
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: fadeSlider.value = 50
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "200 ms"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: fadeSlider.value = 200
                            }

                            AppButton {
                                variant: "quiet"
                                size: "sm"
                                text: "500 ms"
                                enabled: controller.hasStudioProject && !controller.busy
                                onClicked: fadeSlider.value = 500
                            }

                            AppButton {
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Vào đầu")
                                tooltipText: qsTr("Áp dụng mờ dần vào đầu âm thanh")
                                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                                busy: controller.studioBusyKind === "fade"
                                onClicked: controller.studioPushFade("in", fadeSlider.value)
                            }

                            AppButton {
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Ra cuối")
                                tooltipText: qsTr("Áp dụng mờ dần ra cuối âm thanh")
                                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                                busy: controller.studioBusyKind === "fade"
                                onClicked: controller.studioPushFade("out", fadeSlider.value)
                            }
                        }
                    }
                }
            }
        }

        // ── 4. Segment Clip Studio Card ─────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Đoạn âm thanh")
            subtitle: qsTr("Quản lý danh sách các đoạn, nghe thử riêng lẻ, thay đổi thứ tự và tổng hợp lại câu từ")
            badgeText: qsTr("%1 phân đoạn").arg(controller.studioClips ? controller.studioClips.length : 0)
            visible: controller.hasStudioProject

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // Clip container with ScrollView for long documents
                ScrollView {
                    Layout.fillWidth: true
                    implicitHeight: Math.min(480, clipList.implicitHeight)
                    clip: true

                    ColumnLayout {
                        id: clipList

                        objectName: "studioClipList"
                        width: parent.width
                        spacing: Theme.spacingSm

                        Repeater {
                            model: controller.studioClips

                            Rectangle {
                                Layout.fillWidth: true
                                implicitHeight: rowLayout.implicitHeight + Theme.spacingMd * 2

                                required property var modelData
                                required property int index

                                readonly property bool isRegenerating: {
                                    return (typeof controller.studioRegenClipId !== "undefined")
                                        && controller.studioRegenClipId !== ""
                                        && controller.studioRegenClipId === modelData.id
                                }

                                color: isRegenerating ? Theme.accentSubtle : Theme.surfaceAlt
                                border.color: isRegenerating ? Theme.accent : Theme.borderSubtle
                                border.width: isRegenerating ? 1.5 : 1
                                radius: Theme.radiusMd

                                Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                                Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

                                RowLayout {
                                    id: rowLayout
                                    anchors.fill: parent
                                    anchors.margins: Theme.spacingMd
                                    spacing: Theme.spacingSm

                                    // Clip number badge
                                    Rectangle {
                                        Layout.preferredWidth: 32
                                        Layout.preferredHeight: 24
                                        radius: Theme.radiusSm
                                        color: isRegenerating ? Theme.accent : Theme.accentSubtle

                                        Label {
                                            anchors.centerIn: parent
                                            text: "#" + (index + 1)
                                            color: isRegenerating ? Theme.accentText : Theme.accent
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                            font.bold: true
                                        }
                                    }

                                    // Individual Clip Preview / Play Button
                                    AppButton {
                                        variant: "ghost"
                                        size: "sm"
                                        iconKind: "play"
                                        tooltipText: qsTr("Nghe thử riêng đoạn này")
                                        enabled: controller.hasStudioProject && !controller.busy
                                        onClicked: {
                                            if (typeof controller.studioPreviewClip === "function")
                                                controller.studioPreviewClip(modelData.id);
                                        }
                                    }

                                    // Duration pill
                                    Rectangle {
                                        Layout.preferredWidth: 48
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
                                        iconKind: "chevronUp"
                                        accessibleLabel: qsTr("Chuyển lên")
                                        tooltipText: qsTr("Chuyển đoạn này lên trước")
                                        visible: Boolean(controller.studioClips && controller.studioClips.length > 1)
                                        enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true && index > 0
                                        onClicked: controller.studioMoveClip(modelData.id, index - 1)
                                    }

                                    AppButton {
                                        variant: "quiet"
                                        size: "sm"
                                        iconKind: "chevronDown"
                                        accessibleLabel: qsTr("Chuyển xuống")
                                        tooltipText: qsTr("Chuyển đoạn này xuống sau")
                                        visible: Boolean(controller.studioClips && controller.studioClips.length > 1)
                                        enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true && index < controller.studioClips.length - 1
                                        onClicked: controller.studioMoveClip(modelData.id, index + 1)
                                    }

                                    // Regenerate Button
                                    AppButton {
                                        objectName: "studioRegenButton"
                                        variant: isRegenerating ? "primary" : "secondary"
                                        size: "sm"
                                        iconKind: isRegenerating ? "spinner" : "refresh"
                                        text: isRegenerating ? qsTr("Đang tạo lại…") : qsTr("Tạo lại…")
                                        busy: isRegenerating
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
                }

                // Single clip hint
                Label {
                    Layout.fillWidth: true
                    visible: Boolean(controller.studioClips && controller.studioClips.length === 1)
                    text: qsTr("Âm thanh hiện tại gồm 1 đoạn duy nhất. Bấm Tạo lại để thay đổi giọng đọc hoặc sửa lại văn bản cho đoạn này.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.WordWrap
                }
            }
        }
    }
}
