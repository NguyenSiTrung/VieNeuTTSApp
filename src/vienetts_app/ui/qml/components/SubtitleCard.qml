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
    readonly property string exportFormat: (typeof controller !== "undefined" && controller
        && controller.exportFormat === "mp3") ? "mp3" : "wav"

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
            onClicked: subtitleImportDialog.open()
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingMd

        // ── Empty state ────────────────────────────────────────────────────
        Label {
            objectName: "subtitleEmptyHint"
            Layout.fillWidth: true
            visible: !root.loaded
            text: root.hasVideo
                ? qsTr("Đã chọn video '%1'. Nhấn 'Tạo phụ đề (ASR)' bên dưới hoặc nhập tệp .srt.").arg(
                    root.available ? subtitleController.videoName : "")
                : qsTr("Chưa có phụ đề nào. Nhập một tệp .srt hoặc Video để bắt đầu.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.WordWrap
        }

        // ── Video attachment & ASR bar ──────────────────────────────────────
        Rectangle {
            id: videoBar

            objectName: "subtitleVideoBar"
            Layout.fillWidth: true
            visible: root.hasVideo
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
                        text: qsTr("Phân tách người nói")
                        checked: true
                        enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    }

                    AppButton {
                        objectName: "subtitleTranscribeButton"
                        variant: root.loaded ? "secondary" : "primary"
                        size: "sm"
                        iconKind: "wave"
                        text: qsTr("Tạo phụ đề (ASR)")
                        enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
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
                        checked: root.available ? subtitleController.embedSubtitles : true
                        onToggled: {
                            if (root.available)
                                subtitleController.embedSubtitles = checked;
                        }
                    }
                }
            }
        }

        // ── AI Refinement & Diarization Toolbar ─────────────────────────────
        Flow {
            id: aiBar

            objectName: "subtitleAiBar"
            Layout.fillWidth: true
            spacing: Theme.spacingXs
            visible: root.loaded

            AppButton {
                objectName: "subtitleDiarizeButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("Phân tách người nói")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    && (root.hasVideo || root.hasTrack)
                onClicked: {
                    if (root.available)
                        subtitleController.runDiarization();
                }
            }

            AppButton {
                objectName: "subtitleRefineAsrButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("AI Làm sạch phụ đề")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                onClicked: {
                    if (root.available)
                        subtitleController.refineWithLlm("refine_asr", "");
                }
            }

            AppButton {
                objectName: "subtitleFitTimingButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("AI Rút gọn khớp thời gian")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                onClicked: {
                    if (root.available)
                        subtitleController.refineWithLlm("fit_timing", "");
                }
            }

            AppButton {
                objectName: "subtitleTranslateDubButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("AI Dịch lồng tiếng")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                onClicked: {
                    if (root.available)
                        subtitleController.refineWithLlm("translate_dub", "");
                }
            }

            AppButton {
                objectName: "subtitleCancelAiButton"
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy AI")
                visible: root.aiBusy
                onClicked: {
                    if (root.available)
                        subtitleController.cancelAiTask();
                }
            }
        }

        Label {
            objectName: "subtitleAiStageLabel"
            Layout.fillWidth: true
            visible: root.aiBusy
            text: root.available ? subtitleController.aiStageText : ""
            color: Theme.accent
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        // ── Speaker Cast Mapping Bar ────────────────────────────────────────
        Rectangle {
            id: speakerBar

            objectName: "subtitleSpeakerBar"
            Layout.fillWidth: true
            visible: root.loaded && root.hasSpeakers
            implicitHeight: speakerBarCol.implicitHeight + Theme.spacingSm * 2
            radius: Theme.radiusMd
            color: Theme.surfaceAlt
            border.width: 1
            border.color: Theme.borderSubtle

            ColumnLayout {
                id: speakerBarCol

                anchors.fill: parent
                anchors.margins: Theme.spacingSm
                spacing: Theme.spacingXs

                Label {
                    text: qsTr("Phân vai giọng đọc theo người nói:")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    font.weight: Theme.fontWeightMedium
                }

                Repeater {
                    model: root.available ? subtitleController.speakers : []

                    delegate: RowLayout {
                        required property var modelData
                        required property int index

                        objectName: "subtitleSpeakerRow"
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        Rectangle {
                            radius: Theme.radiusSm
                            color: Theme.accentSubtle
                            implicitWidth: spkBadgeLbl.implicitWidth + Theme.spacingSm * 2
                            implicitHeight: 24

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

                        TextField {
                            objectName: "subtitleSpeakerVoiceField"
                            Layout.fillWidth: true
                            implicitHeight: 32
                            leftPadding: Theme.spacingSm
                            rightPadding: Theme.spacingSm
                            text: modelData.voice || ""
                            placeholderText: qsTr("ID giọng đọc (để trống = giọng mặc định)")
                            placeholderTextColor: Theme.textSubtle
                            color: Theme.text
                            selectedTextColor: Theme.accentText
                            selectionColor: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            enabled: root.available && !root.busy && !root.exporting
                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.width: 1
                                border.color: parent.activeFocus ? Theme.accent : Theme.borderSubtle
                            }
                            onEditingFinished: {
                                if (root.available)
                                    subtitleController.setSpeakerVoice(modelData.id, text);
                            }
                        }

                        AppButton {
                            objectName: "subtitleUseCurrentVoiceForSpeakerButton"
                            variant: "secondary"
                            size: "sm"
                            text: qsTr("Gán giọng đang chọn")
                            enabled: root.available && !root.busy && !root.exporting
                                && voicePicker.effectiveVoice !== ""
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
                            onClicked: {
                                if (root.available)
                                    subtitleController.cloneSpeakerFromVideo(modelData.id);
                            }
                        }
                    }
                }
            }
        }

        // ── Fit policy ─────────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd
            visible: root.loaded

            Label {
                text: qsTr("Chế độ:")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
            }

            AppCombo {
                id: modeCombo

                objectName: "subtitleModeCombo"
                textRole: "label"
                comboWidth: 240
                enabled: root.available && !root.busy && !root.exporting
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

            Item { Layout.fillWidth: true }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd
            visible: root.loaded

            Label {
                visible: root.available && subtitleController.mode === "dub"
                text: qsTr("Nén tối đa:")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
            }

            AppSlider {
                id: rateSlider

                objectName: "subtitleRateSlider"
                Layout.preferredWidth: 200
                visible: root.available && subtitleController.mode === "dub"
                enabled: root.available && !root.busy && !root.exporting
                from: 1.0
                to: 2.0
                stepSize: 0.05
                value: root.available ? subtitleController.rateCap : 1.5
                // Each rateCap write rebuilds the dub plan (18–27 ms at 1,500
                // cues), so a drag writes once on release and keyboard steps
                // write once after a short pause — never per 0.05 step. Qt's
                // Slider also toggles `pressed` around every arrow-key step,
                // so key steps are flagged and left to the debounce.
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
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
            }

            AppNumberField {
                id: maxGapField

                objectName: "subtitleMaxGapField"
                visible: root.available && subtitleController.mode === "transcript"
                enabled: root.available && !root.busy && !root.exporting
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
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
            }

            AppNumberField {
                id: offsetField

                objectName: "subtitleOffsetField"
                enabled: root.available && !root.busy && !root.exporting
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
            spacing: Theme.spacingMd
            visible: root.loaded

            Label {
                text: qsTr("Giọng đọc:")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
            }

            VoicePicker {
                id: voicePicker

                objectName: "subtitleVoicePicker"
                Layout.fillWidth: true
                // An instance-level binding REPLACES VoicePicker's own
                // `enabled` (which carries the capability gate), so the
                // capability fact is re-read here from the same seam: a profile
                // with no voices to offer (Base before its first enrollment)
                // must read as unavailable on this surface too, exactly like
                // the text, paragraph and audiobook pickers.
                enabled: root.available && !root.busy && !root.exporting
                         && EngineState.hasVoices
            }

            AppToggle {
                id: mergeToggle

                objectName: "subtitleMergeToggle"
                text: qsTr("Gộp câu")
                enabled: root.available && !root.busy && !root.exporting
                checked: root.available ? subtitleController.mergeSentences : false
                onToggled: {
                    if (root.available)
                        subtitleController.mergeSentences = checked;
                }
            }
        }

        // Language row: the cue render snapshots the active profile's language,
        // so the control is the same capability-driven one the other surfaces
        // use.
        LanguagePicker {
            objectName: "subtitleLanguagePicker"
            Layout.fillWidth: true
            visible: root.loaded
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

        // ── Cue list ───────────────────────────────────────────────────────
        ListView {
            id: cueList

            objectName: "subtitleCueList"
            Layout.fillWidth: true
            Layout.preferredHeight: 240
            visible: root.loaded
            clip: true
            // Row-level model: rows keep their delegates across updates.
            model: root.available ? subtitleController.cueModel : null
            boundsBehavior: Flickable.StopAtBounds
            spacing: Theme.spacingXs

            delegate: Rectangle {
                required property var modelData
                required property int index

                // The highlight follows subtitleController.activeCue — the
                // row payload stays stable (no per-row `active` flag), so a
                // cue transition never rebuilds the list.
                readonly property bool active: root.available
                    && index === subtitleController.activeCue

                objectName: "subtitleCueRow"
                width: cueList.width
                implicitHeight: rowLayout.implicitHeight + Theme.spacingSm * 2
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

                RowLayout {
                    id: rowLayout

                    anchors.fill: parent
                    anchors.margins: Theme.spacingSm
                    spacing: Theme.spacingSm

                    Label {
                        text: (index + 1) + "."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        Layout.alignment: Qt.AlignTop
                    }

                    Label {
                        text: modelData.startLabel + " → " + modelData.endLabel
                        color: active ? Theme.accent : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        Layout.alignment: Qt.AlignTop
                    }

                    Rectangle {
                        objectName: "subtitleCueSpeakerBadge"
                        visible: Boolean(modelData.speaker)
                        radius: Theme.radiusSm
                        color: Theme.accentSubtle
                        implicitWidth: cueSpkLabel.implicitWidth + Theme.spacingXs * 2
                        implicitHeight: 18
                        Layout.alignment: Qt.AlignTop

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

                    Label {
                        Layout.fillWidth: true
                        text: modelData.text
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        wrapMode: Text.WordWrap
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

        // ── Progress + actions ─────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd
            visible: root.busy

            ProgressBar {
                objectName: "subtitleProgressBar"
                Layout.fillWidth: true
                from: 0
                to: 1
                value: root.available ? subtitleController.renderProgress : 0
                indeterminate: root.busy && subtitleController.renderProgress === 0
            }
        }

        Label {
            objectName: "subtitleStatsLabel"
            Layout.fillWidth: true
            visible: root.loaded && subtitleStats.text !== ""
            text: root.available ? subtitleController.statsSummary : ""
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            wrapMode: Text.WordWrap
        }

        Flow {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            AppButton {
                id: renderBtn

                objectName: "subtitleRenderButton"
                variant: "primary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Tạo âm thanh")
                visible: root.loaded
                enabled: root.loaded && !root.busy && !root.exporting
                         && EngineState.blockerReason === ""
                disabledReason: EngineState.blockerReason
                busy: root.busy
                onClicked: subtitleController.render()
            }

            AppButton {
                id: cancelBtn

                objectName: "subtitleCancelButton"
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy")
                visible: root.busy
                onClicked: subtitleController.cancelRender()
            }

            AppButton {
                id: playBtn

                objectName: "subtitlePlayButton"
                variant: "secondary"
                checked: subtitlePlayer.text === "playing"
                size: "lg"
                // No track yet: play() renders first — label says so.
                text: !root.hasTrack ? qsTr("Tạo và phát")
                    : subtitlePlayer.text === "playing" ? qsTr("Tạm dừng") : qsTr("Phát")
                iconKind: subtitlePlayer.text === "playing" ? "pause" : "play"
                visible: root.loaded
                // "Tạo và phát" renders first, so the profile gate applies to
                // that state too — playback of an existing track does not.
                enabled: root.available && (root.hasTrack || !root.busy) && !root.exporting
                         && (root.hasTrack || EngineState.blockerReason === "")
                disabledReason: root.hasTrack ? "" : EngineState.blockerReason
                onClicked: {
                    if (!root.available)
                        return;
                    if (subtitlePlayer.text === "playing")
                        subtitleController.pause();
                    else if (subtitlePlayer.text === "paused")
                        subtitleController.resume();
                    else
                        subtitleController.play();
                }
            }

            AppButton {
                id: exportVideoBtn

                objectName: "subtitleExportVideoButton"
                variant: "secondary"
                size: "lg"
                iconKind: "download"
                text: qsTr("Xuất Video")
                visible: root.loaded && root.hasVideo
                enabled: root.hasTrack && root.hasVideo && !root.busy && !root.aiBusy && !root.exporting
                onClicked: {
                    if (root.available)
                        subtitleController.exportVideo(root.exportDir());
                }
            }

            AppButton {
                id: exportWavBtn

                objectName: "subtitleExportWavButton"
                variant: "secondary"
                size: "lg"
                iconKind: "download"
                text: root.exportFormat === "mp3" ? qsTr("Xuất MP3") : qsTr("Xuất WAV")
                visible: root.loaded
                enabled: root.hasTrack && !root.busy && !root.exporting
                onClicked: {
                    if (root.available)
                        subtitleController.exportTrack(root.exportDir());
                }
            }

            AppButton {
                id: exportSrtBtn

                objectName: "subtitleExportSrtButton"
                variant: "secondary"
                size: "lg"
                iconKind: "download"
                text: qsTr("Xuất SRT")
                visible: root.loaded
                // Needs a rendered track: the controller refuses pre-render
                // export because the retimed cues only exist after a render.
                enabled: root.hasTrack && !root.busy && !root.exporting
                onClicked: {
                    if (root.available)
                        subtitleController.exportSrt(root.exportDir());
                }
            }

            AppButton {
                id: studioBtn

                objectName: "subtitleStudioButton"
                variant: "secondary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Mở trong Studio")
                visible: root.loaded
                enabled: root.hasTrack && !root.busy && !root.exporting
                onClicked: {
                    if (!root.available)
                        return;
                    const pid = typeof subtitleController.projectId !== "undefined"
                        ? subtitleController.projectId : "";
                    const cue = subtitleController.activeCue >= 0
                        ? subtitleController.activeCue : 0;
                    if (pid !== "" && controller.openSubtitleInStudio(pid, cue)) {
                        if (typeof bridge !== "undefined" && bridge !== null)
                            bridge.setCurrentTab("studio");
                    }
                }
            }
        }

        Label {
            objectName: "subtitleStatusLabel"
            Layout.fillWidth: true
            visible: text !== ""
            text: subtitleStatus.text
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            elide: Text.ElideMiddle
        }

        QtObject {
            id: subtitleStats

            readonly property string text: root.available ? subtitleController.statsSummary : ""
        }

        QtObject {
            id: subtitlePlayer

            readonly property string text: root.available ? subtitleController.playerState : "stopped"
        }

        QtObject {
            id: subtitleStatus

            property string text: ""

            function set(path, error) {
                if (error !== "")
                    text = qsTr("Xuất thất bại: %1").arg(error);
                else if (path !== "")
                    text = qsTr("Đã xuất: %1").arg(path);
            }
        }

        Connections {
            target: root.available ? subtitleController : null

            function onExportFinished(path, error) {
                subtitleStatus.set(path, error);
            }
        }
    }
}
