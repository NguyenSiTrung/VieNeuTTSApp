// Studio — offline polish + single-segment re-gen (FR-4.3).
// Feeder pages (Tạo giọng đọc, its Tài liệu mode, Sách nói) route their
// artifact here via controller.openInStudio / openChapterInStudio, then
// switch to this destination.
//
// Layout. The header row (title, meta, undo, export) is pinned, and so is
// the timeline dock on any window tall enough for it. Under the dock only the
// history row and the clip table scroll. On a short window the dock is
// re-parented into the scrolling body instead (pinned at 640x420 it left the
// body no viewport).
// The Hiệu ứng panel stages edits without pushing them, and its apply bar
// commits them as ONE undo step. At wide widths both sit in a right-hand
// column (the panel scrolls on its own and the bar is pinned under it).
// Narrower, the same two instances are re-parented into stacked slots at the
// end of the scrolling body (CreateTab's inspector idiom), so their state and
// objectNames never fork. The apply button is the screen's only primary;
// Nghe thử is a secondary toggle.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py,
// tests/smoke/test_ui_studio.py):
//   studioTab, studioTransportDock, studioWaveform, studioDockTarget,
//   studioTimecode, studioPreviewButton, studioSeekBackButton,
//   studioSeekForwardButton, studioStopButton, studioSelectionBar,
//   studioSelectionLabel, studioTrimSelectionButton, studioCutSelectionButton,
//   studioExportButton, studioQuickExportButton, studioUndoButton,
//   studioOpHistoryCard, studioOpBaseChip, studioOpChip, studioResetButton,
//   studioResetDialog, studioResetConfirmButton, studioClipList (+ the
//   StudioClipRow names), studioRegenProfileBanner, studioRegenProfileLabel,
//   studioSwitchToRegenProfileButton, studioRegenDialog,
//   studioRegenConfirmButton, studioOpenButton, studioGuideCard,
//   studioGuideTitle, studioGuideComposeButton, studioGuideDocumentButton,
//   studioGuideAudiobookButton, studioEffectsSide, studioEffectsStackSlot,
//   studioShortcutPlay / Stop / SeekBack / SeekForward; the timeline,
//   panel and apply bar components document their own.
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

    // The Hiệu ứng column needs ~300 px beside a usable timeline.
    readonly property bool panelBeside: root.width >= 860
    readonly property int panelWidth: root.width >= 1200 ? 340 : 300
    readonly property bool shortWindow: root.height < 620
    // Below this the pinned dock would leave the body no viewport at all.
    readonly property bool dockPinned: root.height >= 520

    // ── Model shortcuts: one binding each, read by many children ───────────
    readonly property var clips: controller.studioClips || []
    // Scalars + row-level models (perf 6.4): the clip/op Repeaters bind
    // studioClipModel/studioOpModel, so an op push adds one chip instead of
    // rebuilding every clip row and chip.
    readonly property int clipCount: controller.studioClipCount
    readonly property int opCount: controller.studioOpCount

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

    // Edits need a project, a free worker and no render in flight.
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
    // Which A/B render is sounding ("" while idle or auditioning a clip).
    readonly property string playingModeText: controller.studioPlayingMode === "base"
        ? qsTr("Gốc")
        : (controller.studioPlayingMode === "pending" ? qsTr("Đã chỉnh") : "")
    readonly property string selectionRangeText: root.hasSelection
        ? qsTr("%1 – %2").arg(root.formatTime(root.selectionStart * root.dockTotalMs))
            .arg(root.formatTime(root.selectionEnd * root.dockTotalMs))
        : ""
    readonly property string monoFamily: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily

    // QUrl → local path string (same shape as CreateTab).
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
        // The picker's own effective voice: the choice, or the active
        // profile's fallback — never another engine's default voice (Task 6.3).
        return regenVoicePicker.effectiveVoice;
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

    // Empty-state links: a Tạo giọng đọc mode, then the destination (the
    // mode first, so the page lands on it in one step).
    function openCreate(mode) {
        if (typeof bridge === "undefined" || !bridge)
            return;
        bridge.setCreateMode(mode);
        bridge.setCurrentTab("create");
    }

    function openDestination(id) {
        if (typeof bridge !== "undefined" && bridge)
            bridge.setCurrentTab(id);
    }

    Connections {
        target: controller
        // The rendered mix changed, so a range drawn on the old one is stale.
        function onStudioProjectChanged() { root.clearSelection(); }
        // The dock's waveform switched between mix and clip — same reasoning.
        function onStudioAuditionChanged() { root.clearSelection(); }
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
        // The size belongs on the Dialog: an implicitWidth override on a
        // contentItem Layout re-enters the style's own implicitWidth binding
        // and QML reports a binding loop.
        width: Math.min(560, root.width - Theme.spacingLg * 2)

        background: Rectangle {
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
            radius: Theme.radiusLg
        }

        contentItem: ColumnLayout {
            spacing: Theme.spacingMd

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
        objectName: "studioResetDialog"

        anchors.centerIn: parent
        modal: true
        title: qsTr("Đặt lại về bản gốc?")
        width: Math.min(400, root.width - Theme.spacingLg * 2)

        background: Rectangle {
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
            radius: Theme.radiusLg
        }

        contentItem: ColumnLayout {
            spacing: Theme.spacingMd

            Label {
                text: qsTr("Toàn bộ %1 hiệu ứng đã áp dụng sẽ bị xoá. Âm thanh gốc vẫn được giữ nguyên.").arg(root.opCount)
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
                    objectName: "studioResetConfirmButton"
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

    // Empty-state guide card: the whole card is the tap target, the button is
    // the discoverable cue (and the 44 px keyboard/accessible target).
    component GuideCard: AppCard {
        id: guideCard

        property string heading: ""
        property string body: ""
        property string glyph: ""
        property string actionText: ""
        property string actionName: ""
        signal go()

        objectName: "studioGuideCard"
        elevation: 0
        clickable: true
        onCardClicked: guideCard.go()
        cardColor: cardHovered ? Theme.surfaceHover : Theme.surfaceAlt
        cardBorderColor: cardHovered ? Theme.border : Theme.borderSubtle
        cardRadius: Theme.radiusMd
        cardPadding: Theme.spacingMd

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Theme.spacingSm

            RowLayout {
                spacing: Theme.spacingSm
                AppIcon { kind: guideCard.glyph; iconColor: Theme.accent }
                Label {
                    objectName: "studioGuideTitle"
                    text: guideCard.heading
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    font.weight: Theme.fontWeightHeading
                }
            }

            Label {
                text: guideCard.body
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Item { Layout.fillHeight: true }

            AppButton {
                objectName: guideCard.actionName
                variant: "secondary"
                size: "sm"
                text: guideCard.actionText
                onClicked: guideCard.go()
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingMd

        // ── Header (pinned): title, project meta, undo, export ─────────────
        PageHeader {
            Layout.fillWidth: true
            title: qsTr("Studio")

            trailing: RowLayout {
                spacing: Theme.spacingXs
                visible: controller.hasStudioProject

                Label {
                    objectName: "studioProjectMeta"
                    visible: root.width >= 720
                    text: qsTr("%1 đoạn · %2").arg(root.clipCount)
                        .arg(root.formatTime(controller.studioDurationMs))
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    Layout.rightMargin: Theme.spacingSm
                }

                AppButton {
                    objectName: "studioUndoButton"
                    variant: "icon"
                    iconKind: "reset"
                    accessibleLabel: qsTr("Hoàn tác")
                    // studioCanUndo, not the op count: a revert or a reset
                    // can empty the stack and still be one undoable step.
                    // The tooltip names the step only when it is one op.
                    tooltipText: controller.studioUndoName
                        ? qsTr("Hoàn tác: %1").arg(controller.studioUndoName)
                        : qsTr("Hoàn tác")
                    enabled: root.rackEnabled && controller.studioCanUndo
                    onClicked: controller.studioUndo()
                }

                AppButton {
                    objectName: "studioQuickExportButton"
                    variant: "icon"
                    iconKind: "folder"
                    accessibleLabel: qsTr("Xuất nhanh")
                    tooltipText: qsTr("Xuất nhanh") + " — " + qsTr("xuất ngay vào thư mục đầu ra đã chọn trong Cài đặt")
                    enabled: controller.hasStudioProject && controller.exporting !== true && controller.studioBusy !== true
                    onClicked: controller.studioExport("")
                }

                AppButton {
                    objectName: "studioExportButton"
                    variant: "secondary"
                    text: qsTr("Xuất âm thanh…")
                    iconKind: "download"
                    enabled: controller.hasStudioProject && controller.exporting !== true && controller.studioBusy !== true
                    busy: controller.exporting === true
                    onClicked: root.openExportDialog()
                }
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

        // ── Empty state ────────────────────────────────────────────────────
        ScrollView {
            id: emptyScroll

            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !controller.hasStudioProject
            contentWidth: availableWidth
            clip: true

            ColumnLayout {
                width: Math.max(1, Math.min(960, emptyScroll.availableWidth))
                x: Math.max(0, (emptyScroll.availableWidth - width) / 2)
                spacing: Theme.spacingLg

                AppCard {
                    Layout.fillWidth: true
                    title: qsTr("Dự án Studio")

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingLg

                        // An artifact is ready to open.
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingMd
                            visible: controller.hasArtifact

                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Âm thanh vừa tạo đã sẵn sàng. Mở vào Studio để chỉnh âm lượng, tốc độ, khoảng lặng, mờ dần hoặc tạo lại từng đoạn.")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                                wrapMode: Text.WordWrap
                            }

                            AppButton {
                                objectName: "studioOpenButton"
                                variant: "primary"
                                size: "lg"
                                iconKind: "studio"
                                text: qsTr("Mở âm thanh vừa tạo vào Studio")
                                enabled: controller.hasArtifact && !controller.busy
                                onClicked: controller.openInStudio("text", "")
                            }
                        }

                        // Nothing to open yet: where audio is made.
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingMd
                            visible: !controller.hasArtifact

                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Chưa có âm thanh để chỉnh. Hãy tạo âm thanh ở một trong các mục dưới đây, rồi bấm “Mở trong Studio” để đưa vào đây.")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                                wrapMode: Text.WordWrap
                            }

                            // Three cards on one row, or one per row below the
                            // width where three can still hold their button —
                            // never a 2+1 wrap. Sized from the Flow's own box
                            // (the enclosing card has padding of its own);
                            // heights equalize so the row reads as one set.
                            Flow {
                                id: guideFlow

                                Layout.fillWidth: true
                                spacing: Theme.spacingMd

                                readonly property real threeAcross: (guideFlow.width - Theme.spacingMd * 2) / 3
                                readonly property real cardWidth: guideFlow.threeAcross >= 200
                                    ? guideFlow.threeAcross : guideFlow.width
                                property real cardHeight: 0

                                function measureCards() {
                                    cardHeight = Math.max(guideCompose.implicitHeight,
                                        guideDocument.implicitHeight, guideAudiobook.implicitHeight);
                                }
                                onWidthChanged: measureCards()

                                GuideCard {
                                    id: guideCompose
                                    width: guideFlow.cardWidth
                                    height: guideFlow.cardHeight > 0 ? guideFlow.cardHeight : implicitHeight
                                    onImplicitHeightChanged: guideFlow.measureCards()
                                    heading: qsTr("Tạo giọng đọc")
                                    body: qsTr("Soạn văn bản, chọn giọng và tạo nhanh từng câu.")
                                    glyph: "create"
                                    actionText: qsTr("Mở Tạo giọng đọc")
                                    actionName: "studioGuideComposeButton"
                                    onGo: root.openCreate("compose")
                                }

                                GuideCard {
                                    id: guideDocument
                                    width: guideFlow.cardWidth
                                    height: guideFlow.cardHeight > 0 ? guideFlow.cardHeight : implicitHeight
                                    onImplicitHeightChanged: guideFlow.measureCards()
                                    heading: qsTr("Tài liệu")
                                    body: qsTr("Nhập tài liệu dài, tự chia đoạn và tạo lần lượt.")
                                    glyph: "paragraph"
                                    actionText: qsTr("Mở Tài liệu")
                                    actionName: "studioGuideDocumentButton"
                                    onGo: root.openCreate("document")
                                }

                                GuideCard {
                                    id: guideAudiobook
                                    width: guideFlow.cardWidth
                                    height: guideFlow.cardHeight > 0 ? guideFlow.cardHeight : implicitHeight
                                    onImplicitHeightChanged: guideFlow.measureCards()
                                    heading: qsTr("Sách nói")
                                    body: qsTr("Nhập sách EPUB, tạo từng chương và đồng bộ chữ.")
                                    glyph: "audiobook"
                                    actionText: qsTr("Mở Sách nói")
                                    actionName: "studioGuideAudiobookButton"
                                    onGo: root.openDestination("audiobook")
                                }
                            }
                        }
                    }
                }
            }
        }

        // ── Project: timeline + body | Hiệu ứng ────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: controller.hasStudioProject
            spacing: Theme.spacingLg

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Theme.spacingMd

                // Tall windows: the timeline dock is pinned here, above the
                // scrolling body, so the transport never scrolls away from
                // the controls that change what you hear.
                Item {
                    id: dockPinnedSlot

                    Layout.fillWidth: true
                    Layout.preferredHeight: dock.implicitHeight
                    visible: root.dockPinned
                }

                // ── Scrolling body: history → clips (→ panel when stacked) ─
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

                        width: Math.max(1, bodyScroll.availableWidth)
                        spacing: Theme.spacingMd

                        // Short windows: the dock scrolls with the body (pinned,
                        // it left the body a 0 px viewport at 640x420).
                        Item {
                            id: dockScrollSlot

                            Layout.fillWidth: true
                            Layout.preferredHeight: dock.implicitHeight
                            visible: !root.dockPinned
                        }

                        // ── Engine-mismatch offer (Task 6.3) ───────────────
                        // A re-synthesis refused because the clip's audio came
                        // from another engine leaves the required profile
                        // armed here: the banner names it and the switch moves
                        // the whole app to it.
                        Rectangle {
                            objectName: "studioRegenProfileBanner"

                            Layout.fillWidth: true
                            visible: controller.studioRegenProfile !== ""
                            implicitHeight: mismatchRow.implicitHeight + Theme.spacingMd * 2
                            radius: Theme.radiusMd
                            color: Theme.warningSubtle
                            border.color: Theme.warningText
                            border.width: 1

                            RowLayout {
                                id: mismatchRow

                                anchors.fill: parent
                                anchors.margins: Theme.spacingMd
                                spacing: Theme.spacingMd

                                AppIcon {
                                    Layout.alignment: Qt.AlignVCenter
                                    kind: "wave"
                                    width: 18
                                    height: 18
                                    iconColor: Theme.warningText
                                }

                                Label {
                                    objectName: "studioRegenProfileLabel"

                                    Layout.fillWidth: true
                                    text: qsTr("Đoạn này được tạo bằng %1. Chuyển sang hồ sơ đó để tạo lại.")
                                        .arg(controller.studioRegenProfileLabel)
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    wrapMode: Text.Wrap
                                }

                                AppButton {
                                    objectName: "studioSwitchToRegenProfileButton"

                                    variant: "secondary"
                                    size: "sm"
                                    text: qsTr("Chuyển sang %1").arg(controller.studioRegenProfileLabel)
                                    enabled: !controller.busy
                                    onClicked: controller.studioSwitchToRegenProfile()
                                }
                            }
                        }

                        // ── History: Bản gốc → step → step … ───────────────
                        // Chips are buttons: clicking one drops every step
                        // after it. The current step carries the selected look.
                        AppCard {
                            objectName: "studioOpHistoryCard"
                            Layout.fillWidth: true
                            cardPadding: Theme.spacingSm
                            elevation: 0

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingSm

                                Flow {
                                    Layout.fillWidth: true
                                    spacing: Theme.spacingXs

                                    Label {
                                        height: Theme.controlHitTarget
                                        verticalAlignment: Text.AlignVCenter
                                        rightPadding: Theme.spacingXs
                                        text: qsTr("Lịch sử")
                                        color: Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeSm
                                        font.weight: Theme.fontWeightMedium
                                    }

                                    AppButton {
                                        objectName: "studioOpBaseChip"
                                        variant: "chip"
                                        size: "sm"
                                        checked: root.opCount === 0
                                        text: qsTr("Bản gốc")
                                        tooltipText: qsTr("Quay lại âm thanh gốc, chưa áp dụng hiệu ứng nào")
                                        enabled: root.rackEnabled
                                        onClicked: controller.studioRevertTo(-1)
                                    }

                                    Repeater {
                                        model: controller.studioOpModel

                                        Row {
                                            id: opStep

                                            required property var modelData
                                            required property int index

                                            spacing: Theme.spacingXs

                                            Label {
                                                height: Theme.controlHitTarget
                                                verticalAlignment: Text.AlignVCenter
                                                text: "→"
                                                color: Theme.textSubtle
                                                font.family: Theme.fontFamily
                                                font.pixelSize: Theme.fontSizeSm
                                                Accessible.ignored: true
                                            }

                                            AppButton {
                                                objectName: "studioOpChip"
                                                variant: "chip"
                                                size: "sm"
                                                checked: opStep.index === root.opCount - 1
                                                text: opStep.modelData.desc || opStep.modelData.name || ""
                                                tooltipText: qsTr("Quay lại bước %1").arg(opStep.index + 1)
                                                enabled: root.rackEnabled
                                                onClicked: controller.studioRevertTo(opStep.index)
                                            }
                                        }
                                    }
                                }

                                AppButton {
                                    objectName: "studioResetButton"
                                    Layout.alignment: Qt.AlignTop
                                    variant: "quiet"
                                    size: "sm"
                                    text: qsTr("Đặt lại gốc")
                                    tooltipText: qsTr("Xoá toàn bộ hiệu ứng đã áp dụng, quay về âm thanh gốc")
                                    enabled: root.rackEnabled && root.opCount > 0
                                    onClicked: resetDialog.open()
                                }
                            }
                        }

                        // ── Clip table ─────────────────────────────────────
                        // Plain rows, no inner ScrollView (a nested flickable
                        // swallows the page's wheel events).
                        AppCard {
                            Layout.fillWidth: true
                            title: qsTr("Các đoạn")
                            badgeText: qsTr("%1 đoạn").arg(root.clipCount)
                            cardPadding: Theme.spacingMd

                            ColumnLayout {
                                objectName: "studioClipList"
                                Layout.fillWidth: true
                                spacing: Theme.spacingXxs

                                Repeater {
                                    model: controller.studioClipModel

                                    StudioClipRow {
                                        Layout.fillWidth: true
                                        clipsCount: root.clipCount
                                        auditionClipId: root.auditionClipId
                                        showDuration: bodyScroll.availableWidth >= 560
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

                            Label {
                                Layout.fillWidth: true
                                visible: root.clipCount === 1
                                text: qsTr("Âm thanh hiện tại gồm 1 đoạn duy nhất. Bấm Tạo lại để thay đổi giọng đọc hoặc sửa lại văn bản cho đoạn này.")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                                wrapMode: Text.WordWrap
                            }
                        }

                        // Narrow windows: the Hiệu ứng panel and its apply bar
                        // stack here and scroll with the body.
                        Item {
                            id: effectsStackSlot

                            objectName: "studioEffectsStackSlot"
                            Layout.fillWidth: true
                            Layout.preferredHeight: effectsPanel.implicitHeight
                            visible: !root.panelBeside
                        }

                        Item {
                            id: applyStackSlot

                            Layout.fillWidth: true
                            Layout.preferredHeight: applyBar.implicitHeight
                            visible: !root.panelBeside
                        }
                    }
                }
            }

            // Wide windows: the Hiệu ứng column — the panel scrolls on its own
            // when the window is short, the apply bar stays pinned under it.
            ColumnLayout {
                Layout.fillHeight: true
                Layout.preferredWidth: root.panelWidth
                Layout.maximumWidth: root.panelWidth
                visible: root.panelBeside
                spacing: Theme.spacingSm

                Flickable {
                    id: effectsSide

                    objectName: "studioEffectsSide"
                    Layout.fillWidth: true
                    // Its full height when the window allows; shrinks (and
                    // scrolls) when it does not. The spacer below takes the
                    // rest, so the apply bar sits right under the panel.
                    Layout.fillHeight: true
                    Layout.preferredHeight: effectsPanel.implicitHeight
                    Layout.maximumHeight: effectsPanel.implicitHeight
                    contentWidth: width
                    contentHeight: effectsPanel.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    visible: root.panelBeside

                    ScrollBar.vertical: ScrollBar {
                        policy: ScrollBar.AsNeeded
                        opacity: size < 1.0 ? 1.0 : 0.0
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
                }

                Item {
                    id: applySideSlot

                    Layout.fillWidth: true
                    Layout.preferredHeight: applyBar.implicitHeight
                }

                Item {
                    Layout.fillHeight: true
                    Layout.preferredHeight: 0
                }
            }
        }
    }

    // ── Timeline dock ──────────────────────────────────────────────────────
    // The one place that auditions the mix. It describes whatever
    // it is pointed at — the whole mix or a single clip — so the
    // timecode, the waveform and the highlighted block/row never
    // disagree.
    AppCard {
        id: dock

        objectName: "studioTransportDock"
        parent: root.dockPinned ? dockPinnedSlot : dockScrollSlot
        x: 0
        y: 0
        width: parent ? parent.width : 0
        height: implicitHeight
        cardPadding: Theme.spacingMd

        StudioTimeline {
            Layout.fillWidth: true
            clipModel: controller.studioClipModel
            clips: root.clips
            auditionClipId: root.auditionClipId
            totalMs: root.dockTotalMs
            envelope: root.auditioningClip ? controller.studioClipEnvelope : controller.studioEnvelope
            position: controller.replayPosition
            active: controller.replayActive
            // Seeking only means something while a replay is live;
            // selecting a range works idle too (that is how a trim
            // is drawn). Both are off while a clip is auditioned,
            // whose fractions do not describe the mix.
            seekable: controller.hasStudioProject && !root.auditioningClip
                && controller.replayActive && root.dockTotalMs > 0
            selectable: controller.hasStudioProject && !root.auditioningClip
                && root.dockTotalMs > 0
            selectionStart: root.selectionStart
            selectionEnd: root.selectionEnd
            waveformHeight: root.shortWindow ? 44 : 64
            onSeekRequested: (fraction) => controller.seekReplay(fraction)
            onRangeSelected: (start, end) => {
                root.selectionStart = start;
                root.selectionEnd = end;
            }
            onRangeCleared: root.clearSelection()
        }

        // Transport. A Flow so the cluster wraps at the 640 px
        // minimum instead of overflowing the dock.
        Flow {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            AppButton {
                objectName: "studioSeekBackButton"
                variant: "icon"
                iconKind: "previous"
                accessibleLabel: qsTr("Lùi 5 giây")
                tooltipText: qsTr("Lùi 5 giây") + " (←)"
                enabled: controller.replayActive && root.dockTotalMs > 0
                onClicked: root.seekBy(-5000)
            }

            // Play/pause: a secondary toggle (selected while it
            // sounds) — the screen's one primary is Áp dụng.
            AppButton {
                objectName: "studioPreviewButton"
                variant: "secondary"
                checked: controller.replayActive && !controller.replayPaused
                text: controller.replayActive
                    ? (controller.replayPaused ? qsTr("Tiếp tục") : qsTr("Tạm dừng"))
                    : qsTr("Nghe thử")
                iconKind: controller.replayActive && !controller.replayPaused ? "pause" : "play"
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
                iconKind: "next"
                accessibleLabel: qsTr("Tiến 5 giây")
                tooltipText: qsTr("Tiến 5 giây") + " (→)"
                enabled: controller.replayActive && root.dockTotalMs > 0
                onClicked: root.seekBy(5000)
            }

            AppButton {
                objectName: "studioStopButton"
                variant: "icon"
                iconKind: "stop"
                accessibleLabel: qsTr("Dừng")
                tooltipText: qsTr("Dừng") + " (Esc)"
                enabled: controller.replayActive
                onClicked: controller.stopReplay()
            }

            AppButton {
                objectName: "studioReplayButton"
                variant: "icon"
                iconKind: "reset"
                accessibleLabel: qsTr("Phát lại từ đầu")
                tooltipText: qsTr("Dừng và phát lại từ đầu dự án")
                enabled: controller.hasStudioProject && !controller.busy && controller.studioBusy !== true
                onClicked: {
                    controller.stopReplay();
                    controller.studioPreview();
                }
            }

            // Timecode + what the transport is pointed at.
            RowLayout {
                height: Theme.controlHitTarget
                spacing: Theme.spacingSm

                Label {
                    objectName: "studioTimecode"
                    text: root.formatTime(controller.replayActive
                        ? Math.round(controller.replayPosition * root.dockTotalMs) : 0)
                        + " / " + root.formatTime(root.dockTotalMs)
                    color: controller.replayActive ? Theme.accent : Theme.text
                    font.family: root.monoFamily
                    font.pixelSize: Theme.fontSizeSm
                    font.weight: Theme.fontWeightMedium
                }

                // Idle reads neutral, live reads accent.
                Rectangle {
                    width: 8
                    height: 8
                    radius: 4
                    color: controller.replayActive ? Theme.accent : Theme.textSubtle
                }

                Label {
                    objectName: "studioDockState"
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
                    text: root.dockTargetText
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }

                // The A/B render that is sounding (Gốc / Đã chỉnh).
                Rectangle {
                    objectName: "studioPlayingModeTag"
                    visible: root.playingModeText !== ""
                    radius: Theme.radiusPill
                    color: Theme.accentSubtle
                    implicitHeight: 22
                    implicitWidth: playingModeLabel.implicitWidth + Theme.spacingMd

                    Label {
                        id: playingModeLabel
                        anchors.centerIn: parent
                        text: root.playingModeText
                        color: Theme.accent
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                    }
                }
            }
        }

        // Range toolbar: only while a range exists; it names the
        // range in seconds because the two buttons edit the mix.
        Flow {
            objectName: "studioSelectionBar"

            Layout.fillWidth: true
            spacing: Theme.spacingSm
            visible: root.hasSelection

            Label {
                objectName: "studioSelectionLabel"
                height: Theme.controlHitTarget
                verticalAlignment: Text.AlignVCenter
                text: qsTr("Vùng chọn: %1").arg(root.selectionRangeText)
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightMedium
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

            // Cut removes audio from the mix — danger styling so
            // the destructive half of the pair never reads as a
            // sibling of the safe keep.
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
    }

    // ONE panel and ONE apply bar, re-parented between the wide column and
    // the stacked slots, so their state and objectNames never fork.
    StudioEffectsPanel {
        id: effectsPanel

        parent: root.panelBeside ? effectsSide.contentItem : effectsStackSlot
        x: 0
        y: 0
        width: parent ? parent.width : 0
        height: implicitHeight
        rackEnabled: root.rackEnabled
    }

    StudioApplyBar {
        id: applyBar

        parent: root.panelBeside ? applySideSlot : applyStackSlot
        x: 0
        y: 0
        width: parent ? parent.width : 0
        height: implicitHeight
        rackEnabled: root.rackEnabled
    }
}
