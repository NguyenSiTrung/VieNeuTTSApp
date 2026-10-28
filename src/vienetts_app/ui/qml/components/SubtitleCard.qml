import QtQuick
import QtQuick.Controls
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
    readonly property bool twoColWorkspace: root.wideLayout && root.loaded
        && (root.stage === "produce" || (root.stage === "prepare" && root.hasVideo))
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

    // In-app links to Cài đặt › <section>: Main.qml's openSettingsSection
    // when hosted in the app window, else just the Settings tab.
    function openSettingsSection(sectionId) {
        const appWindow = root.Window.window;
        if (appWindow && typeof appWindow.openSettingsSection === "function")
            appWindow.openSettingsSection(sectionId);
        else if (typeof bridge !== "undefined" && bridge !== null)
            bridge.setCurrentTab("settings");
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

    // FR-2.4: the source panel owns drop routing; this keeps the tab-level
    // seam that the smoke driver and future hosts call.
    function handleDroppedUrls(urls) {
        return sourcePanel.handleDroppedUrls(urls);
    }

    function exportDir() {
        return (typeof controller !== "undefined" && controller && controller.outputDir)
            ? controller.outputDir : "";
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
            onClicked: sourcePanel.openImportVideo()
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
            onClicked: sourcePanel.openImportSrt()
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

        SubtitleSourcePanel {
            id: sourcePanel

            available: root.available
            loaded: root.loaded
            stageName: root.stage
            hasVideo: root.hasVideo
            busy: root.busy
            aiBusy: root.aiBusy
            exporting: root.exporting
            videoName: root.available ? subtitleController.videoName : ""
            busyReason: root.busyReason()
            asrReason: root.aiActionReason("asr")
            asrSetupHint: (typeof controller !== "undefined" && controller
                && typeof controller.asrSetupHint === "string") ? controller.asrSetupHint : ""
            onSettingsRequested: (sectionId) => root.openSettingsSection(sectionId)
            Layout.fillWidth: true
        }

        SubtitlePreparePanel {
            shown: root.loaded && root.stage === "prepare"
            available: root.available
            busy: root.busy
            aiBusy: root.aiBusy
            exporting: root.exporting
            llmReason: root.aiActionReason("llm")
            targetLanguage: root.translateTargetLanguage
            onLanguageSelected: (code) => { root.translateTargetLanguage = code; }
            onSettingsRequested: (sectionId) => root.openSettingsSection(sectionId)
            Layout.fillWidth: true
        }

        // ── Main Workspace: 2 columns on wide windows (Cue list | Video + Timing & Voice) ──
        GridLayout {
            id: workspaceGrid

            Layout.fillWidth: true
            Layout.fillHeight: root.loaded && root.stage !== "source"
            visible: (root.loaded && root.stage !== "source")
                || (root.hasVideo && root.stage === "source")
            columns: root.twoColWorkspace ? 2 : 1
            columnSpacing: Theme.spacingMd
            rowSpacing: Theme.spacingSm

            // ── Video preview: below Source panel in Source stage; top of right
            //    column on wide Prepare/Produce; top of stack on narrow layouts ──
            SubtitleVideoPreview {
                id: videoPreview

                shown: root.hasVideo
                available: root.available
                hasVideo: root.hasVideo
                hasTrack: root.hasTrack
                loaded: root.loaded
                busy: root.busy
                aiBusy: root.aiBusy
                exporting: root.exporting
                busyReason: root.busyReason()
                stageName: root.stage
                compact: Boolean(root.Window.window && root.Window.window.height < 560)
                videoName: root.available ? subtitleController.videoName : ""
                videoPath: root.available ? subtitleController.videoPath : ""
                videoUrl: root.available ? subtitleController.videoUrl : ""
                dubPositionMs: root.available ? subtitleController.positionMs : 0
                dubDurationMs: root.available ? subtitleController.durationMs : 0
                Layout.row: 0
                Layout.column: root.twoColWorkspace ? 1 : 0
                Layout.fillWidth: !root.twoColWorkspace
                Layout.preferredWidth: root.twoColWorkspace ? 404 : -1
                Layout.maximumWidth: root.twoColWorkspace ? 424 : -1
                Layout.alignment: Qt.AlignTop
            }

            // ── Left column: Cue list ──────────────────────────────────────
            SubtitleCueEditor {
                shown: root.loaded && root.stage !== "source"
                available: root.available
                busy: root.busy
                aiBusy: root.aiBusy
                exporting: root.exporting
                disabledReason: root.busyReason()
                Layout.row: (root.twoColWorkspace || !root.hasVideo) ? 0 : 1
                Layout.column: 0
                Layout.rowSpan: root.twoColWorkspace ? (root.stage === "produce" ? 3 : 1) : 1
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 180
            }

            // ── Right column: Fit policy & Voice ───────────────────────────
            AppCard {
                objectName: "subtitleTimingPanel"
                Layout.row: root.twoColWorkspace ? (root.hasVideo ? 1 : 0) : 2
                Layout.column: root.twoColWorkspace ? 1 : 0
                Layout.fillWidth: !root.twoColWorkspace
                Layout.preferredWidth: root.twoColWorkspace ? 404 : -1
                Layout.maximumWidth: root.twoColWorkspace ? 424 : -1
                Layout.alignment: Qt.AlignTop
                visible: root.loaded && root.stage === "produce"
                elevation: 0
                cardColor: Theme.surfaceAlt
                cardBorderColor: Theme.borderSubtle
                cardPadding: Theme.spacingSm

                ColumnLayout {
                    id: timingVoiceCol

                    Layout.fillWidth: true
                    spacing: Theme.spacingXs + 2

                    SectionLabel {
                        objectName: "subtitleTimingSectionLabel"
                        Layout.fillWidth: true
                        text: qsTr("THỜI GIAN & GIỌNG ĐỌC")
                    }

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
                        accessibleLabel: qsTr("Chế độ phụ đề")
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
                        accessibleLabel: qsTr("Mức nén tối đa")
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
                        accessibleLabel: qsTr("Giới hạn khoảng lặng (ms)")
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
                        accessibleLabel: qsTr("Độ lệch thời gian (ms)")
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

            Item {
                visible: root.twoColWorkspace && root.stage === "produce"
                Layout.row: 2
                Layout.column: 1
                Layout.fillHeight: true
            }
        }

        SubtitleCastPanel {
            shown: root.loaded && root.hasSpeakers && root.stage !== "source"
            available: root.available
            busy: root.busy
            aiBusy: root.aiBusy
            exporting: root.exporting
            hasVideo: root.hasVideo
            busyReason: root.busyReason()
            diarizeReason: root.aiActionReason("diarize")
            currentVoice: voicePicker.effectiveVoice
            Layout.fillWidth: true
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
