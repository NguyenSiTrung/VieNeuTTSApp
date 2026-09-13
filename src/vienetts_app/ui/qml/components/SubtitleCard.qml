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
// subtitleStatsLabel, subtitleStatusLabel, subtitleEmptyHint.
AppCard {
    id: root

    objectName: "subtitleCard"
    title: qsTr("Phụ đề (SRT)")
    subtitle: qsTr("Nhập tệp .srt, chọn cách khớp thời gian, rồi tạo âm thanh và phụ đề đã căn chỉnh.")

    readonly property bool available: typeof subtitleController !== "undefined" && subtitleController !== null
    readonly property bool loaded: available && subtitleController.loaded === true
    readonly property bool busy: available && subtitleController.rendering === true
    readonly property bool exporting: available && subtitleController.exporting === true
    readonly property bool hasTrack: available && subtitleController.rendered === true

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

    headerAction: AppButton {
        id: importBtn

        objectName: "subtitleImportButton"
        variant: "secondary"
        size: "sm"
        iconKind: "upload"
        text: qsTr("Nhập .srt…")
        enabled: root.available && !root.busy && !root.exporting
        onClicked: subtitleImportDialog.open()
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingMd

        // ── Empty state ────────────────────────────────────────────────────
        Label {
            objectName: "subtitleEmptyHint"
            Layout.fillWidth: true
            visible: !root.loaded
            text: qsTr("Chưa có phụ đề nào. Nhập một tệp .srt để bắt đầu.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.WordWrap
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
                onMoved: {
                    if (root.available)
                        subtitleController.rateCap = value;
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
                enabled: root.available && !root.busy && !root.exporting
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

        Connections {
            target: voicePicker

            function onSelectedVoiceChanged() {
                if (root.available && voicePicker.selectedVoice !== "")
                    subtitleController.voice = voicePicker.selectedVoice;
            }
        }

        // ── Cue list ───────────────────────────────────────────────────────
        ListView {
            id: cueList

            objectName: "subtitleCueList"
            Layout.fillWidth: true
            Layout.preferredHeight: 240
            visible: root.loaded
            clip: true
            model: root.available ? subtitleController.cues : []
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
                variant: subtitlePlayer.text === "playing" ? "primary" : "secondary"
                size: "lg"
                // No track yet: play() renders first — label says so.
                text: !root.hasTrack ? qsTr("Tạo và phát")
                    : subtitlePlayer.text === "playing" ? qsTr("Tạm dừng") : qsTr("Phát")
                iconKind: subtitlePlayer.text === "playing" ? "pause" : "play"
                visible: root.loaded
                enabled: root.available && (root.hasTrack || !root.busy) && !root.exporting
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
                id: exportWavBtn

                objectName: "subtitleExportWavButton"
                variant: "secondary"
                size: "lg"
                iconKind: "download"
                text: qsTr("Xuất WAV")
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
