import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia
import ".."
import "."

// Dual-mode video preview panel for the "Lồng tiếng & Phụ đề" destination
// (FR-2.1–FR-2.4, bead VieNeuTTSApp-t80a):
//  - Source Mode ("source"): plays/scrubs the attached video with its original
//    audio track and syncs the active subtitle cue from video timestamps.
//  - Dub Sync Mode ("dub"): activates automatically when dubbed audio plays,
//    muting source video audio and syncing video frames to the dub clock.
//  - Live caption pill overlay + toggle, collapsible 16:9 viewport (auto-
//    collapsing on compact windows outside the Source stage), and graceful
//    inline fallback notice when a codec/backend cannot decode frames.
AppCard {
    id: root

    objectName: "subtitleVideoPreview"
    visible: root.shown && root.hasVideo
    elevation: 0
    cardColor: Theme.surfaceAlt
    cardBorderColor: Theme.borderSubtle
    cardPadding: 6

    property bool shown: true
    property bool available: typeof subtitleController !== "undefined"
        && subtitleController !== null
    readonly property var ctrl: root.available ? subtitleController : null
    property bool hasVideo: root.ctrl ? Boolean(subtitleController.hasVideo) : false
    property bool hasTrack: root.ctrl ? Boolean(subtitleController.rendered) : false
    property bool loaded: root.ctrl ? Boolean(subtitleController.loaded) : false
    property bool busy: root.ctrl ? Boolean(subtitleController.rendering) : false
    property bool aiBusy: root.ctrl ? Boolean(subtitleController.aiBusy) : false
    property bool exporting: root.ctrl ? Boolean(subtitleController.exporting) : false
    property string busyReason: ""
    property string stageName: "source"
    property bool compact: false
    property string videoName: root.available ? String(subtitleController.videoName || "") : ""
    property string videoPath: root.available ? String(subtitleController.videoPath || "") : ""
    property string videoUrl: root.available ? String(subtitleController.videoUrl || "") : ""
    property int dubPositionMs: root.available ? Number(subtitleController.positionMs || 0) : 0
    property int dubDurationMs: root.available ? Number(subtitleController.durationMs || 0) : 0
    readonly property string dubPlayerState: root.available
        ? String(subtitleController.playerState || "stopped") : "stopped"

    property string preferredAudioMode: "source"
    readonly property string audioMode: (root.dubPlayerState === "playing"
        || (root.preferredAudioMode === "dub" && root.hasTrack)) ? "dub" : "source"
    readonly property bool audioMuted: root.audioMode === "dub"

    property bool captionsEnabled: true
    property bool userCollapseOverride: false
    property bool collapsed: false
    property bool forceFallbackError: false
    readonly property bool hasDecodeError: root.forceFallbackError
        || mediaPlayer.error !== MediaPlayer.NoError

    property int _lastRequestedPositionMs: 0
    property int _cueRevision: 0
    property bool _released: false
    property bool _audioOutputRequested: false

    readonly property string mediaSourceString: mediaPlayer.source.toString()
    readonly property bool isPlaying: root.audioMode === "dub"
        ? root.dubPlayerState === "playing"
        : mediaPlayer.playbackState === MediaPlayer.PlayingState

    readonly property int _inferredCuesDurationMs: {
        const _rev = root._cueRevision;
        if (!root.available)
            return 0;
        const m = subtitleController.cueModel;
        if (!m)
            return 0;
        if (typeof m.get === "function") {
            const c = m.count !== undefined ? m.count : 0;
            if (c > 0) {
                const last = m.get(c - 1);
                return Number(last && last.endMs ? last.endMs : 0);
            }
            return 0;
        }
        if (m.length !== undefined && m.length > 0) {
            const last = m[m.length - 1];
            return Number(last && last.endMs ? last.endMs : 0);
        }
        return 0;
    }

    readonly property int effectiveDurationMs: mediaPlayer.duration > 0
        ? mediaPlayer.duration
        : (root.dubDurationMs > 0 ? root.dubDurationMs : root._inferredCuesDurationMs)

    readonly property int displayPositionMs: (root.audioMode === "dub"
        && root.dubPlayerState === "playing")
        ? root.dubPositionMs
        : (mediaPlayer.duration > 0 && mediaPlayer.position > 0
            ? mediaPlayer.position : root._lastRequestedPositionMs)

    function syncAutoCollapse() {
        if (!root.userCollapseOverride)
            root.collapsed = root.compact && root.stageName !== "source";
    }

    onCompactChanged: syncAutoCollapse()
    onStageNameChanged: syncAutoCollapse()
    onVideoUrlChanged: syncSourceFromUrl()
    onHasVideoChanged: syncSourceFromUrl()

    onDubPositionMsChanged: {
        if (root.audioMode === "dub" && root.dubPlayerState === "playing") {
            root._lastRequestedPositionMs = root.dubPositionMs;
            if (mediaPlayer.source.toString() !== ""
                    && Math.abs(mediaPlayer.position - root.dubPositionMs) > 120) {
                mediaPlayer.position = root.dubPositionMs;
            }
        }
    }

    Component.onCompleted: {
        syncAutoCollapse();
        syncSourceFromUrl();
    }

    function cueAt(idx) {
        if (!root.available || idx < 0)
            return null;
        const m = subtitleController.cueModel;
        if (!m)
            return null;
        if (typeof m.get === "function") {
            const count = m.count !== undefined ? m.count : 0;
            if (idx < count)
                return m.get(idx);
            return null;
        }
        if (m.length !== undefined && idx < m.length)
            return m[idx];
        return null;
    }

    // FR-6.2: one bisect in the controller per position tick, then at most one
    // row read — never a scan of every cue row.
    function cueAtTime(ms) {
        if (!root.available || typeof subtitleController.cueIndexAt !== "function")
            return null;
        return root.cueAt(subtitleController.cueIndexAt(Math.max(0, Math.round(Number(ms || 0)))));
    }

    readonly property var activeCueRow: {
        const _rev = root._cueRevision;
        if (!root.available)
            return null;
        const idx = subtitleController.activeCue;
        if (idx >= 0) {
            const direct = root.cueAt(idx);
            if (direct)
                return direct;
        }
        return root.cueAtTime(root.displayPositionMs);
    }

    readonly property string activeCaptionText: root.activeCueRow && root.activeCueRow.text
        ? String(root.activeCueRow.text) : ""
    readonly property string activeCaptionSpeaker: root.activeCueRow && root.activeCueRow.speaker
        ? String(root.activeCueRow.speaker) : ""

    function formatTimecode(ms) {
        const totalSec = Math.max(0, Math.floor(Number(ms || 0) / 1000));
        const mins = Math.floor(totalSec / 60);
        const secs = totalSec % 60;
        const mm = mins < 10 ? "0" + mins : String(mins);
        const ss = secs < 10 ? "0" + secs : String(secs);
        return mm + ":" + ss;
    }

    function seekVideoTo(ms) {
        const target = Math.max(0, Math.round(Number(ms || 0)));
        root._lastRequestedPositionMs = target;
        if (mediaPlayer.source.toString() !== "")
            mediaPlayer.position = target;
    }

    function releaseVideo() {
        root._released = true;
        mediaPlayer.stop();
        mediaPlayer.source = "";
    }

    function syncSourceFromUrl() {
        if (!root.hasVideo || root.videoUrl === "") {
            mediaPlayer.stop();
            mediaPlayer.source = "";
            return;
        }
        root._released = false;
        if (mediaPlayer.source.toString() !== root.videoUrl)
            mediaPlayer.source = root.videoUrl;
    }

    Loader {
        id: audioOutputLoader

        active: root._audioOutputRequested && !root.audioMuted
        sourceComponent: AudioOutput {
            muted: root.audioMuted
        }
    }

    MediaPlayer {
        id: mediaPlayer

        audioOutput: audioOutputLoader.item
        videoOutput: videoOutput

        onMediaStatusChanged: {
            if (mediaPlayer.mediaStatus === MediaPlayer.LoadedMedia
                    || mediaPlayer.mediaStatus === MediaPlayer.BufferedMedia) {
                if (root._lastRequestedPositionMs > 0
                        && Math.abs(mediaPlayer.position - root._lastRequestedPositionMs) > 50) {
                    mediaPlayer.position = root._lastRequestedPositionMs;
                }
            }
        }

        onPositionChanged: {
            if (root.audioMode === "source"
                    && mediaPlayer.playbackState === MediaPlayer.PlayingState) {
                root._lastRequestedPositionMs = mediaPlayer.position;
                if (root.available
                        && typeof subtitleController.syncActiveCueFromVideo === "function") {
                    subtitleController.syncActiveCueFromVideo(mediaPlayer.position);
                }
            }
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 4

        // ── Header row: title, audio mode switch, caption toggle, collapse ──
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingXs

            SectionLabel {
                objectName: "subtitleVideoSectionLabel"
                Layout.fillWidth: true
                elide: Text.ElideRight
                text: root.videoName !== ""
                    ? qsTr("XEM TRƯỚC VIDEO · %1").arg(root.videoName)
                    : qsTr("XEM TRƯỚC VIDEO")
            }

            AppButton {
                id: audioModeSwitch

                objectName: "subtitleVideoAudioModeSwitch"
                variant: "chip"
                size: "sm"
                checked: root.audioMode === "dub"
                text: root.audioMode === "dub" ? qsTr("Lồng tiếng") : qsTr("Âm gốc")
                tooltipText: qsTr("Chuyển nguồn âm thanh khi xem trước (Âm gốc / Lồng tiếng)")
                accessibleLabel: text
                enabled: root.available && (root.hasTrack || root.audioMode === "dub")
                disabledReason: root.hasTrack
                    ? "" : qsTr("Cần tạo âm thanh lồng tiếng trước.")
                onClicked: {
                    if (root.audioMode === "dub") {
                        root.preferredAudioMode = "source";
                        if (root.available && root.dubPlayerState === "playing")
                            subtitleController.pause();
                    } else if (root.hasTrack) {
                        root.preferredAudioMode = "dub";
                        if (mediaPlayer.playbackState === MediaPlayer.PlayingState)
                            mediaPlayer.pause();
                    }
                }
            }

            AppButton {
                id: captionToggleBtn

                objectName: "subtitleVideoCaptionToggle"
                variant: "chip"
                size: "sm"
                checked: root.captionsEnabled
                text: qsTr("Phụ đề")
                tooltipText: root.captionsEnabled
                    ? qsTr("Ẩn phụ đề trên khung hình video")
                    : qsTr("Hiện phụ đề trên khung hình video")
                accessibleLabel: tooltipText
                onClicked: {
                    root.captionsEnabled = !root.captionsEnabled;
                }
            }

            AppIconButton {
                id: collapseBtn

                objectName: "subtitleVideoCollapseButton"
                size: "sm"
                iconKind: root.collapsed ? "chevronDown" : "chevronUp"
                tooltipText: root.collapsed
                    ? qsTr("Mở khung xem trước video")
                    : qsTr("Thu gọn khung xem trước video")
                accessibleLabel: tooltipText
                onClicked: {
                    root.userCollapseOverride = true;
                    root.collapsed = !root.collapsed;
                }
            }
        }

        // ── 16:9 Video Viewport + Live Caption Overlay + Codec Fallback ─────
        Rectangle {
            id: viewport

            objectName: "subtitleVideoViewport"
            Layout.fillWidth: true
            Layout.preferredHeight: root.stageName === "source" ? 128 : 76
            visible: !root.collapsed
            radius: Theme.radiusMd
            color: "#0B0F17"
            border.width: 1
            border.color: Theme.borderSubtle
            clip: true

            VideoOutput {
                id: videoOutput

                anchors.fill: parent
                fillMode: VideoOutput.PreserveAspectFit
                visible: !root.hasDecodeError
            }

            Label {
                id: fallbackLabel

                objectName: "subtitleVideoFallbackLabel"
                anchors.centerIn: parent
                width: Math.max(120, parent.width - Theme.spacingMd * 2)
                visible: root.hasDecodeError
                text: qsTr("Không giải mã được khung hình video trên hệ thống này — các chức năng nhận dạng, lồng tiếng và xuất video vẫn hoạt động bình thường.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
            }

            Rectangle {
                id: captionOverlay

                objectName: "subtitleVideoCaptionOverlay"
                property alias captionText: captionTextLabel.text

                anchors.horizontalCenter: parent.horizontalCenter
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Theme.spacingSm
                width: Math.min(
                    parent.width - Theme.spacingMd * 2,
                    captionRow.implicitWidth + Theme.spacingMd * 2
                )
                height: captionRow.implicitHeight + Theme.spacingXs * 2
                visible: root.captionsEnabled && root.activeCaptionText !== ""
                radius: Theme.radiusMd
                color: Qt.rgba(0.06, 0.08, 0.12, 0.84)
                border.width: 1
                border.color: Qt.rgba(1, 1, 1, 0.14)

                RowLayout {
                    id: captionRow

                    anchors.fill: parent
                    anchors.leftMargin: Theme.spacingSm
                    anchors.rightMargin: Theme.spacingSm
                    anchors.topMargin: Theme.spacingXs
                    anchors.bottomMargin: Theme.spacingXs
                    spacing: Theme.spacingXs

                    StatusBadge {
                        visible: root.activeCaptionSpeaker !== ""
                        status: "info"
                        dotVisible: false
                        text: root.activeCaptionSpeaker
                        customPadding: Theme.spacingXs
                        Layout.alignment: Qt.AlignVCenter
                    }

                    Label {
                        id: captionTextLabel

                        Layout.fillWidth: true
                        text: root.activeCaptionText
                        color: "#F8FAFC"
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }

        // ── Mini Transport & Scrub Bar ──────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            visible: !root.collapsed
            spacing: Theme.spacingXs

            AppIconButton {
                id: videoPlayBtn

                objectName: "subtitleVideoPlayButton"
                size: "sm"
                iconKind: root.isPlaying ? "pause" : "play"
                tooltipText: root.isPlaying
                    ? qsTr("Tạm dừng xem trước video")
                    : qsTr("Phát xem trước video")
                accessibleLabel: tooltipText
                enabled: root.available && root.hasVideo && !root.exporting
                disabledReason: root.busyReason
                onClicked: {
                    if (!root.available)
                        return;
                    if (root.audioMode === "dub" && root.hasTrack) {
                        if (root.dubPlayerState === "playing")
                            subtitleController.pause();
                        else if (root.dubPlayerState === "paused")
                            subtitleController.resume();
                        else
                            subtitleController.play();
                        return;
                    }
                    if (mediaPlayer.playbackState === MediaPlayer.PlayingState) {
                        mediaPlayer.pause();
                    } else {
                        if (typeof subtitleController.notifyVideoPreviewPlaying === "function")
                            subtitleController.notifyVideoPreviewPlaying();
                        root.preferredAudioMode = "source";
                        root._audioOutputRequested = true;
                        if (mediaPlayer.source.toString() === "" && root.videoUrl !== "")
                            root.syncSourceFromUrl();
                        mediaPlayer.play();
                    }
                }
            }

            AppSlider {
                id: scrubSlider

                objectName: "subtitleVideoScrubSlider"
                accessibleLabel: qsTr("Vị trí xem trước video")
                Layout.fillWidth: true
                Layout.minimumWidth: scrubSlider.minimumTrackWidth
                enabled: root.available && root.hasVideo && !root.exporting
                disabledReason: root.busyReason
                from: 0
                to: Math.max(1000, root.effectiveDurationMs)
                value: pressed ? value : Math.min(to, Math.max(0, root.displayPositionMs))
                onMoved: {
                    const target = Math.round(value);
                    root.seekVideoTo(target);
                    if (root.available) {
                        if (root.audioMode === "dub" && root.hasTrack)
                            subtitleController.seek(target);
                        else if (typeof subtitleController.syncActiveCueFromVideo === "function")
                            subtitleController.syncActiveCueFromVideo(target);
                    }
                }
            }

            Label {
                id: timecodeLabel

                objectName: "subtitleVideoTimecodeLabel"
                text: root.formatTimecode(root.displayPositionMs) + " / "
                    + root.formatTimecode(root.effectiveDurationMs)
                color: Theme.textMuted
                font.family: Theme.fontFamilyMono !== ""
                    ? Theme.fontFamilyMono : Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
            }
        }
    }

    Connections {
        target: root.available ? subtitleController : null
        ignoreUnknownSignals: true

        function onVideoChanged() {
            if (root.available) {
                root.hasVideo = Boolean(subtitleController.hasVideo);
                root.videoName = String(subtitleController.videoName || "");
                root.videoPath = String(subtitleController.videoPath || "");
                root.videoUrl = String(subtitleController.videoUrl || "");
            }
            root.syncSourceFromUrl();
        }

        function onVideoSeekRequested(ms) {
            root.seekVideoTo(ms);
            root._cueRevision += 1;
        }

        function onVideoReleaseRequested() {
            root.releaseVideo();
        }

        function onPositionMsChanged() {
            if (root.available)
                root.dubPositionMs = Number(subtitleController.positionMs || 0);
        }

        function onDurationChanged() {
            if (root.available)
                root.dubDurationMs = Number(subtitleController.durationMs || 0);
        }

        function onPlayerStateChanged() {
            if (!root.available)
                return;
            if (subtitleController.playerState === "playing") {
                if (root.hasTrack) {
                    root.preferredAudioMode = "dub";
                    root.seekVideoTo(root.dubPositionMs);
                    if (mediaPlayer.source.toString() !== "" && !root.hasDecodeError)
                        mediaPlayer.play();
                } else if (mediaPlayer.playbackState === MediaPlayer.PlayingState) {
                    mediaPlayer.pause();
                }
            } else if (root.audioMode === "dub"
                    && mediaPlayer.playbackState === MediaPlayer.PlayingState) {
                mediaPlayer.pause();
            }
        }

        function onActiveCueChanged() {
            root._cueRevision += 1;
        }

        function onCuesChanged() {
            root._cueRevision += 1;
        }
    }
}
