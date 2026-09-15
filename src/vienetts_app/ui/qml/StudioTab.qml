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
        gainSlider.value = typeof values.gain === "number" ? values.gain : 0;
        fadeSlider.value = typeof values.fade === "number" ? values.fade : 200;
        speedSlider.value = typeof values.speed === "number" ? values.speed : 1.0;
        gapSlider.value = typeof values.gap === "number" ? values.gap : 500;
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
        onActivated: controller.seekReplay(controller.replayPosition - 5000 / root.dockTotalMs)
    }
    Shortcut {
        objectName: "studioShortcutSeekForward"
        sequence: "Right"
        enabled: root.tabActive && controller.replayActive && root.dockTotalMs > 0
        context: Qt.WindowShortcut
        onActivated: controller.seekReplay(controller.replayPosition + 5000 / root.dockTotalMs)
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

                    Rectangle {
                        width: 8
                        height: 8
                        radius: 4
                        color: controller.replayActive ? Theme.accent : Theme.success
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

                    AppButton {
                        objectName: "studioCutSelectionButton"
                        variant: "secondary"
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

                // A Flow, not a RowLayout: at the 640 px minimum width four
                // labelled controls overflow the dock.
                Flow {
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
                        variant: "quiet"
                        size: "lg"
                        iconKind: "folder"
                        text: qsTr("Xuất nhanh")
                        tooltipText: qsTr("Xuất ngay vào thư mục đầu ra đã chọn trong Cài đặt")
                        enabled: controller.hasStudioProject && controller.exporting !== true && controller.studioBusy !== true
                        onClicked: controller.studioExport("")
                    }

                    AppButton {
                        id: studioExportBtn

                        objectName: "studioExportButton"
                        variant: "primary"
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
                            Flow {
                                id: guideFlow

                                Layout.fillWidth: true
                                spacing: Theme.spacingMd

                                readonly property real threeAcross: (guideFlow.width - Theme.spacingMd * 2) / 3
                                readonly property real cardWidth: guideFlow.threeAcross >= 200
                                    ? guideFlow.threeAcross : guideFlow.width

                                AppCard {
                                    objectName: "studioGuideCard"
                                    width: guideFlow.cardWidth
                                    elevation: 0
                                    cardColor: Theme.surfaceAlt
                                    cardBorderColor: Theme.borderSubtle
                                    cardRadius: Theme.radiusMd
                                    cardPadding: Theme.spacingMd

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
                                    objectName: "studioGuideCard"
                                    width: guideFlow.cardWidth
                                    elevation: 0
                                    cardColor: Theme.surfaceAlt
                                    cardBorderColor: Theme.borderSubtle
                                    cardRadius: Theme.radiusMd
                                    cardPadding: Theme.spacingMd

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
                                    objectName: "studioGuideCard"
                                    width: guideFlow.cardWidth
                                    elevation: 0
                                    cardColor: Theme.surfaceAlt
                                    cardBorderColor: Theme.borderSubtle
                                    cardRadius: Theme.radiusMd
                                    cardPadding: Theme.spacingMd

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

                                Rectangle {
                                    Layout.fillWidth: true
                                    implicitHeight: rowLayout.implicitHeight + Theme.spacingMd * 2

                                    required property var modelData
                                    required property int index

                                    readonly property bool isRegenerating: (typeof controller.studioRegenClipId !== "undefined")
                                        && controller.studioRegenClipId !== ""
                                        && controller.studioRegenClipId === modelData.id
                                    readonly property bool isAuditioning: root.auditionClipId === modelData.id
                                    readonly property bool highlighted: isRegenerating || isAuditioning

                                    color: highlighted ? Theme.accentSubtle : Theme.surfaceAlt
                                    border.color: highlighted ? Theme.accent : Theme.borderSubtle
                                    border.width: highlighted ? 1.5 : 1
                                    radius: Theme.radiusMd

                                    Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                                    Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

                                    // Two lines, not one: at the 640 px minimum
                                    // a single row of identity + excerpt + five
                                    // controls left the excerpt ~78 px wide
                                    // (measured) and it wrapped to "Hà\nNội…".
                                    // Line 1 is the control bar, line 2 is the
                                    // clip's text at full card width.
                                    ColumnLayout {
                                        id: rowLayout
                                        anchors.fill: parent
                                        anchors.margins: Theme.spacingMd
                                        spacing: Theme.spacingSm

                                        RowLayout {
                                            Layout.fillWidth: true
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

                                            // Audition this clip. The icon and the
                                            // accessible name both follow the
                                            // controller's audition state, so the
                                            // row never claims to be playing a clip
                                            // the transport has already moved off.
                                            AppButton {
                                                objectName: "studioClipPlayButton"
                                                variant: "icon"
                                                size: "sm"
                                                iconKind: isAuditioning && !controller.replayPaused ? "pause" : "play"
                                                accessibleLabel: isAuditioning
                                                    ? qsTr("Dừng nghe đoạn %1").arg(index + 1)
                                                    : qsTr("Nghe thử đoạn %1").arg(index + 1)
                                                tooltipText: accessibleLabel
                                                enabled: controller.hasStudioProject && !controller.busy
                                                onClicked: {
                                                    if (isAuditioning)
                                                        controller.stopReplay();
                                                    else
                                                        controller.studioPreviewClip(modelData.id);
                                                }
                                            }

                                            // Duration pill — dropped at the 640 px
                                            // minimum, where the control bar needs
                                            // the room.
                                            Rectangle {
                                                Layout.preferredWidth: 48
                                                Layout.preferredHeight: 24
                                                radius: Theme.radiusSm
                                                color: Theme.surfaceCard
                                                border.color: Theme.borderSubtle
                                                border.width: 1
                                                visible: root.width >= 720 && Boolean(modelData.duration_str)

                                                Label {
                                                    anchors.centerIn: parent
                                                    text: modelData.duration_str || ""
                                                    color: Theme.textMuted
                                                    font.family: Theme.fontFamily
                                                    font.pixelSize: Theme.fontSizeXs
                                                }
                                            }

                                            Item { Layout.fillWidth: true }

                                            // Reorder actions (only visible when > 1 clip)
                                            AppButton {
                                                variant: "icon"
                                                size: "sm"
                                                iconKind: "chevronUp"
                                                accessibleLabel: qsTr("Chuyển lên")
                                                tooltipText: qsTr("Chuyển đoạn này lên trước")
                                                visible: root.clips.length > 1
                                                enabled: root.rackEnabled && index > 0
                                                onClicked: controller.studioMoveClip(modelData.id, index - 1)
                                            }

                                            AppButton {
                                                variant: "icon"
                                                size: "sm"
                                                iconKind: "chevronDown"
                                                accessibleLabel: qsTr("Chuyển xuống")
                                                tooltipText: qsTr("Chuyển đoạn này xuống sau")
                                                visible: root.clips.length > 1
                                                enabled: root.rackEnabled && index < root.clips.length - 1
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

                                            // Drop the clip. Never the last one —
                                            // a project with no clips cannot render.
                                            AppButton {
                                                objectName: "studioDeleteClipButton"
                                                variant: "icon"
                                                size: "sm"
                                                iconKind: "close"
                                                accessibleLabel: qsTr("Xoá đoạn %1").arg(index + 1)
                                                tooltipText: root.clips.length > 1
                                                    ? qsTr("Bỏ đoạn này khỏi bản trộn")
                                                    : qsTr("Không thể bỏ đoạn cuối cùng của dự án")
                                                enabled: root.rackEnabled && root.clips.length > 1
                                                onClicked: controller.studioDeleteClip(modelData.id)
                                            }
                                        }

                                        // Text excerpt — full card width, so the
                                        // line count is the card's, not the
                                        // control bar's leftover.
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
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                // ── 2. FX rack ──────────────────────────────────────────
                // Every slider is an absolute setting and every readout comes
                // from folding the op stack exactly like render_project does
                // (gain sums, speed multiplies), so what you read is what you
                // hear — and Apply replaces that setting instead of stacking a
                // second copy of it.
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
                                spacing: Theme.spacingMd

                                Label {
                                    Layout.fillWidth: true
                                    text: qsTr("ÂM LƯỢNG & ĐỘNG LỰC HỌC (DYNAMICS)")
                                    color: Theme.accent
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightBold
                                    font.letterSpacing: Theme.trackingWide
                                }

                                // Gain: name + value + presets, then slider + apply
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.spacingSm

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        Label {
                                            text: qsTr("Khuếch đại")
                                            color: Theme.text
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Label {
                                            text: gainSlider.value.toFixed(1) + " dB"
                                            color: Theme.accent
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Item { Layout.fillWidth: true }

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
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        AppSlider {
                                            id: gainSlider

                                            objectName: "studioGainSlider"
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: minimumTrackWidth
                                            from: -20
                                            to: 12
                                            stepSize: 0.5
                                            value: 0
                                            enabled: controller.hasStudioProject && !controller.busy
                                            accessibleLabel: qsTr("Khuếch đại")
                                        }

                                        AppButton {
                                            objectName: "studioGainApply"
                                            variant: root.isDirty("gain", gainSlider.value, 0, 0.001) ? "primary" : "secondary"
                                            size: "md"
                                            text: qsTr("Áp dụng")
                                            enabled: root.rackEnabled
                                            busy: controller.studioBusyKind === "gain"
                                            onClicked: controller.studioPushGain(gainSlider.value)
                                        }

                                        Label {
                                            visible: root.isDirty("gain", gainSlider.value, 0, 0.001)
                                            text: qsTr("Chưa áp dụng")
                                            color: Theme.warning
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                            font.weight: Theme.fontWeightMedium
                                        }
                                    }
                                }

                                // Quick Normalize & Silence Trim actions
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
                                spacing: Theme.spacingMd

                                Label {
                                    Layout.fillWidth: true
                                    text: qsTr("TỐC ĐỘ & NHỊP ĐIỆU (TEMPO & CADENCE)")
                                    color: Theme.accent
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightBold
                                    font.letterSpacing: Theme.trackingWide
                                }

                                // Speed
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.spacingSm

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        Label {
                                            text: qsTr("Tốc độ")
                                            color: Theme.text
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Label {
                                            text: speedSlider.value.toFixed(2) + "×"
                                            color: Theme.accent
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Item { Layout.fillWidth: true }

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
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        AppSlider {
                                            id: speedSlider

                                            objectName: "studioSpeedSlider"
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: minimumTrackWidth
                                            from: 0.5
                                            to: 2.0
                                            stepSize: 0.05
                                            value: 1.0
                                            enabled: controller.hasStudioProject && !controller.busy
                                            accessibleLabel: qsTr("Tốc độ")
                                        }

                                        AppButton {
                                            variant: root.isDirty("speed", speedSlider.value, 1.0, 0.001) ? "primary" : "secondary"
                                            size: "md"
                                            text: qsTr("Áp dụng")
                                            enabled: root.rackEnabled
                                            busy: controller.studioBusyKind === "speed"
                                            onClicked: controller.studioPushSpeed(speedSlider.value)
                                        }

                                        Label {
                                            visible: root.isDirty("speed", speedSlider.value, 1.0, 0.001)
                                            text: qsTr("Chưa áp dụng")
                                            color: Theme.warning
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                            font.weight: Theme.fontWeightMedium
                                        }
                                    }
                                }

                                // Gap between clips
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.spacingSm

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        Label {
                                            text: qsTr("Khoảng lặng giữa đoạn")
                                            color: Theme.text
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Label {
                                            text: gapSlider.value + " ms"
                                            color: Theme.accent
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Item { Layout.fillWidth: true }

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
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        AppSlider {
                                            id: gapSlider

                                            objectName: "studioGapSlider"
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: minimumTrackWidth
                                            from: 0
                                            to: 2000
                                            stepSize: 100
                                            value: 500
                                            enabled: controller.hasStudioProject && !controller.busy
                                            accessibleLabel: qsTr("Khoảng lặng giữa đoạn")
                                        }

                                        AppButton {
                                            variant: root.isDirty("gap", gapSlider.value, 500, 0.5) ? "primary" : "secondary"
                                            size: "md"
                                            text: qsTr("Áp dụng")
                                            enabled: root.rackEnabled
                                            busy: controller.studioBusyKind === "gap"
                                            onClicked: controller.studioPushGap(gapSlider.value)
                                        }

                                        Label {
                                            visible: root.isDirty("gap", gapSlider.value, 500, 0.5)
                                            text: qsTr("Chưa áp dụng")
                                            color: Theme.warning
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                            font.weight: Theme.fontWeightMedium
                                        }
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
                                spacing: Theme.spacingMd

                                Label {
                                    Layout.fillWidth: true
                                    text: qsTr("CHUYỂN TIẾP & MỜ DẦN (FADES & TRANSITIONS)")
                                    color: Theme.accent
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightBold
                                    font.letterSpacing: Theme.trackingWide
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.spacingSm

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        Label {
                                            text: qsTr("Mờ dần")
                                            color: Theme.text
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Label {
                                            text: fadeSlider.value + " ms"
                                            color: Theme.accent
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        // The two edges hold independent values,
                                        // so the mix's own fades are spelled out
                                        // next to the one the slider proposes.
                                        Label {
                                            visible: root.appliedValue("fadeIn", 0) > 0 || root.appliedValue("fadeOut", 0) > 0
                                            text: qsTr("đang áp dụng: vào %1 ms · ra %2 ms")
                                                .arg(root.appliedValue("fadeIn", 0))
                                                .arg(root.appliedValue("fadeOut", 0))
                                            color: Theme.textSubtle
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                            elide: Text.ElideRight
                                        }

                                        Item { Layout.fillWidth: true }

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
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spacingSm

                                        AppSlider {
                                            id: fadeSlider

                                            objectName: "studioFadeSlider"
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: minimumTrackWidth
                                            from: 0
                                            to: 1000
                                            stepSize: 50
                                            value: 200
                                            enabled: controller.hasStudioProject && !controller.busy
                                            accessibleLabel: qsTr("Mờ dần")
                                        }

                                        AppButton {
                                            variant: "secondary"
                                            size: "md"
                                            text: qsTr("Vào đầu")
                                            tooltipText: qsTr("Áp dụng mờ dần vào đầu âm thanh")
                                            enabled: root.rackEnabled
                                            busy: controller.studioBusyKind === "fade"
                                            onClicked: controller.studioPushFade("in", fadeSlider.value)
                                        }

                                        AppButton {
                                            variant: "secondary"
                                            size: "md"
                                            text: qsTr("Ra cuối")
                                            tooltipText: qsTr("Áp dụng mờ dần ra cuối âm thanh")
                                            enabled: root.rackEnabled
                                            busy: controller.studioBusyKind === "fade"
                                            onClicked: controller.studioPushFade("out", fadeSlider.value)
                                        }
                                    }
                                }
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
                                variant: "quiet"
                                size: "sm"
                                iconKind: "refresh"
                                text: qsTr("Đặt lại gốc")
                                enabled: root.rackEnabled && root.ops.length > 0
                                onClicked: controller.studioReset()
                            }
                        }

                        Flow {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            AppButton {
                                objectName: "studioOpBaseChip"
                                variant: "quiet"
                                size: "sm"
                                iconKind: "check"
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
                                    variant: index === root.ops.length - 1 ? "secondary" : "quiet"
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
