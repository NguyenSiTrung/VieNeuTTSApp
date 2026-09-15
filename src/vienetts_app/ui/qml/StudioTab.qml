// Studio tab — pro-audio studio: offline polish + single-segment re-gen.
// Feeder tabs (Text / Paragraph / Audiobook) route their artifact here via
// controller.openInStudio / openChapterInStudio, then flip to this tab.
//
// Layout: the page header and the transport dock are PINNED; only the body
// (clips → FX rack → op history) scrolls. The dock is the only thing that
// auditions audio, so it must never scroll away from the controls that change
// what you hear — the old order put the clip editor 1084 px down a 652 px
// viewport with the transport at the top and the fades ~800 px below it.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
//   studioTab, studioWaveform, studioOpStack, studioClipList,
//   studioPreviewButton, studioExportButton, studioGainApply, studioRegenButton,
//   studioOpenButton, studioRegenConfirmButton, studioResetButton.
// The redesign added: studioTransportDock, studioSelectionBar,
//   studioTrimSelectionButton, studioCutSelectionButton, studioClipPlayButton,
//   studioDeleteClipButton, studioQuickExportButton, studioOpHistoryCard,
//   studioOpChip, studioOpBaseChip, studioDockTarget, studioSelectionLabel,
//   studioShortcutPlay / Stop / SeekBack / SeekForward.
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

    readonly property int contentMaxWidth: 960

    // ── Model shortcuts: one binding each, read by many children ───────────
    readonly property var clips: controller.studioClips || []
    readonly property var ops: controller.studioOps || []
    readonly property var applied: controller.studioControls || {}

    // ── Waveform range selection ──────────────────────────────────────────
    // Fractions 0..1 of the audio the dock is showing. PlaybackWaveform never
    // writes these (assigning here would destroy the host binding); it reports
    // a drag through selectionChanged and a plain click through
    // selectionCleared, and the host owns the commit. A range only means
    // something against the mix it was drawn on, so it is dropped whenever
    // that mix changes — and it is disabled outright while a single clip is
    // auditioned, because "keep 0.2–0.6" would then trim the whole mix.
    property real selectionStart: -1
    property real selectionEnd: -1
    readonly property bool hasSelection: selectionStart >= 0 && selectionEnd > selectionStart

    // Id of the clip being auditioned; "" means the transport is on the mix.
    readonly property string auditionClipId: controller.studioClipPlayingId || ""
    readonly property bool auditioningClip: root.auditionClipId !== ""

    // Length of whatever the dock's waveform and timecode describe: a running
    // replay, else the auditioned clip, else the rendered mix.
    readonly property int dockTotalMs: {
        if (controller.replayActive && controller.replayDurationMs > 0)
            return controller.replayDurationMs;
        if (root.auditioningClip)
            return controller.studioClipDurationMs;
        if (controller.studioDurationMs > 0)
            return controller.studioDurationMs;
        let sumMs = 0;
        for (let i = 0; i < root.clips.length; i++)
            sumMs += Math.round((root.clips[i].duration || 0) * 1000);
        return sumMs;
    }

    // Rack edits need a project, a free worker and no render in flight.
    readonly property bool rackEnabled: controller.hasStudioProject
        && !controller.busy && controller.studioBusy !== true

    // True while the shell is showing this tab — the transport keys below are
    // window shortcuts, so they must not fire from another studio.
    readonly property bool tabActive: typeof bridge !== "undefined" && bridge !== null
        && bridge.currentTab === "studio"

    readonly property string dockStateText: !controller.replayActive
        ? qsTr("Sẵn sàng")
        : (controller.replayPaused ? qsTr("Tạm dừng") : qsTr("Đang phát"))
    readonly property string dockTargetText: root.auditioningClip
        ? qsTr("Đoạn #%1").arg(root.clipLabelFor(root.auditionClipId))
        : qsTr("Toàn bộ dự án")
    readonly property string selectionRangeText: root.hasSelection
        ? qsTr("%1 – %2").arg(root.formatTime(root.selectionStart * root.dockTotalMs))
            .arg(root.formatTime(root.selectionEnd * root.dockTotalMs))
        : ""

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

    // A clip row's display label ("1", "2", "Ch 3"), from its stable id.
    function clipLabelFor(clipId) {
        for (let i = 0; i < root.clips.length; i++) {
            if (root.clips[i].id === clipId)
                return String(root.clips[i].label || (i + 1));
        }
        return "";
    }

    function clearSelection() {
        root.selectionStart = -1;
        root.selectionEnd = -1;
    }

    // Shared seek step for the dock buttons and the ←/→ shortcuts.
    function seekBy(deltaMs) {
        if (root.dockTotalMs <= 0)
            return;
        controller.seekReplay(controller.replayPosition + deltaMs / root.dockTotalMs);
    }

    // Commit the drawn range: keep it (TrimOp) or remove it (CutOp). The
    // controller takes milliseconds and maps them to 48 kHz frames.
    function applySelection(keep) {
        if (!root.hasSelection || root.dockTotalMs <= 0)
            return;
        const startMs = Math.round(root.selectionStart * root.dockTotalMs);
        const endMs = Math.round(root.selectionEnd * root.dockTotalMs);
        root.clearSelection();
        if (keep)
            controller.studioPushTrimRange(startMs, endMs);
        else
            controller.studioPushCutRange(startMs, endMs);
    }

    // The mix's actual setting for a rack parameter. Read through a binding so
    // every readout re-evaluates on studioControlsChanged.
    function appliedValue(key, fallback) {
        const v = root.applied;
        return (v && typeof v[key] === "number") ? v[key] : fallback;
    }

    // Sliders show an absolute setting, so a value that differs from the mix
    // is a pending edit — surfaced instead of silently ignored.
    function isDirty(key, pending, fallback, epsilon) {
        return Math.abs(pending - root.appliedValue(key, fallback)) > epsilon;
    }

    function syncControls() {
        const values = root.applied;
        gainRow.sliderValue = typeof values.gain === "number" ? values.gain : 0;
        fadeRow.sliderValue = typeof values.fade === "number" ? values.fade : 200;
        speedRow.sliderValue = typeof values.speed === "number" ? values.speed : 1.0;
        gapRow.sliderValue = typeof values.gap === "number" ? values.gap : 500;
    }

    Connections {
        target: controller
        function onStudioControlsChanged() { root.syncControls(); }
        // The rendered mix changed, so a range drawn on the old one is stale.
        function onStudioProjectChanged() { root.clearSelection(); }
        // The dock's waveform switched between mix and clip — same reasoning.
        function onStudioAuditionChanged() { root.clearSelection(); }
    }

    Component.onCompleted: root.syncControls()

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
                    text: qsTr("Hủy")
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

    // Reset drops the whole op stack in one click — confirm first.
    Dialog {
        id: resetDialog

        anchors.centerIn: parent
        modal: true
        title: qsTr("Đặt lại về bản gốc?")

        background: Rectangle {
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
            radius: Theme.radiusLg
        }

        contentItem: ColumnLayout {
            spacing: Theme.spacingMd
            width: Math.min(400, root.width - Theme.spacingLg * 2)

            Label {
                text: qsTr("Toàn bộ %1 hiệu ứng đã áp dụng sẽ bị xoá. Âm thanh gốc vẫn được giữ nguyên.").arg(root.ops.length)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Item { Layout.fillWidth: true }

                AppButton {
                    variant: "quiet"
                    text: qsTr("Hủy")
                    onClicked: resetDialog.close()
                }

                AppButton {
                    variant: "danger"
                    text: qsTr("Đặt lại gốc")
                    onClicked: {
                        controller.studioReset();
                        resetDialog.close();
                    }
                }
            }
        }
    }

    // ── Keyboard transport (this tab only) ────────────────────────────────
    // Mirrors AudiobookTab's keys: Space toggles play/pause, ←/→ seek 5 s,
    // Escape stops. Gated on the active tab so no other studio's keys clash.
    Shortcut {
        objectName: "studioShortcutPlay"
        sequence: "Space"
        enabled: root.tabActive && controller.hasStudioProject
        context: Qt.WindowShortcut
        onActivated: {
            if (controller.replayActive) {
                if (controller.replayPaused)
                    controller.resumeReplay();
                else
                    controller.pauseReplay();
            } else {
                controller.studioPreview();
            }
        }
    }
    Shortcut {
        objectName: "studioShortcutStop"
        sequence: "Escape"
        enabled: root.tabActive && controller.replayActive
        context: Qt.WindowShortcut
        onActivated: controller.stopReplay()
    }
    Shortcut {
        objectName: "studioShortcutSeekBack"
        sequence: "Left"
        enabled: root.tabActive && controller.replayActive && root.dockTotalMs > 0
        context: Qt.WindowShortcut
        onActivated: root.seekBy(-5000)
    }
    Shortcut {
        objectName: "studioShortcutSeekForward"
        sequence: "Right"
        enabled: root.tabActive && controller.replayActive && root.dockTotalMs > 0
        context: Qt.WindowShortcut
        onActivated: root.seekBy(5000)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingLg

        // ── Transport dock (pinned) ─────────────────────────────────────
        // The one place that auditions audio. It describes whatever it is
        // pointed at — the whole mix or a single clip — so the timecode, the
        // waveform and the highlighted clip row can never disagree.
        // It is the ONLY pinned element: the page header scrolls with the
        // body, because at the 640x420 minimum a pinned header plus a pinned
        // dock left the body a 0 px viewport (measured) — the transport is
        // what must stay reachable, a title is not.
        AppCard {
            objectName: "studioTransportDock"
            Layout.fillWidth: true
            Layout.maximumWidth: root.contentMaxWidth
            Layout.alignment: Qt.AlignHCenter
            visible: controller.hasStudioProject
            cardPadding: Theme.spacingMd
            z: 1

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    // Idle reads neutral, live reads accent — a green "ready"
                    // dot inverted the usual transport convention (green =
                    // something is actively running).
                    Rectangle {
                        width: 8
                        height: 8
                        radius: 4
                        color: controller.replayActive ? Theme.accent : Theme.textSubtle
                    }

                    Label {
                        text: root.dockStateText
                        color: controller.replayActive ? Theme.accent : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                    }

                    Label {
                        text: "·"
                        color: Theme.textSubtle
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }

                    Label {
                        objectName: "studioDockTarget"
                        Layout.fillWidth: true
                        text: root.dockTargetText
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        elide: Text.ElideRight
                    }

                    Rectangle {
                        radius: Theme.radiusSm
                        color: Theme.surfaceAlt
                        border.color: Theme.borderSubtle
                        border.width: 1
                        implicitHeight: 28
                        implicitWidth: digitalTimeLabel.implicitWidth + Theme.spacingMd * 2

                        Label {
                            id: digitalTimeLabel
                            anchors.centerIn: parent
                            text: root.formatTime(controller.replayActive
                                ? Math.round(controller.replayPosition * root.dockTotalMs) : 0)
                                + " / " + root.formatTime(root.dockTotalMs)
                            color: controller.replayActive ? Theme.accent : Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            font.weight: Theme.fontWeightMedium
                        }
                    }
                }

                PlaybackWaveform {
                    id: studioWaveform

                    objectName: "studioWaveform"
                    Layout.fillWidth: true
                    // Shorter on a short window: at the 420 px minimum height
                    // the dock is the only thing the user can see, so every
                    // pixel it keeps for itself is a pixel the body loses.
                    Layout.preferredHeight: root.height < 620 ? 44 : 72
                    envelope: root.auditioningClip ? controller.studioClipEnvelope : controller.studioEnvelope
                    position: controller.replayPosition
                    active: controller.replayActive
                    durationMs: root.dockTotalMs
                    // Seeking only means something while a replay is live;
                    // selecting a range works idle too (that is how a trim is
                    // drawn). Both are off while a clip is auditioned, whose
                    // fractions do not describe the mix a trim would edit.
                    seekable: controller.hasStudioProject && !root.auditioningClip
                        && controller.replayActive && root.dockTotalMs > 0
                    selectable: controller.hasStudioProject && !root.auditioningClip
                        && root.dockTotalMs > 0
                    selectionStart: root.selectionStart
                    selectionEnd: root.selectionEnd
                    onSeekRequested: (fraction) => controller.seekReplay(fraction)
                    onSelectionChanged: (start, end) => {
                        root.selectionStart = start;
                        root.selectionEnd = end;
                    }
                    onSelectionCleared: root.clearSelection()
                }

                // Range toolbar: only while a range exists, and it names the
                // range in seconds because the two buttons edit the mix.
                RowLayout {
                    objectName: "studioSelectionBar"

                    Layout.fillWidth: true
                    spacing: Theme.spacingSm
                    visible: root.hasSelection

                    Label {
                        objectName: "studioSelectionLabel"
                        Layout.fillWidth: true
                        text: qsTr("Vùng chọn: %1").arg(root.selectionRangeText)
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                        elide: Text.ElideRight
                    }

                    AppButton {
                        objectName: "studioTrimSelectionButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "check"
                        text: qsTr("Giữ vùng chọn")
                        tooltipText: qsTr("Chỉ giữ lại đoạn đã chọn, bỏ phần còn lại")
                        enabled: root.rackEnabled
                        onClicked: root.applySelection(true)
                    }

                    // Cut removes audio from the mix — danger styling so the
                    // destructive half of the pair never reads as a sibling
                    // of the safe keep.
                    AppButton {
                        objectName: "studioCutSelectionButton"
                        variant: "danger"
                        size: "sm"
                        iconKind: "close"
                        text: qsTr("Xoá vùng chọn")
                        tooltipText: qsTr("Bỏ đoạn đã chọn và nối hai phần còn lại")
                        enabled: root.rackEnabled
                        onClicked: root.applySelection(false)
                    }

                    AppButton {
                        variant: "quiet"
                        size: "sm"
                        text: qsTr("Bỏ chọn")
                        onClicked: root.clearSelection()
                    }
                }

                // A Flow, not a RowLayout: at the 640 px minimum width the
                // full transport cluster overflows the dock and must wrap.
                //
                // Hierarchy: play/pause is the dock's one primary (it is the
                // reason the dock exists); seek/stop/replay are icon actions;
                // the two export paths collapse to a secondary dialog button
                // plus an icon-only quick export — two adjacent filled CTAs
                // used to flatten the hierarchy exactly while playing.
                Flow {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        objectName: "studioSeekBackButton"
                        variant: "icon"
                        size: "lg"
                        iconKind: "previous"
                        accessibleLabel: qsTr("Lùi 5 giây")
                        tooltipText: qsTr("Lùi 5 giây") + " (←)"
                        enabled: controller.replayActive && root.dockTotalMs > 0
                        onClicked: root.seekBy(-5000)
                    }

                    AppButton {
                        id: previewBtn

                        objectName: "studioPreviewButton"
                        variant: "primary"
                        size: "lg"
                        text: controller.replayActive ? (controller.replayPaused ? qsTr("Tiếp tục") : qsTr("Tạm dừng")) : qsTr("Nghe thử")
                        iconKind: controller.replayActive ? (controller.replayPaused ? "play" : "pause") : "play"
                        enabled: controller.hasStudioProject && controller.studioBusy !== true
                        busy: controller.studioBusyKind === "preview"
                        tooltipText: (controller.replayActive
                            ? (controller.replayPaused ? qsTr("Phát tiếp từ vị trí đã dừng") : qsTr("Tạm dừng, giữ nguyên vị trí"))
                            : qsTr("Nghe thử toàn bộ dự án")) + " (Space)"
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
                        objectName: "studioSeekForwardButton"
                        variant: "icon"
                        size: "lg"
                        iconKind: "next"
                        accessibleLabel: qsTr("Tiến 5 giây")
                        tooltipText: qsTr("Tiến 5 giây") + " (→)"
                        enabled: controller.replayActive && root.dockTotalMs > 0
                        onClicked: root.seekBy(5000)
                    }

                    // Visible stop: previously Esc-only, which nobody finds.
                    AppButton {
                        objectName: "studioStopButton"
                        variant: "icon"
                        size: "lg"
                        iconKind: "stop"
                        accessibleLabel: qsTr("Dừng")
                        tooltipText: qsTr("Dừng") + " (Esc)"
                        enabled: controller.replayActive
                        onClicked: controller.stopReplay()
                    }

                    AppButton {
                        variant: "quiet"
                        size: "lg"
                        iconKind: "reset"
                        // Icon-only on a short window: that keeps the transport
                        // on ONE row, which is worth ~50 px of scrolling body
                        // (at 640x420 the two-row dock left the body a 100 px
                        // viewport). The label survives in the tooltip and the
                        // accessible name.
                        text: root.height < 620 ? "" : qsTr("Phát lại từ đầu")
                        accessibleLabel: qsTr("Phát lại từ đầu")
                        tooltipText: qsTr("Dừng và phát lại từ đầu dự án")
                        enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                        onClicked: {
                            controller.stopReplay();
                            controller.studioPreview();
                        }
                    }

                    AppButton {
                        objectName: "studioQuickExportButton"
                        variant: "icon"
                        size: "lg"
                        iconKind: "folder"
                        accessibleLabel: qsTr("Xuất nhanh")
                        tooltipText: qsTr("Xuất nhanh") + " — " + qsTr("xuất ngay vào thư mục đầu ra đã chọn trong Cài đặt")
                        enabled: controller.hasStudioProject && controller.exporting !== true && controller.studioBusy !== true
                        onClicked: controller.studioExport("")
                    }

                    AppButton {
                        id: studioExportBtn

                        objectName: "studioExportButton"
                        variant: "secondary"
                        size: "lg"
                        text: qsTr("Xuất âm thanh…")
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
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── Scrolling body: clips → FX rack → op history ────────────────
        ScrollView {
            id: bodyScroll

            objectName: "pageScrollView"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true

            ScrollBar.vertical: ScrollBar {
                objectName: "studioScrollBarV"
                policy: ScrollBar.AsNeeded
                implicitWidth: 8
                contentItem: Rectangle {
                    radius: 4
                    color: Theme.border
                    opacity: 0.7
                }
                background: Rectangle {
                    radius: 4
                    color: "transparent"
                }
            }

            ColumnLayout {
                id: bodyColumn

                width: Math.max(1, Math.min(root.contentMaxWidth, bodyScroll.availableWidth))
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.spacingLg

                // ── Studio header (scrolls) ─────────────────────────────
                PageHeader {
                    Layout.fillWidth: true
                    iconKind: "studio"
                    title: qsTr("Studio Âm thanh")
                    // The subtitle is prose: at a short window it wrapped to
                    // three lines and ate the whole body viewport, so it steps
                    // aside for the clips instead of pushing them below the fold.
                    subtitle: root.height < 620
                        ? ""
                        : qsTr("Tinh chỉnh hiệu ứng hậu kỳ, sắp xếp các đoạn và hoàn thiện âm thanh trước khi xuất.")

                    trailing: RowLayout {
                        spacing: Theme.spacingSm
                        visible: controller.hasStudioProject

                        Rectangle {
                            radius: Theme.radiusPill
                            color: Theme.accentSubtle
                            implicitHeight: 28
                            implicitWidth: clipCountLabel.implicitWidth + Theme.spacingMd * 2

                            Label {
                                id: clipCountLabel
                                anchors.centerIn: parent
                                text: qsTr("%1 đoạn").arg(root.clips.length)
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightMedium
                            }
                        }
                    }
                }

                // ── Empty state ─────────────────────────────────────────
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

                            // 3 quick navigation cards. Sized from the Flow's
                            // own box, not the page column: these cards sit
                            // inside a card that has its own padding, and
                            // sizing them from the column made the third one
                            // wrap at EVERY width from 640 to 1120 (measured —
                            // a 2+1 wrap reads as a mistake). Below the width
                            // where three columns can still hold a button they
                            // stack one per row instead of splitting 2+1.
                            // Heights equalize and CTAs pin to the bottom so
                            // the three cards read as one row, not three
                            // unrelated boxes; the whole card is the tap
                            // target, the button is the discoverable cue.
                            Flow {
                                id: guideFlow

                                Layout.fillWidth: true
                                spacing: Theme.spacingMd

                                readonly property real threeAcross: (guideFlow.width - Theme.spacingMd * 2) / 3
                                readonly property real cardWidth: guideFlow.threeAcross >= 200
                                    ? guideFlow.threeAcross : guideFlow.width
                                property real cardHeight: 0

                                function measureCards() {
                                    cardHeight = Math.max(
                                        guideCardText.implicitHeight,
                                        guideCardParagraph.implicitHeight,
                                        guideCardAudiobook.implicitHeight);
                                }
                                onWidthChanged: measureCards()

                                AppCard {
                                    id: guideCardText
                                    objectName: "studioGuideCard"
                                    width: guideFlow.cardWidth
                                    height: guideFlow.cardHeight > 0 ? guideFlow.cardHeight : implicitHeight
                                    elevation: 0
                                    clickable: true
                                    onCardClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("text")
                                    cardColor: cardHovered ? Theme.surfaceHover : Theme.surfaceAlt
                                    cardBorderColor: cardHovered ? Theme.border : Theme.borderSubtle
                                    cardRadius: Theme.radiusMd
                                    cardPadding: Theme.spacingMd
                                    onImplicitHeightChanged: guideFlow.measureCards()

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        Layout.fillHeight: true
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

                                        Item { Layout.fillHeight: true }

                                        AppButton {
                                            variant: "secondary"
                                            size: "sm"
                                            text: qsTr("Đến Tab Văn bản")
                                            onClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("text")
                                        }
                                    }
                                }

                                AppCard {
                                    id: guideCardParagraph
                                    objectName: "studioGuideCard"
                                    width: guideFlow.cardWidth
                                    height: guideFlow.cardHeight > 0 ? guideFlow.cardHeight : implicitHeight
                                    elevation: 0
                                    clickable: true
                                    onCardClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("paragraph")
                                    cardColor: cardHovered ? Theme.surfaceHover : Theme.surfaceAlt
                                    cardBorderColor: cardHovered ? Theme.border : Theme.borderSubtle
                                    cardRadius: Theme.radiusMd
                                    cardPadding: Theme.spacingMd
                                    onImplicitHeightChanged: guideFlow.measureCards()

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        Layout.fillHeight: true
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

                                        Item { Layout.fillHeight: true }

                                        AppButton {
                                            variant: "secondary"
                                            size: "sm"
                                            text: qsTr("Đến Tab Đoạn văn")
                                            onClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("paragraph")
                                        }
                                    }
                                }

                                AppCard {
                                    id: guideCardAudiobook
                                    objectName: "studioGuideCard"
                                    width: guideFlow.cardWidth
                                    height: guideFlow.cardHeight > 0 ? guideFlow.cardHeight : implicitHeight
                                    elevation: 0
                                    clickable: true
                                    onCardClicked: if (typeof bridge !== "undefined" && bridge) bridge.setCurrentTab("audiobook")
                                    cardColor: cardHovered ? Theme.surfaceHover : Theme.surfaceAlt
                                    cardBorderColor: cardHovered ? Theme.border : Theme.borderSubtle
                                    cardRadius: Theme.radiusMd
                                    cardPadding: Theme.spacingMd
                                    onImplicitHeightChanged: guideFlow.measureCards()

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        Layout.fillHeight: true
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

                                        Item { Layout.fillHeight: true }

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

                // ── 1. Clip list ────────────────────────────────────────
                // Plain layout, no inner ScrollView: nesting a 480 px flickable
                // inside the page scroll swallowed every wheel event and left
                // the page itself stuck.
                AppCard {
                    Layout.fillWidth: true
                    visible: controller.hasStudioProject
                    title: qsTr("Đoạn âm thanh")
                    subtitle: qsTr("Nghe thử từng đoạn, đổi thứ tự, tạo lại câu từ hoặc bỏ đoạn không cần thiết.")
                    badgeText: qsTr("%1 phân đoạn").arg(root.clips.length)

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        ColumnLayout {
                            objectName: "studioClipList"
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Repeater {
                                model: controller.studioClips

                                StudioClipRow {
                                    Layout.fillWidth: true
                                    clipsCount: root.clips.length
                                    auditionClipId: root.auditionClipId
                                    showDuration: root.width >= 720
                                    onRegenRequested: (clipData, clipIndex) => {
                                        regenDialog.clipId = clipData.id;
                                        regenDialog.clipLabel = String(clipIndex + 1);
                                        regenDialog.clipText = clipData.text || clipData.label || "";
                                        regenDialog.clipDuration = clipData.duration_str || "";
                                        regenDialog.open();
                                    }
                                }
                            }
                        }

                        // Single clip hint
                        Label {
                            Layout.fillWidth: true
                            visible: root.clips.length === 1
                            text: qsTr("Âm thanh hiện tại gồm 1 đoạn duy nhất. Bấm Tạo lại để thay đổi giọng đọc hoặc sửa lại văn bản cho đoạn này.")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                // ── 2. FX rack ──────────────────────────────────────────
                // Every slider is an absolute setting and every readout comes
                // from folding the op stack exactly like render_project does
                // (gain sums, speed multiplies), so what you read is what you
                // hear — and Apply replaces that setting instead of stacking a
                // second copy of it. Module/param chrome lives in
                // StudioRackModule/StudioParamRow so the four rows share one
                // implementation (slider + numeric entry + presets + apply).
                AppCard {
                    id: opStackCard

                    objectName: "studioOpStack"
                    Layout.fillWidth: true
                    visible: controller.hasStudioProject
                    title: qsTr("Tinh chỉnh âm thanh")
                    subtitle: qsTr("Thông số hiển thị đúng bằng bản trộn đang có; Áp dụng đặt lại thông số đó thay vì cộng dồn.")

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingLg

                        // Module 1: level — gain, peak normalize, silence trim
                        StudioRackModule {
                            Layout.fillWidth: true
                            title: qsTr("ÂM LƯỢNG & TỈA LẶNG")

                            StudioParamRow {
                                id: gainRow

                                Layout.fillWidth: true
                                title: qsTr("Khuếch đại (dB)")
                                sliderObjectName: "studioGainSlider"
                                applyObjectName: "studioGainApply"
                                from: -20
                                to: 12
                                stepSize: 0.5
                                decimals: 1
                                presets: [
                                    { "text": "-3 dB", "value": -3.0 },
                                    { "text": "0 dB", "value": 0.0 },
                                    { "text": "+3 dB", "value": 3.0 }
                                ]
                                dirty: root.isDirty("gain", gainRow.sliderValue, 0, 0.001)
                                busy: controller.studioBusyKind === "gain"
                                rowEnabled: controller.hasStudioProject && !controller.busy
                                applyEnabled: root.rackEnabled
                                onApplied: controller.studioPushGain(gainRow.sliderValue)
                            }

                            // One-shot cleanup actions
                            Flow {
                                Layout.fillWidth: true
                                spacing: Theme.spacingSm

                                AppButton {
                                    variant: "secondary"
                                    size: "sm"
                                    text: qsTr("Chuẩn hóa đỉnh (0 dBFS)")
                                    tooltipText: qsTr("Đưa âm lượng đỉnh cao nhất về mức tối đa mà không gây rè âm")
                                    enabled: root.rackEnabled
                                    busy: controller.studioBusyKind === "normalize"
                                    onClicked: controller.studioPushNormalize()
                                }

                                AppButton {
                                    variant: "secondary"
                                    size: "sm"
                                    text: qsTr("Cắt khoảng lặng thừa")
                                    tooltipText: qsTr("Tự động cắt bỏ các đoạn im lặng thừa ở đầu và cuối tệp (-50 dB)")
                                    enabled: root.rackEnabled
                                    busy: controller.studioBusyKind === "silence"
                                    onClicked: controller.studioPushSilenceTrim()
                                }
                            }
                        }

                        // Module 2: pacing — playback speed, gap between clips
                        StudioRackModule {
                            Layout.fillWidth: true
                            title: qsTr("TỐC ĐỘ & KHOẢNG LẶNG")

                            StudioParamRow {
                                id: speedRow

                                Layout.fillWidth: true
                                title: qsTr("Tốc độ (×)")
                                sliderObjectName: "studioSpeedSlider"
                                from: 0.5
                                to: 2.0
                                stepSize: 0.05
                                decimals: 2
                                presets: [
                                    { "text": "0.85×", "value": 0.85 },
                                    { "text": "1.0×", "value": 1.0 },
                                    { "text": "1.25×", "value": 1.25 }
                                ]
                                dirty: root.isDirty("speed", speedRow.sliderValue, 1.0, 0.001)
                                busy: controller.studioBusyKind === "speed"
                                rowEnabled: controller.hasStudioProject && !controller.busy
                                applyEnabled: root.rackEnabled
                                onApplied: controller.studioPushSpeed(speedRow.sliderValue)
                            }

                            StudioParamRow {
                                id: gapRow

                                Layout.fillWidth: true
                                title: qsTr("Khoảng lặng giữa đoạn (ms)")
                                sliderObjectName: "studioGapSlider"
                                from: 0
                                to: 2000
                                stepSize: 100
                                presets: [
                                    { "text": "200 ms", "value": 200 },
                                    { "text": "500 ms", "value": 500 },
                                    { "text": "1000 ms", "value": 1000 }
                                ]
                                dirty: root.isDirty("gap", gapRow.sliderValue, 500, 0.5)
                                busy: controller.studioBusyKind === "gap"
                                rowEnabled: controller.hasStudioProject && !controller.busy
                                applyEnabled: root.rackEnabled
                                onApplied: controller.studioPushGap(gapRow.sliderValue)
                            }
                        }

                        // Module 3: fades — one proposed value, two edges. The
                        // edges hold independent applied values, so both Apply
                        // buttons stay secondary and the pending hint carries
                        // the dirty state instead of promoting either button.
                        StudioRackModule {
                            Layout.fillWidth: true
                            title: qsTr("MỜ DẦN ĐẦU & CUỐI")

                            StudioParamRow {
                                id: fadeRow

                                Layout.fillWidth: true
                                title: qsTr("Mờ dần (ms)")
                                appliedNote: (root.appliedValue("fadeIn", 0) > 0
                                        || root.appliedValue("fadeOut", 0) > 0)
                                    ? qsTr("đang áp dụng: vào %1 ms · ra %2 ms")
                                        .arg(root.appliedValue("fadeIn", 0))
                                        .arg(root.appliedValue("fadeOut", 0))
                                    : ""
                                sliderObjectName: "studioFadeSlider"
                                from: 0
                                to: 1000
                                stepSize: 50
                                presets: [
                                    { "text": "50 ms", "value": 50, "tip": qsTr("Khử tiếng click đầu/cuối") },
                                    { "text": "200 ms", "value": 200 },
                                    { "text": "500 ms", "value": 500 }
                                ]
                                dirty: root.isDirty("fade", fadeRow.sliderValue, 200, 0.5)
                                promoteDirty: false
                                applyText: qsTr("Vào đầu")
                                applyTooltip: qsTr("Áp dụng mờ dần vào đầu âm thanh")
                                secondApplyText: qsTr("Ra cuối")
                                secondApplyTooltip: qsTr("Áp dụng mờ dần ra cuối âm thanh")
                                busy: controller.studioBusyKind === "fade"
                                rowEnabled: controller.hasStudioProject && !controller.busy
                                applyEnabled: root.rackEnabled
                                onApplied: controller.studioPushFade("in", fadeRow.sliderValue)
                                onSecondApplied: controller.studioPushFade("out", fadeRow.sliderValue)
                            }
                        }
                    }
                }
                // ── 3. Op history ───────────────────────────────────────
                // Chips are buttons, not decoration: clicking one drops every
                // step after it, which is the only way back several steps
                // without clicking Undo that many times. Undo/Reset live in
                // the body rather than in AppCard's headerAction slot: at the
                // 640 px minimum the title plus two labelled actions overflowed
                // the card (measured), and the actions belong next to the
                // history they walk back.
                AppCard {
                    id: opTimelineCard

                    objectName: "studioOpHistoryCard"
                    Layout.fillWidth: true
                    visible: controller.hasStudioProject
                    title: qsTr("Lịch sử hiệu ứng (Op Stack)")
                    subtitle: qsTr("Bấm một bước để quay lại đúng trạng thái đó — âm thanh gốc không bị phá hủy.")
                    badgeText: root.ops.length > 0
                        ? qsTr("%1 hiệu ứng").arg(root.ops.length)
                        : qsTr("Gốc (chưa chỉnh sửa)")
                    badgeColor: root.ops.length > 0 ? Theme.accentSubtle : Theme.surfaceAlt
                    badgeTextColor: root.ops.length > 0 ? Theme.accent : Theme.textMuted

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Item { Layout.fillWidth: true }

                            AppButton {
                                id: undoBtn
                                variant: "quiet"
                                size: "sm"
                                iconKind: "reset"
                                text: qsTr("Hoàn tác")
                                tooltipText: root.ops.length > 0
                                    ? qsTr("Bỏ bước %1").arg(root.ops[root.ops.length - 1].name || "")
                                    : ""
                                enabled: root.rackEnabled && root.ops.length > 0
                                onClicked: controller.studioUndo()
                            }

                            AppButton {
                                id: resetBtn
                                objectName: "studioResetButton"
                                variant: "danger"
                                size: "sm"
                                iconKind: "reset"
                                text: qsTr("Đặt lại gốc")
                                tooltipText: qsTr("Xoá toàn bộ hiệu ứng đã áp dụng, quay về âm thanh gốc")
                                enabled: root.rackEnabled && root.ops.length > 0
                                onClicked: resetDialog.open()
                            }
                        }

                        Flow {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            AppButton {
                                objectName: "studioOpBaseChip"
                                variant: "chip"
                                size: "sm"
                                iconKind: "previous"
                                text: qsTr("Bản gốc")
                                tooltipText: qsTr("Quay lại âm thanh gốc, chưa áp dụng hiệu ứng nào")
                                enabled: root.ops.length > 0
                                onClicked: controller.studioRevertTo(-1)
                            }

                            Repeater {
                                model: root.ops

                                AppButton {
                                    required property var modelData
                                    required property int index

                                    objectName: "studioOpChip"
                                    variant: index === root.ops.length - 1 ? "secondary" : "chip"
                                    size: "sm"
                                    text: (index + 1) + ". " + (modelData.desc || modelData.name || "")
                                    tooltipText: qsTr("Quay lại bước %1").arg(index + 1)
                                    enabled: index < root.ops.length - 1
                                    onClicked: controller.studioRevertTo(index)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
