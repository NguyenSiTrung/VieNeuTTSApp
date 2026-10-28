import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."

// Source stage (FR-2.4): the two-choice launcher with drag-and-drop, the video
// attachment bar with ASR + audio/embed options, and the two file dialogs.
// Header import buttons and the card's public drop seam call into this panel.
ColumnLayout {
    id: root

    property bool available: false
    property bool loaded: false
    property string stageName: "source"
    property bool hasVideo: false
    property bool busy: false
    property bool aiBusy: false
    property bool exporting: false
    property string videoName: ""
    property string busyReason: ""
    property string asrReason: ""

    // Nothing in this panel exists outside the source stage, so the whole
    // column leaves the parent layout instead of holding on to a stale height.
    visible: !root.loaded || root.stageName === "source"
    spacing: Theme.spacingSm

    // A newly attached video re-checks ffmpeg (cheap path lookups), so a copy
    // installed outside the app since launch clears the missing hint.
    onHasVideoChanged: {
        if (root.hasVideo && typeof controller !== "undefined" && controller
                && typeof controller.refreshFfmpegRuntimeStatus === "function")
            controller.refreshFfmpegRuntimeStatus();
    }

    function toLocalPath(url) {
        const s = url.toString();
        if (!s.startsWith("file://"))
            return s;
        let path = decodeURIComponent(s.substring(7));
        if (/^\/[A-Za-z]:\//.test(path))
            path = path.substring(1);
        return path;
    }

    function openImportSrt() {
        subtitleImportDialog.open();
    }

    function openImportVideo() {
        subtitleVideoDialog.open();
    }

    // One drop seam: `.srt` imports cues, anything else is the video to
    // transcribe. Returns whether a URL was accepted.
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
        nameFilters: [qsTr("Phụ đề SubRip (*.srt)"), qsTr("Tất cả tệp (*)")]
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
        nameFilters: [
            qsTr("Video (*.mp4 *.mkv *.mov *.webm *.avi *.m4v)"),
            qsTr("Tất cả tệp (*)")
        ]
        onAccepted: {
            if (root.available)
                subtitleController.importVideo(root.toLocalPath(selectedFile));
        }
    }

    // ── Empty launcher ────────────────────────────────────────────────────
    Rectangle {
        objectName: "subtitleEmptyDropZone"
        Layout.fillWidth: true
        visible: !root.loaded && root.stageName === "source"
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
                visible: !root.loaded && root.stageName === "source"
                text: root.hasVideo
                    ? qsTr("Đã chọn video '%1'. Nhấn 'Tạo phụ đề (ASR)' bên dưới hoặc nhập tệp .srt.").arg(root.videoName)
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
            enabled: !root.loaded && root.stageName === "source"
            onDropped: (drop) => root.handleDroppedUrls(drop.urls)
        }
    }

    // ── Video attachment & ASR bar ────────────────────────────────────────
    AppCard {
        id: videoBar

        objectName: "subtitleVideoBar"
        Layout.fillWidth: true
        visible: root.hasVideo && root.stageName === "source"
        elevation: 0
        cardColor: Theme.surfaceAlt
        cardBorderColor: Theme.borderSubtle
        cardPadding: Theme.spacingSm

        ColumnLayout {
            id: videoBarLayout

            Layout.fillWidth: true
            spacing: Theme.spacingXs

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    objectName: "subtitleVideoLabel"
                    Layout.fillWidth: true
                    text: qsTr("Video: %1").arg(root.videoName)
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
                        ? root.busyReason : qsTr("Cần video để nhận dạng phụ đề.")
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
                    tooltipText: qsTr("Nhận dạng phụ đề từ video bằng mô hình Whisper")
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    disabledReason: root.asrReason
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
                    disabledReason: root.busyReason
                    onClicked: {
                        if (root.available)
                            subtitleController.clearVideo();
                    }
                }
            }

            // FR-7.2: ASR and video export both need ffmpeg + ffprobe. When
            // neither the managed pack nor a system copy is found, say how to
            // get one (the host's Homebrew/apt/winget note when no pack is
            // published) and offer Settings, where the install row lives.
            AppNotice {
                id: ffmpegHint

                readonly property var status: (typeof controller !== "undefined" && controller
                    && controller.ffmpegRuntimeStatus) ? controller.ffmpegRuntimeStatus : ({})

                objectName: "subtitleFfmpegMissingHint"
                Layout.fillWidth: true
                visible: root.hasVideo && ffmpegHint.status.available === false
                tone: "warning"
                title: qsTr("Chưa có ffmpeg")
                message: ffmpegHint.status.downloadable
                    ? qsTr("Cần ffmpeg để nhận dạng phụ đề và xuất video. Cài bản do ứng dụng quản lý trong Cài đặt › Mô hình.")
                    : qsTr("Cần ffmpeg để nhận dạng phụ đề và xuất video. %1")
                        .arg(ffmpegHint.status.note || qsTr("Cài ffmpeg rồi bấm \"Kiểm tra lại\" trong Cài đặt › Mô hình."))
                actionText: qsTr("Mở Cài đặt")
                onActionTriggered: {
                    if (typeof bridge !== "undefined" && bridge !== null)
                        bridge.setCurrentTab("settings");
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
                        accessibleLabel: qsTr("Âm thanh video")
                        textRole: "label"
                        comboWidth: 210
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason
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
                        accessibleLabel: qsTr("Âm nền (dB)")
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason
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
                    disabledReason: root.busyReason
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
                        accessibleLabel: qsTr("Kiểu phụ đề")
                        textRole: "label"
                        comboWidth: 210
                        enabled: root.available && !root.busy && !root.exporting
                        disabledReason: root.busyReason
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
}
