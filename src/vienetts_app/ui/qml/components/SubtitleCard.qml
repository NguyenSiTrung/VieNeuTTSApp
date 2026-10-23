import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."
import "."

// SRT subtitle studio (Paragraph tab, "Phụ đề" mode): import a subtitle file,
// choose how the SRT clock and the voice share control, render a cue-aligned
// track, follow the cue list during playback, and export the WAV + retimed SRT.
//
// Reads the `subtitleController` context property (the name must NOT be
// `subtitle`: AppCard's own `subtitle` header property would shadow it).
// Everything here is a thin view over
// SubtitleController; the fit math lives in core/align.py and the streaming
// render in core/subtitle_project.py.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// subtitleCard, subtitleImportButton, subtitleImportDialog, subtitleModeCombo,
// subtitleRateSlider, subtitleOffsetField, subtitleMaxGapField,
// subtitleMergeToggle, subtitleVoicePicker, subtitleCueList, subtitleCueRow,
// subtitleRenderButton, subtitleCancelButton, subtitlePlayButton,
// subtitleExportWavButton, subtitleExportSrtButton, subtitleProgressBar,
// subtitleStatsLabel, subtitleStatusLabel, subtitleEmptyHint,
// subtitleLanguagePicker.
AppCard {
    id: root

    objectName: "subtitleCard"
    title: qsTr("Phụ đề (SRT)")
    subtitle: qsTr("Nhập tệp .srt, chọn cách khớp thời gian, rồi tạo âm thanh và phụ đề đã căn chỉnh.")

    readonly property bool available: typeof subtitleController !== "undefined" && subtitleController !== null
    readonly property bool loaded: available && subtitleController.loaded === true
    readonly property bool busy: available && subtitleController.rendering === true
    readonly property bool aiBusy: available && subtitleController.aiBusy === true
    readonly property bool exporting: available && subtitleController.exporting === true
    readonly property bool hasTrack: available && subtitleController.rendered === true
    readonly property bool hasVideo: available && subtitleController.hasVideo === true
    readonly property bool hasSpeakers: available && subtitleController.hasSpeakers === true
    // FR-2.5: the card is inset by the nav rail + page padding, so the card's
    // own width shrank non-monotonically when the rail switched between its
    // compact and full forms at 800 px. Track the window instead.
    readonly property bool wideLayout: (root.Window.window
        && root.Window.window.width >= 900) || root.width >= 900
    // FR-2.3: Source → Prepare → Produce. The card splits its body into these
    // mutually exclusive stages so a tall column never buries the next step.
    property string stage: "source"
    property Item pageShell: null
    readonly property string stageHint: {
        if (root.loaded)
            return "";
        return qsTr("Nhập tệp .srt hoặc video để mở 'Chuẩn bị' và 'Sản xuất'.");
    }
    readonly property var stageModel: [
        { value: "source", label: qsTr("Nguồn"), enabled: true, reason: "" },
        {
            value: "prepare",
            label: qsTr("Chuẩn bị"),
            enabled: root.loaded,
            reason: root.loaded ? "" : qsTr("Cần nhập tệp phụ đề hoặc video.")
        },
        {
            value: "produce",
            label: qsTr("Sản xuất"),
            enabled: root.loaded,
            reason: root.loaded ? "" : qsTr("Cần có phụ đề trước khi tạo âm thanh.")
        }
    ]

    // FR-3.2: translation target is chosen here, not inherited silently from
    // the synthesis tab. "" keeps the old fallback (synthesisLanguage).
    property string translateTargetLanguage: ""
    readonly property var translateLanguageOptions: {
        const rows = [{ code: "", label: qsTr("Theo ngôn ngữ tổng hợp") }];
        const langs = root.available ? (subtitleController.profileLanguages || []) : [];
        for (let i = 0; i < langs.length; ++i)
            rows.push({ code: langs[i].code, label: langs[i].label });
        return rows;
    }
    readonly property int translateLanguageIndex: {
        const rows = root.translateLanguageOptions;
        for (let i = 0; i < rows.length; ++i)
            if (rows[i].code === root.translateTargetLanguage)
                return i;
        return 0;
    }

    // FR-3.5: one reason per gated action, shown as the tooltip/accessible
    // description exactly when the action is disabled.
    function busyReason() {
        if (!root.available)
            return qsTr("Không có bộ điều khiển phụ đề.");
        if (root.exporting)
            return qsTr("Đang xuất tệp — chờ hoàn tất.");
        if (root.busy)
            return qsTr("Đang tạo âm thanh — chờ hoàn tất.");
        if (root.aiBusy)
            return qsTr("Đang chạy tác vụ AI — chờ hoàn tất.");
        return "";
    }

    function aiActionReason(kind) {
        if (!root.available)
            return qsTr("Không có bộ điều khiển phụ đề.");
        if (root.exporting)
            return qsTr("Đang xuất tệp — chờ hoàn tất.");
        if (root.busy)
            return qsTr("Đang tạo âm thanh — chờ hoàn tất.");
        if (root.aiBusy)
            return qsTr("Một tác vụ AI khác đang chạy.");
        if (!root.loaded)
            return qsTr("Cần nhập phụ đề trước.");
        if (kind === "diarize" && !root.hasVideo)
            return qsTr("Cần video để phân tách người nói.");
        if (kind === "asr" && !root.hasVideo)
            return qsTr("Cần đính kèm video để nhận dạng phụ đề.");
        return "";
    }

    function setStage(value) {
        if (value === root.stage)
            return;
        for (let i = 0; i < stageModel.length; ++i) {
            if (stageModel[i].value === value && !stageModel[i].enabled)
                return;
        }
        root.stage = value;
        if (root.pageShell && root.pageShell.scrollToTop)
            root.pageShell.scrollToTop();
    }

    readonly property string exportFormat: (typeof controller !== "undefined" && controller
        && controller.exportFormat === "mp3") ? "mp3" : "wav"

    Component.onCompleted: {
        if (root.loaded)
            root.stage = root.hasTrack ? "produce" : "prepare";
    }

    // QUrl → local path string for the controller (Windows file:///C:/… fix).
    function toLocalPath(url) {
        const s = url.toString();
        if (!s.startsWith("file://"))
            return s;
        let path = decodeURIComponent(s.substring(7));
        if (/^\/[A-Za-z]:\//.test(path))
            path = path.substring(1);
        return path;
    }

    function exportDir() {
        return (typeof controller !== "undefined" && controller && controller.outputDir)
            ? controller.outputDir : "";
    }

    // FR-2.4: one drop seam for the empty launcher. `.srt` imports cues;
    // anything else is treated as the video to transcribe. Returns whether a
    // URL was accepted so the smoke driver can assert the routing.
    function handleDroppedUrls(urls) {
        if (!root.available || !urls || urls.length === 0)
            return false;
        const path = root.toLocalPath(urls[0]);
        if (path === "")
            return false;
        if (/\.srt$/i.test(path))
            subtitleController.importSrt(path);
        else
            subtitleController.importVideo(path);
        return true;
    }

    FileDialog {
        id: subtitleImportDialog

        objectName: "subtitleImportDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp phụ đề")
        nameFilters: ["Phụ đề SubRip (*.srt)", "Tất cả tệp (*)"]
        onAccepted: {
            if (root.available)
                subtitleController.importSrt(root.toLocalPath(selectedFile));
        }
    }

    FileDialog {
        id: subtitleVideoDialog

        objectName: "subtitleVideoDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp video")
        nameFilters: ["Video (*.mp4 *.mkv *.mov *.webm *.avi *.m4v)", "Tất cả tệp (*)"]
        onAccepted: {
            if (root.available)
                subtitleController.importVideo(root.toLocalPath(selectedFile));
        }
    }

    // FR-1.1: every render-invalidating action (policy knob, cue edit, AI
    // re-run, re-ASR) is staged in the controller; this is the one gate that
    // turns it into an explicit, loss-naming decision.
    Dialog {
        id: invalidationDialog

        objectName: "subtitleInvalidationDialog"
        property bool confirming: false

        modal: true
        focus: true
        anchors.centerIn: Overlay.overlay
        width: Math.min((Overlay.overlay ? Overlay.overlay.width : 420) - Theme.spacingXl * 2, 440)
        padding: Theme.spacingLg
        closePolicy: Popup.CloseOnEscape

        header: Label {
            text: qsTr("Bỏ bản đã tạo?")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeMd
            font.weight: Theme.fontWeightHeading
            wrapMode: Text.Wrap
            leftPadding: Theme.spacingLg
            rightPadding: Theme.spacingLg
            topPadding: Theme.spacingLg
        }

        background: Rectangle {
            radius: Theme.radiusLg
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
        }

        contentItem: ColumnLayout {
            spacing: Theme.spacingMd

            Label {
                objectName: "subtitleInvalidationMessage"
                Layout.fillWidth: true
                text: root.available ? subtitleController.invalidationMessage : ""
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Item { Layout.fillWidth: true }

                AppButton {
                    objectName: "subtitleInvalidationCancelButton"
                    variant: "quiet"
                    text: qsTr("Giữ nguyên")
                    onClicked: {
                        invalidationDialog.close();
                        if (root.available)
                            subtitleController.rejectRenderInvalidation();
                    }
                }

                AppButton {
                    objectName: "subtitleInvalidationConfirmButton"
                    variant: "danger"
                    text: qsTr("Vẫn thay đổi")
                    onClicked: {
                        invalidationDialog.confirming = true;
                        invalidationDialog.close();
                        if (root.available)
                            subtitleController.confirmRenderInvalidation();
                    }
                }
            }
        }

        onClosed: {
            // Escape / outside click means "keep my render".
            if (!confirming && root.available && subtitleController.invalidationPending)
                subtitleController.rejectRenderInvalidation();
            confirming = false;
        }
    }

    headerAction: RowLayout {
        spacing: Theme.spacingXs

        AppButton {
            id: importVideoBtn

            objectName: "subtitleImportVideoButton"
            variant: "secondary"
            size: "sm"
            iconKind: "upload"
            text: qsTr("Nhập Video…")
            enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
            disabledReason: root.busyReason()
            onClicked: subtitleVideoDialog.open()
        }

        AppButton {
            id: importBtn

            objectName: "subtitleImportButton"
            variant: "secondary"
            size: "sm"
            iconKind: "upload"
            text: qsTr("Nhập .srt…")
            enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
            disabledReason: root.busyReason()
            onClicked: subtitleImportDialog.open()
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        AppSegmented {
            id: stageSwitch

            objectName: "subtitleStageSwitch"
            Layout.fillWidth: true
            visible: root.available
            accessibleLabel: qsTr("Giai đoạn làm việc")
            model: root.stageModel
            currentValue: root.stage
            onActivated: (value) => root.setStage(value)
        }

        Label {
            objectName: "subtitleStageHint"
            Layout.fillWidth: true
            visible: text !== ""
            text: root.stageHint
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            wrapMode: Text.WordWrap
        }

        // FR-3.6: actions that cannot run now say why in place instead of
        // returning silently (seek before render, play that renders first,
        // an AI action refused while another task runs).
        Label {
            objectName: "subtitleHintLabel"
            Layout.fillWidth: true
            visible: text !== ""
            text: root.available ? subtitleController.hintText : ""
            color: Theme.warningText
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            wrapMode: Text.WordWrap
        }

        // ── Empty state: two explicit entry choices (FR-2.4) ───────────────
        Rectangle {
            objectName: "subtitleEmptyDropZone"
            Layout.fillWidth: true
            visible: !root.loaded && root.stage === "source"
            implicitHeight: emptyLauncher.implicitHeight + Theme.spacingSm * 2
            color: "transparent"
            radius: Theme.radiusMd
            border.width: emptyDrop.containsDrag ? 2 : 0
            border.color: Theme.accent

            ColumnLayout {
                id: emptyLauncher

                objectName: "subtitleEmptyLauncher"
                anchors.fill: parent
                anchors.margins: Theme.spacingSm
                spacing: Theme.spacingSm

                Label {
                    objectName: "subtitleEmptyHint"
                    Layout.fillWidth: true
                    visible: !root.loaded && root.stage === "source"
                    text: root.hasVideo
                        ? qsTr("Đã chọn video '%1'. Nhấn 'Tạo phụ đề (ASR)' bên dưới hoặc nhập tệp .srt.").arg(
                            root.available ? subtitleController.videoName : "")
                        : qsTr("Chưa có phụ đề nào. Chọn một cách để bắt đầu.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    wrapMode: Text.WordWrap
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppCard {
                        objectName: "subtitleEmptyVideoCard"
                        width: Math.max(220, (emptyLauncher.width - Theme.spacingSm * 2) / 2)
                        title: qsTr("Video + tự động nhận dạng")
                        elevation: 0
                        clickable: true
                        onCardClicked: subtitleVideoDialog.open()

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("Cần mô hình Whisper trong Cài đặt › Mô hình AI. Hệ thống tự tạo phụ đề từ âm thanh.")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.WordWrap
                        }
                    }

                    AppCard {
                        objectName: "subtitleEmptySrtCard"
                        width: Math.max(220, (emptyLauncher.width - Theme.spacingSm * 2) / 2)
                        title: qsTr("Tệp phụ đề .srt")
                        elevation: 0
                        clickable: true
                        onCardClicked: subtitleImportDialog.open()

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("Cần tệp .srt có sẵn. Thời gian được giữ nguyên; giọng đọc dùng engine đang chọn.")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                Label {
                    objectName: "subtitleEmptyDropHint"
                    Layout.fillWidth: true
                    text: qsTr("Hoặc kéo thả tệp .srt / video vào đây.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.WordWrap
                }
            }

            DropArea {
                id: emptyDrop

                objectName: "subtitleEmptyDropArea"
                anchors.fill: parent
                enabled: !root.loaded && root.stage === "source"
                onDropped: (drop) => root.handleDroppedUrls(drop.urls)
            }
        }

        // ── Video attachment & ASR bar ──────────────────────────────────────
        Rectangle {
            id: videoBar

            objectName: "subtitleVideoBar"
            Layout.fillWidth: true
            visible: root.hasVideo && root.stage === "source"
            implicitHeight: videoBarLayout.implicitHeight + Theme.spacingSm * 2
            radius: Theme.radiusMd
            color: Theme.surfaceAlt
            border.width: 1
            border.color: Theme.borderSubtle

            ColumnLayout {
                id: videoBarLayout

                anchors.fill: parent
                anchors.margins: Theme.spacingSm
                spacing: Theme.spacingXs

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        objectName: "subtitleVideoLabel"
                        Layout.fillWidth: true
                        text: qsTr("Video: %1").arg(root.available ? subtitleController.videoName : "")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        font.weight: Theme.fontWeightMedium
                        elide: Text.ElideMiddle
                    }

                    AppToggle {
                        id: diarizeToggle

                        objectName: "subtitleDiarizeToggle"
                        text: qsTr("Tự tách người nói khi nhận dạng")
                        disabledReason: root.hasVideo
                            ? root.busyReason() : qsTr("Cần video để nhận dạng phụ đề.")
                        // Opt-in: diarization adds tens of seconds on CPU and,
                        // without the Nemotron GGUF installed, falls back to the
                        // acoustic heuristic. The warning now explains that.
                        checked: false
                        enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    }

                    AppButton {
                        objectName: "subtitleTranscribeButton"
                        variant: root.loaded ? "secondary" : "primary"
                        size: "sm"
                        iconKind: "wave"
                        text: qsTr("Tạo phụ đề (ASR)")
                        enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                        disabledReason: root.aiActionReason("asr")
                        onClicked: {
                            if (root.available)
                                subtitleController.transcribeVideo(diarizeToggle.checked);
                        }
                    }

                    AppIconButton {
                        objectName: "subtitleClearVideoButton"
                        size: "sm"
                        iconKind: "close"
                        tooltipText: qsTr("Bỏ đính kèm video")
                        accessibleLabel: tooltipText
                        enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                        disabledReason: root.busyReason()
                        onClicked: {
                            if (root.available)
                                subtitleController.clearVideo();
                        }
                    }
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm
                    visible: root.loaded

                    RowLayout {
                        spacing: Theme.spacingXs

                        Label {
                            text: qsTr("Âm thanh video:")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }

                        AppCombo {
                            objectName: "subtitleVideoMixCombo"
                            textRole: "label"
                            comboWidth: 210
                            enabled: root.available && !root.busy && !root.exporting
                            disabledReason: root.busyReason()
                            model: [
                                { label: qsTr("Thay thế âm gốc"), value: "replace" },
                                { label: qsTr("Giữ nhạc nền (Ducking)"), value: "duck" }
                            ]
                            currentIndex: root.available && subtitleController.videoMixMode === "duck" ? 1 : 0
                            onActivated: function (index) {
                                if (root.available)
                                    subtitleController.videoMixMode = model[index].value;
                            }
                        }
                    }

                    RowLayout {
                        spacing: Theme.spacingXs
                        visible: root.available && subtitleController.videoMixMode === "duck"

                        Label {
                            text: qsTr("Âm nền (dB):")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }

                        AppNumberField {
                            objectName: "subtitleVideoBgGainField"
                            enabled: root.available && !root.busy && !root.exporting
                            disabledReason: root.busyReason()
                            decimals: 0
                            scaleFactor: 1
                            from: -36
                            to: 0
                            stepSize: 2
                            value: root.available ? subtitleController.videoBgGainDb : -16
                            onValueModified: {
                                if (root.available)
                                    subtitleController.videoBgGainDb = value;
                            }
                        }
                    }

                    AppToggle {
                        objectName: "subtitleEmbedSrtToggle"
                        text: qsTr("Nhúng phụ đề vào video")
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason()
                        checked: root.available ? subtitleController.embedSubtitles : true
                        onToggled: {
                            if (root.available)
                                subtitleController.embedSubtitles = checked;
                        }
                    }

                    RowLayout {
                        spacing: Theme.spacingXs
                        visible: root.available && subtitleController.embedSubtitles

                        Label {
                            text: qsTr("Kiểu phụ đề:")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }

                        AppCombo {
                            objectName: "subtitleEmbedModeCombo"
                            textRole: "label"
                            comboWidth: 210
                            enabled: root.available && !root.busy && !root.exporting
                            disabledReason: root.busyReason()
                            model: [
                                { label: qsTr("Bật/tắt trong trình phát"), value: "soft" },
                                { label: qsTr("In cố định lên hình"), value: "burn" }
                            ]
                            currentIndex: root.available && subtitleController.subtitleEmbedMode === "burn" ? 1 : 0
                            onActivated: function (index) {
                                if (root.available)
                                    subtitleController.subtitleEmbedMode = model[index].value;
                            }
                        }
                    }
                }

                Label {
                    id: embedHint

                    // Burn-in re-encodes (slow); an AVI cannot carry a soft
                    // track, so its soft mode would export no subtitles at all.
                    readonly property bool burn: root.available && subtitleController.subtitleEmbedMode === "burn"
                    readonly property bool softUnsupported: root.available && !burn
                        && subtitleController.softSubtitlesSupported === false

                    objectName: "subtitleEmbedHint"
                    Layout.fillWidth: true
                    visible: root.loaded && subtitleController.embedSubtitles && (burn || softUnsupported)
                    text: softUnsupported
                        ? qsTr("Định dạng AVI không hỗ trợ phụ đề bật/tắt. Chọn \"In cố định lên hình\" để video có phụ đề.")
                        : qsTr("Phụ đề được vẽ thẳng vào khung hình nên luôn hiển thị, nhưng video phải mã hoá lại — xuất chậm hơn nhiều lần so với kiểu bật/tắt.")
                    color: softUnsupported ? Theme.warningText : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.WordWrap
                }

                Label {
                    id: overrunHint

                    // The exported file keeps every spoken word, so a dub that
                    // ran past the picture leaves a frozen tail. Say so up front.
                    objectName: "subtitleOverrunHint"
                    Layout.fillWidth: true
                    visible: root.loaded && root.hasVideo
                        && root.available && subtitleController.videoOverrunMs > 0
                    text: qsTr("Âm thanh lồng tiếng dài hơn video %1 giây — phần cuối video sẽ đứng hình để giữ đủ lời thoại.")
                        .arg(((root.available ? subtitleController.videoOverrunMs : 0) / 1000).toFixed(1))
                    color: Theme.warningText
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── AI refinement group: clean → translate → fit (FR-3.1/3.2) ─────
        ColumnLayout {
            id: aiBar

            objectName: "subtitleAiBar"
            Layout.fillWidth: true
            spacing: Theme.spacingXs
            visible: root.loaded && root.stage === "prepare"

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                SectionLabel {
                    objectName: "subtitleAiGroupLabel"
                    Layout.fillWidth: true
                    text: qsTr("CÔNG CỤ AI · THEO THỨ TỰ")
                }

                AppButton {
                    objectName: "subtitleCancelAiButton"
                    variant: "danger"
                    size: "sm"
                    text: qsTr("Hủy AI")
                    enabled: root.aiBusy
                    disabledReason: root.aiBusy ? "" : qsTr("Không có tác vụ AI nào đang chạy.")
                    onClicked: {
                        if (root.available)
                            subtitleController.cancelAiTask();
                    }
                }
            }

            Flow {
                Layout.fillWidth: true
                spacing: Theme.spacingXs

                AppButton {
                    objectName: "subtitleRefineAsrButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("AI Làm sạch phụ đề")
                    tooltipText: qsTr("Bước 1 · Sửa chính tả và nhãn người nói trên toàn bộ phụ đề.")
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    disabledReason: root.aiActionReason("llm")
                    onClicked: {
                        if (root.available)
                            subtitleController.refineWithLlm("refine_asr", "");
                    }
                }

                AppButton {
                    objectName: "subtitleTranslateDubButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("AI Dịch lồng tiếng")
                    tooltipText: qsTr("Bước 2 · Dịch phụ đề sang ngôn ngữ đích đã chọn.")
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    disabledReason: root.aiActionReason("llm")
                    onClicked: {
                        if (root.available)
                            subtitleController.refineWithLlm(
                                "translate_dub", root.translateTargetLanguage);
                    }
                }

                AppCombo {
                    objectName: "subtitleTranslateLanguageCombo"
                    comboWidth: 200
                    accessibleLabel: qsTr("Ngôn ngữ đích khi dịch")
                    textRole: "label"
                    model: root.translateLanguageOptions
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    currentIndex: root.translateLanguageIndex
                    onActivated: function (index) {
                        const rows = root.translateLanguageOptions;
                        if (index >= 0 && index < rows.length)
                            root.translateTargetLanguage = rows[index].code;
                    }
                }

                AppButton {
                    objectName: "subtitleFitTimingButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("AI Rút gọn khớp thời gian")
                    tooltipText: qsTr("Bước 3 · Rút ngắn câu cho vừa tốc độ đọc tối đa.")
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    disabledReason: root.aiActionReason("llm")
                    onClicked: {
                        if (root.available)
                            subtitleController.refineWithLlm("fit_timing", "");
                    }
                }
            }

            // FR-3.5: the AI prerequisites live in one named place with a
            // deep-link, instead of being discovered only when a run fails.
            RowLayout {
                objectName: "subtitleAiPrereqRow"
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    Layout.fillWidth: true
                    text: qsTr("ASR cần mô hình Whisper; AI cần máy chủ LLM tương thích OpenAI.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.WordWrap
                }

                AppButton {
                    objectName: "subtitleAiSettingsButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("Mở Cài đặt › Mô hình")
                    tooltipText: qsTr("Mở phần cài đặt mô hình AI và nhận dạng giọng nói")
                    onClicked: {
                        if (typeof bridge !== "undefined" && bridge !== null)
                            bridge.setCurrentTab("settings");
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            visible: root.aiBusy && root.stage === "prepare"
            spacing: Theme.spacingSm

            Label {
                objectName: "subtitleAiStageLabel"
                Layout.fillWidth: true
                text: root.available ? subtitleController.aiStageText : ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }

            ProgressBar {
                objectName: "subtitleAiProgressBar"
                Layout.preferredWidth: 160
                from: 0.0
                to: 1.0
                value: root.available ? subtitleController.aiProgress : 0.0
            }
        }

        // ── Main Workspace: 2 columns on wide windows (Cue list | Timing & Voice) ──
        GridLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.loaded && root.stage !== "source"
            columns: (root.wideLayout && root.stage === "produce") ? 2 : 1
            columnSpacing: Theme.spacingMd
            rowSpacing: Theme.spacingSm

            // ── Left column: Cue list ──────────────────────────────────────
            ListView {
                id: cueList

                objectName: "subtitleCueList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                // Never collapse below three rows (FR-2.5); the viewport
                // height above that flows straight into the list.
                Layout.minimumHeight: 180
                visible: root.loaded
                clip: true
                // Row-level model: rows keep their delegates across updates.
                model: root.available ? subtitleController.cueModel : null
                boundsBehavior: Flickable.StopAtBounds
                spacing: Theme.spacingXs

                ScrollBar.vertical: ScrollBar {
                    objectName: "subtitleCueScrollBar"
                    policy: ScrollBar.AsNeeded
                }

                delegate: Rectangle {
                    id: cueRowRoot

                    required property var modelData
                    required property int index
                    property bool editing: false

                    // The highlight follows subtitleController.activeCue — the
                    // row payload stays stable (no per-row `active` flag), so a
                    // cue transition never rebuilds the list.
                    readonly property bool active: root.available
                        && index === subtitleController.activeCue

                    objectName: "subtitleCueRow"
                    width: cueList.width - (cueList.ScrollBar.vertical && cueList.ScrollBar.vertical.visible ? 8 : 0)
                    implicitHeight: rowLayout.implicitHeight + 6
                    radius: Theme.radiusSm
                    color: active ? Theme.accentSubtle : "transparent"
                    border.width: 1
                    border.color: active ? Theme.accent : Theme.borderSubtle

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (root.available)
                                subtitleController.seekToCue(index);
                        }
                    }

                    ColumnLayout {
                        id: rowLayout

                        anchors.fill: parent
                        anchors.leftMargin: Theme.spacingSm + 2
                        anchors.rightMargin: Theme.spacingXs
                        anchors.topMargin: 3
                        anchors.bottomMargin: 3
                        spacing: Theme.spacingXs

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingXs

                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.alignment: Qt.AlignVCenter
                                spacing: 2

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.spacingXs

                                    Label {
                                        text: (modelData.index !== undefined ? modelData.index : (index + 1)) + "."
                                        color: Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                        font.weight: Theme.fontWeightMedium
                                    }

                                    Label {
                                        text: modelData.startLabel + " → " + modelData.endLabel
                                        color: active ? Theme.accent : Theme.textMuted
                                        font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                    }

                                    Rectangle {
                                        objectName: "subtitleCueSpeakerBadge"
                                        visible: Boolean(modelData.speaker)
                                        radius: Theme.radiusSm
                                        color: Theme.accentSubtle
                                        implicitWidth: cueSpkLabel.implicitWidth + Theme.spacingXs * 2
                                        implicitHeight: 18

                                        Label {
                                            id: cueSpkLabel

                                            anchors.centerIn: parent
                                            text: modelData.speaker || ""
                                            color: Theme.accent
                                            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                            font.weight: Theme.fontWeightMedium
                                        }
                                    }

                                    Rectangle {
                                        objectName: "subtitleCueFitBadge"
                                        visible: Boolean(
                                            (modelData.overflowMs && modelData.overflowMs > 0)
                                            || (modelData.fitRate && modelData.fitRate > 1.02)
                                            || modelData.overBudget
                                        )
                                        radius: Theme.radiusSm
                                        color: (modelData.overflowMs && modelData.overflowMs > 0) || modelData.overBudget
                                            ? Theme.surface
                                            : Theme.accentSubtle
                                        implicitWidth: cueFitLbl.implicitWidth + Theme.spacingXs * 2
                                        implicitHeight: 18

                                        Label {
                                            id: cueFitLbl

                                            anchors.centerIn: parent
                                            text: (modelData.overflowMs && modelData.overflowMs > 0)
                                                ? ("+" + modelData.overflowMs + " ms")
                                                : (modelData.fitRate && modelData.fitRate > 1.02)
                                                    ? (Number(modelData.fitRate).toFixed(2) + "×")
                                                    : qsTr("Dài")
                                            color: (modelData.overflowMs && modelData.overflowMs > 0) || modelData.overBudget
                                                ? Theme.warning
                                                : Theme.accent
                                            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                        }
                                    }

                                    Item { Layout.fillWidth: true }
                                }

                                Label {
                                    Layout.fillWidth: true
                                    visible: !cueRowRoot.editing
                                    text: modelData.text
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSm
                                    wrapMode: Text.WordWrap
                                }
                            }

                            AppIconButton {
                                objectName: "subtitleCueEditButton"
                                Layout.alignment: Qt.AlignVCenter
                                size: "sm"
                                iconKind: cueRowRoot.editing ? "close" : "edit"
                                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                                disabledReason: root.busyReason()
                                tooltipText: qsTr("Chỉnh sửa lời thoại phụ đề")
                                accessibleLabel: tooltipText
                                onClicked: {
                                    cueRowRoot.editing = !cueRowRoot.editing;
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: cueRowRoot.editing
                            spacing: Theme.spacingXs

                            TextField {
                                id: cueEditTextField

                                objectName: "subtitleCueEditField"
                                Layout.fillWidth: true
                                implicitHeight: Theme.controlHeightSm
                                text: modelData.text || ""
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                                leftPadding: Theme.spacingSm
                                rightPadding: Theme.spacingSm
                                background: Rectangle {
                                    radius: Theme.radiusSm
                                    color: Theme.surface
                                    border.width: 1
                                    border.color: parent.activeFocus ? Theme.accent : Theme.borderSubtle
                                }
                                onAccepted: {
                                    if (root.available && subtitleController.updateCue(
                                            index, cueEditTextField.text, cueEditSpkField.text))
                                        cueRowRoot.editing = false;
                                }
                            }

                            TextField {
                                id: cueEditSpkField

                                objectName: "subtitleCueEditSpeakerField"
                                Layout.preferredWidth: 110
                                implicitHeight: Theme.controlHeightSm
                                text: modelData.speaker || ""
                                placeholderText: qsTr("Người nói")
                                placeholderTextColor: Theme.textSubtle
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                leftPadding: Theme.spacingSm
                                rightPadding: Theme.spacingSm
                                background: Rectangle {
                                    radius: Theme.radiusSm
                                    color: Theme.surface
                                    border.width: 1
                                    border.color: parent.activeFocus ? Theme.accent : Theme.borderSubtle
                                }
                            }

                            AppButton {
                                objectName: "subtitleCueSaveEditButton"
                                variant: "primary"
                                size: "sm"
                                text: qsTr("Lưu")
                                onClicked: {
                                    if (root.available && subtitleController.updateCue(
                                            index, cueEditTextField.text, cueEditSpkField.text))
                                        cueRowRoot.editing = false;
                                }
                            }
                        }
                    }
                }

                // Follow the active cue during playback; activeCueChanged fires
                // only on transitions, so the user's scroll is never reset by
                // word-level updates and the model is never rebuilt.
                Connections {
                    target: root.available ? subtitleController : null

                    function onActiveCueChanged() {
                        if (root.available && subtitleController.activeCue >= 0)
                            cueList.positionViewAtIndex(
                                subtitleController.activeCue, ListView.Contain);
                    }
                }
            }

            // ── Right column: Fit policy & Voice ───────────────────────────
            Rectangle {
                objectName: "subtitleTimingPanel"
                Layout.fillWidth: !root.wideLayout
                Layout.preferredWidth: root.wideLayout ? 404 : -1
                Layout.maximumWidth: root.wideLayout ? 424 : -1
                Layout.alignment: Qt.AlignTop
                visible: root.stage === "produce"
                implicitHeight: timingVoiceCol.implicitHeight + Theme.spacingSm * 2
                radius: Theme.radiusMd
                color: Theme.surfaceAlt
                border.width: 1
                border.color: Theme.borderSubtle

                ColumnLayout {
                    id: timingVoiceCol

                    anchors.fill: parent
                    anchors.margins: Theme.spacingSm
                    spacing: Theme.spacingXs + 2
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        text: qsTr("Chế độ:")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        font.weight: Theme.fontWeightMedium
                    }

                    AppCombo {
                        id: modeCombo

                        objectName: "subtitleModeCombo"
                        Layout.fillWidth: true
                        textRole: "label"
                        comboWidth: 165
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason()
                        model: [
                            { label: qsTr("Lồng tiếng (theo SRT)"), value: "dub" },
                            { label: qsTr("Bản thoại tự nhiên"), value: "transcript" }
                        ]
                        currentIndex: root.available && subtitleController.mode === "transcript" ? 1 : 0
                        onActivated: function (index) {
                            if (root.available)
                                subtitleController.mode = model[index].value;
                        }
                    }

                    AppToggle {
                        id: mergeToggle

                        objectName: "subtitleMergeToggle"
                        text: qsTr("Gộp câu")
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason()
                        checked: root.available ? subtitleController.mergeSentences : false
                        onToggled: {
                            if (root.available)
                                subtitleController.mergeSentences = checked;
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        visible: root.available && subtitleController.mode === "dub"
                        text: qsTr("Nén tối đa:")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        font.weight: Theme.fontWeightMedium
                    }

                    AppSlider {
                        id: rateSlider

                        objectName: "subtitleRateSlider"
                        implicitWidth: 96
                        Layout.fillWidth: true
                        Layout.minimumWidth: 64
                        visible: root.available && subtitleController.mode === "dub"
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason()
                        from: 1.0
                        to: 2.0
                        stepSize: 0.05
                        value: root.available ? subtitleController.rateCap : 1.5
                        property bool keyStepping: false

                        Keys.onPressed: (event) => {
                            keyStepping = true;
                            event.accepted = false;
                        }
                        onMoved: {
                            if (keyStepping || !pressed)
                                rateApplyDebounce.restart();
                        }
                        onPressedChanged: {
                            if (!pressed && !keyStepping)
                                applyRate();
                        }

                        function applyRate() {
                            rateApplyDebounce.stop();
                            keyStepping = false;
                            if (root.available && Math.abs(subtitleController.rateCap - value) > 1e-6)
                                subtitleController.rateCap = value;
                        }

                        Timer {
                            id: rateApplyDebounce

                            interval: 300
                            onTriggered: rateSlider.applyRate()
                        }
                    }

                    Label {
                        visible: rateSlider.visible
                        text: root.available ? subtitleController.rateCap.toFixed(2) + "×" : ""
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                    }

                    Label {
                        visible: root.available && subtitleController.mode === "transcript"
                        text: qsTr("Giới hạn khoảng lặng (ms):")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        font.weight: Theme.fontWeightMedium
                    }

                    AppNumberField {
                        id: maxGapField

                        objectName: "subtitleMaxGapField"
                        implicitWidth: 84
                        Layout.preferredWidth: 84
                        visible: root.available && subtitleController.mode === "transcript"
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason()
                        decimals: 0
                        scaleFactor: 1
                        from: 0
                        to: 60000
                        stepSize: 100
                        value: root.available ? subtitleController.maxGapMs : 0
                        onValueModified: {
                            if (root.available)
                                subtitleController.maxGapMs = value;
                        }
                    }

                    Label {
                        text: qsTr("Lệch (ms):")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        font.weight: Theme.fontWeightMedium
                    }

                    AppNumberField {
                        id: offsetField

                        objectName: "subtitleOffsetField"
                        implicitWidth: 84
                        Layout.preferredWidth: 84
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason()
                        decimals: 0
                        scaleFactor: 1
                        from: -600000
                        to: 600000
                        stepSize: 100
                        value: root.available ? subtitleController.offsetMs : 0
                        onValueModified: {
                            if (root.available)
                                subtitleController.offsetMs = value;
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        text: qsTr("Giọng đọc:")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        font.weight: Theme.fontWeightMedium
                    }

                    VoicePicker {
                        id: voicePicker

                        objectName: "subtitleVoicePicker"
                        Layout.fillWidth: true
                        enabled: root.available && !root.busy && !root.exporting
                                 && EngineState.hasVoices
                    }
                }

                LanguagePicker {
                    objectName: "subtitleLanguagePicker"
                    Layout.fillWidth: true
                    visible: root.loaded && takesLanguage
                }

                Connections {
                    target: voicePicker

                    function onEffectiveVoiceChanged() {
                        if (root.available && voicePicker.effectiveVoice !== "")
                            subtitleController.voice = voicePicker.effectiveVoice;
                    }
                }

                Component.onCompleted: {
                    if (root.available && voicePicker.effectiveVoice !== "")
                        subtitleController.voice = voicePicker.effectiveVoice;
                }
                }
            }
        }

        // ── Speaker Cast Mapping Bar (full width so each speaker fits 1 row) ──
        Rectangle {
            id: speakerBar

            objectName: "subtitleSpeakerBar"
            Layout.fillWidth: true
            visible: root.loaded && root.hasSpeakers && root.stage !== "source"
            implicitHeight: speakerBarCol.implicitHeight + Theme.spacingSm + Theme.spacingXs
            radius: Theme.radiusMd
            color: Theme.surfaceAlt
            border.width: 1
            border.color: Theme.borderSubtle

            ColumnLayout {
                id: speakerBarCol

                anchors.fill: parent
                anchors.leftMargin: Theme.spacingSm + 2
                anchors.rightMargin: Theme.spacingSm + 2
                anchors.topMargin: Theme.spacingSm
                anchors.bottomMargin: Theme.spacingXs
                spacing: Theme.spacingXxs

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    Label {
                        Layout.fillWidth: true
                        text: qsTr("Phân vai giọng đọc theo người nói:")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                    }

                    AppButton {
                        objectName: "subtitleDiarizeButton"
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Phân tách lại người nói")
                        tooltipText: qsTr("Chạy lại phân tách trên bản phụ đề hiện tại.")
                        enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                            && root.hasVideo
                        disabledReason: root.aiActionReason("diarize")
                        onClicked: {
                            if (root.available)
                                subtitleController.runDiarization();
                        }
                    }
                }

                Repeater {
                    model: root.available ? subtitleController.speakers : []

                    delegate: Flow {
                        id: speakerRow

                        required property var modelData
                        required property int index

                        objectName: "subtitleSpeakerRow"
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        Rectangle {
                            radius: Theme.radiusSm
                            color: Theme.accentSubtle
                            implicitWidth: spkBadgeLbl.implicitWidth + Theme.spacingSm * 2
                            implicitHeight: Theme.controlHeightSm

                            Label {
                                id: spkBadgeLbl

                                anchors.centerIn: parent
                                text: modelData.label + " (" + modelData.cueCount + ")"
                                color: Theme.accent
                                font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightMedium
                            }
                        }

                        AppButton {
                            objectName: "subtitleSpeakerSampleButton"
                            variant: "secondary"
                            size: "sm"
                            visible: root.hasVideo
                            text: qsTr("Nghe mẫu")
                            enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                            disabledReason: root.busyReason()
                            onClicked: {
                                if (root.available)
                                    subtitleController.playSpeakerSample(modelData.id);
                            }
                        }

                        VoicePicker {
                            objectName: "subtitleSpeakerVoicePicker"
                            compact: true
                            fieldLabel: qsTr("Giọng cho %1").arg(modelData.label)
                            selectedVoice: modelData.voice || ""
                            enabled: root.available && !root.busy && !root.exporting
                            onVoiceActivated: (id) => {
                                if (root.available && id !== "")
                                    subtitleController.setSpeakerVoice(modelData.id, id);
                            }
                        }

                        AppButton {
                            objectName: "subtitleUseCurrentVoiceForSpeakerButton"
                            variant: "secondary"
                            size: "sm"
                            text: qsTr("Gán giọng đang chọn")
                            enabled: root.available && !root.busy && !root.exporting
                                && voicePicker.effectiveVoice !== ""
                            disabledReason: root.busyReason() !== ""
                                ? root.busyReason() : qsTr("Chọn một giọng đọc trước khi gán.")
                            onClicked: {
                                if (root.available)
                                    subtitleController.setSpeakerVoice(
                                        modelData.id, voicePicker.effectiveVoice);
                            }
                        }

                        AppButton {
                            objectName: "subtitleCloneSpeakerButton"
                            variant: "secondary"
                            size: "sm"
                            visible: root.hasVideo
                            text: qsTr("Lấy mẫu giọng từ video")
                            enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                                && EngineState.supportsCloning
                            disabledReason: root.aiActionReason("diarize") !== ""
                                ? root.aiActionReason("diarize")
                                : qsTr("Engine hiện tại không hỗ trợ nhân bản giọng.")
                            onClicked: {
                                if (root.available)
                                    subtitleController.cloneSpeakerFromVideo(modelData.id);
                            }
                        }
                    }
                }
            }
        }

        Label {
            objectName: "subtitleStatsLabel"
            Layout.fillWidth: true
            visible: root.loaded && root.stage === "produce" && text !== ""
            text: root.available ? subtitleController.statsSummary : ""
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            wrapMode: Text.WordWrap
        }

        Connections {
            target: root.available ? subtitleController : null

            function onLoadedChanged() {
                root.stage = root.loaded ? (root.hasTrack ? "produce" : "prepare") : "source";
            }

            function onRenderInvalidationRequested(reason, message) {
                if (root.available)
                    invalidationDialog.open();
            }

            function onInvalidationPendingChanged() {
                if (root.available && !subtitleController.invalidationPending
                        && invalidationDialog.visible)
                    invalidationDialog.close();
            }
        }
    }
}
