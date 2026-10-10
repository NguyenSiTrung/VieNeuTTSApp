// Settings tab (FR-3.5, FR-4.2): the page shell — header with the settings
// filter, a sub-navigation of six sections and ONE scroll area showing the
// selected section. Each section is its own file under settings/:
//   Chung              settings/SettingsGeneral.qml  (settingsSection_general)
//   Giọng & nhịp đọc   settings/SettingsVoice.qml    (settingsSection_voice)
//   Xuất tệp           settings/SettingsExport.qml   (settingsSection_export)
//   Engine & phần cứng settings/SettingsEngine.qml   (settingsSection_engine)
//   Mô hình            settings/SettingsModels.qml   (settingsSection_models)
//   Cập nhật           settings/SettingsUpdates.qml  (settingsSection_updates)
// Every section stays instantiated (only visibility follows the selection),
// so the objectNames below always resolve; a hidden section's controls
// report visible == false until it is selected (currentSection) or, while
// the filter holds text, until one of its rows matches.
//
// Engine/output settings flow through the `controller` seam (validated +
// persisted, invalid writes become errorText); the Color mode control writes
// `bridge.themePreference` (live switch persisted to the same settings field).
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// settingsTab, backendCombo, detectedEngineLabel, precisionCombo,
// settingsDefaultVoiceValue, settingsDefaultVoiceLink (FR-3.6: the default
// voice is read-only here and chosen in Giọng đọc), defaultVoiceNote,
// outputDirLabel, outputDirBrowseButton, outputDirDialog, temperatureSpin,
// themeCombo, languageCombo, errorLabel, checkUpdatesButton,
// downloadUpdateButton, viewReleaseButton, otherPlatformsToggle,
// otherPlatformsList, updateBanner, updateErrorLabel.
// Page shell: settingsFilterField, settingsFilterClearButton,
// settingsFilterEmpty, settingsSectionNav (currentSection),
// settingsNavButton_<general|voice|export|engine|models|updates>,
// settingsSection_<id>, pageScrollView (the only scroller).
// Dialogs owned here: outputDirDialog, cudaRuntimeRemoveDialog /
// cudaRuntimeRemoveConfirmButton, QwenSetupDialog. Each section file's
// header lists its own names (CUDA, Qwen, model source, updates…).
//
// API for sections (`page`): currentSection, filterText, filterActive,
// isCompact, the option arrays, the CUDA/Qwen state aliases,
// cudaRuntimeExpanded / cudaRuntimeDetailsExpanded (+ their auto-expand
// rules), openOutputDirDialog(), openCudaRemoveDialog(), openQwenSetup(),
// requestQwenSetup(), valueIndex(), formatBytes(), defaultVoiceName().
// Main.qml calls jumpToSection("updates") from the status-bar update link.
// The FolderDialog is authored but NOT exercised offscreen (native dialogs
// are unreliable headless); the tested seam is setOutputDir(path).
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "components"
import "settings"
import "."

pragma ComponentBehavior: Bound

Pane {
    id: root

    objectName: "settingsTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.bg
    }

    // ── page state ───────────────────────────────────────────────────────
    readonly property var sections: [
        { id: "general", label: qsTr("Chung") },
        { id: "voice", label: qsTr("Giọng & nhịp đọc") },
        { id: "export", label: qsTr("Xuất tệp") },
        { id: "engine", label: qsTr("Engine & phần cứng") },
        { id: "models", label: qsTr("Mô hình") },
        { id: "updates", label: qsTr("Cập nhật") }
    ]
    property string currentSection: "general"
    property string filterText: ""
    readonly property bool filterActive: root.filterText.trim() !== ""
    readonly property bool filterEmpty: root.filterActive
        && !general.hasMatch && !voice.hasMatch && !exportSection.hasMatch
        && !engine.hasMatch && !models.hasMatch && !updates.hasMatch
    // Below this width the sub-navigation turns into a chip row above the
    // content instead of a column beside it.
    readonly property bool navStacked: root.availableWidth < 760

    onCurrentSectionChanged: settingsScroll.scrollToTop()

    // Select a section (sub-nav, Main.qml's update link). Clears the filter
    // so the selected section is what the page shows.
    function jumpToSection(sectionId) {
        for (let i = 0; i < root.sections.length; i++) {
            if (root.sections[i].id === sectionId) {
                filterField.text = "";
                root.currentSection = sectionId;
                return;
            }
        }
    }

    // Precision choices mirror Settings._PRECISIONS (ONNX-only; the torch
    // path ignores precision).
    readonly property var precisionOptions: [
        { value: "int8", label: qsTr("int8 — nhanh (mặc định)") },
        { value: "fp32", label: qsTr("fp32 — chất lượng tối đa") }
    ]

    // Export container choices mirror Settings._EXPORT_FORMATS in
    // core/models.py. Batch + audiobook outputs and Save-dialog defaults use
    // this (quick-export stays WAV); Save dialogs still pick per file.
    readonly property var exportFormatOptions: [
        { value: "wav", label: qsTr("WAV — chuẩn, dung lượng lớn") },
        { value: "mp3", label: qsTr("MP3 — gọn nhẹ") }
    ]

    // Short labels: they sit side by side in the Color mode segmented control.
    readonly property var themeOptions: [
        { value: "system", label: qsTr("Hệ thống") },
        { value: "light", label: qsTr("Sáng") },
        { value: "dark", label: qsTr("Tối") }
    ]

    // Language names stay in their native form (standard practice — each
    // name is readable by its own speakers); only the "system" row label
    // is translatable. Values mirror Settings._LANGUAGES in core/models.py.
    readonly property var languageOptions: [
        { value: "system", label: qsTr("Theo hệ điều hành") },
        { value: "vi", label: "Tiếng Việt" },
        { value: "en", label: "English" }
    ]

    // Responsive breakpoint — when the pane narrows below ~640 px the
    // setting rows stack vertically (label on top, control full-width
    // below) so no ComboBox text is truncated (the “Tự động …” cut in
    // the screenshot was caused by a fixed 260 px control fighting a
    // flexible label in a RowLayout at ~400 px available width).
    readonly property bool isCompact: settingsScroll.width < 600

    readonly property bool cudaRuntimeSupported: controller
        ? controller.cudaRuntimeSupported : false
    readonly property bool cudaRuntimeDriverChecked: controller
        ? controller.cudaRuntimeDriverChecked : false
    readonly property bool cudaRuntimeDriverReady: controller
        ? controller.cudaRuntimeDriverReady : false
    readonly property bool cudaRuntimeInstallAllowed: controller
        ? controller.cudaRuntimeInstallAllowed : false
    readonly property string cudaRuntimeState: controller
        ? controller.cudaRuntimeState : "unavailable"
    readonly property int localCudaRuntimeCount: controller
        ? controller.localCudaRuntimes.length : 0
    property bool localCudaRuntimeScanRequested: false

    // ── Qwen engine (Task 6.1): compute device + managed runtime + checkpoints
    // State is controller truth; these aliases guard the `controller` context
    // property only (a bare controller must not break the page).
    readonly property bool qwenRuntimeSupported: controller
        ? controller.qwenRuntimeSupported : false

    readonly property bool qwenProfileActive: controller
        ? controller.engineProfileIsQwen : false
    property bool qwenSetupAutoPending: false
    readonly property bool qwenSetupNeeded: root.qwenProfileActive
        && controller && !controller.profileReady

    // Human-readable byte size. The pinned Qwen installs are GB-scale, so raw
    // counts are unreadable; String() stays the fallback for exact counts.
    // Display name of the default voice ("Ngọc Huyền — Nữ · Bắc · …" →
    // "Ngọc Huyền"); the raw id when the catalog does not list it.
    function defaultVoiceName() {
        const id = (typeof controller !== "undefined" && controller) ? controller.defaultVoice : "";
        const groups = EngineState.voiceGroups;
        for (let i = 0; i < groups.length; i++)
            for (let j = 0; j < groups[i].voices.length; j++)
                if (groups[i].voices[j].id === id)
                    return String(groups[i].voices[j].label || id).split(" — ")[0].trim();
        return id;
    }

    function formatBytes(bytes) {
        const value = Number(bytes);
        if (!isFinite(value) || value <= 0)
            return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let index = 0;
        let scaled = value;
        while (scaled >= 1024 && index < units.length - 1) {
            scaled /= 1024;
            index++;
        }
        const digits = (index === 0 || scaled >= 100) ? 0 : 1;
        return scaled.toFixed(digits) + " " + units[index];
    }

    function requestQwenSetup(profileId) {
        if (!profileId || !String(profileId).startsWith("qwen"))
            return;
        root.qwenSetupAutoPending = true;
        Qt.callLater(root.maybeOpenQwenSetup);
    }

    function maybeOpenQwenSetup() {
        if (!root.qwenSetupAutoPending)
            return;
        if (!root.qwenProfileActive || !controller || controller.profileReady) {
            root.qwenSetupAutoPending = false;
            return;
        }
        if ((controller ? controller.profileModelState : "") === "checking"
                || (controller ? controller.profileRuntimeState : "") === "checking")
            return;
        const fallback = root.readySiblingQwenQuantization();
        if (fallback !== "" && controller.setQwenVariant("gguf", fallback))
            return;
        root.qwenSetupAutoPending = false;
        qwenSetupDialog.open();
    }

    function readySiblingQwenQuantization() {
        if (!controller || controller.qwenModelFormat !== "gguf")
            return "";
        const rows = controller.qwenModels || [];
        let selectedReady = false;
        let fallback = "";
        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            if (!row.isActive)
                continue;
            if (row.isSelected && row.ready)
                selectedReady = true;
            else if (!row.isSelected && row.ready && fallback === "")
                fallback = row.quantization;
        }
        return selectedReady ? "" : fallback;
    }

    // bridge.ENGINE_NOTE_PENDING ("…") until the deferred hardware probe
    // lands; QML cannot import the Python constant — keep the two in sync.
    readonly property string engineNoteText: bridge ? bridge.engineNote : ""
    readonly property bool engineNoteReady: engineNoteText !== ""
        && engineNoteText !== "…"

    // CUDA diagnostics disclosure: verbose guidance and the diagnostic scan
    // start collapsed and auto-expand when a state actually needs them —
    // install in flight or failed, an unusable driver, or a scan already run.
    property bool cudaRuntimeDetailsExpanded: false
    readonly property bool cudaRuntimeDetailsWanted: root.cudaRuntimeState === "downloading"
        || root.cudaRuntimeState === "validating"
        || root.cudaRuntimeState === "failed"
        || (root.cudaRuntimeDriverChecked && !root.cudaRuntimeDriverReady)
        || root.localCudaRuntimeScanRequested
    onCudaRuntimeDetailsWantedChanged: if (cudaRuntimeDetailsWanted) cudaRuntimeDetailsExpanded = true

    // Managed CUDA runtime row (Engine › Nâng cao): collapsed by default,
    // auto-expands while an install is in flight or failed and when the
    // driver is unusable (every actionable CUDA notice lives inside it); a
    // manual collapse sticks until the next state that wants it.
    property bool cudaRuntimeExpanded: false
    readonly property bool cudaRuntimeWanted: root.cudaRuntimeState === "downloading"
        || root.cudaRuntimeState === "validating"
        || root.cudaRuntimeState === "failed"
        || (root.cudaRuntimeDriverChecked && !root.cudaRuntimeDriverReady)
    onCudaRuntimeWantedChanged: if (cudaRuntimeWanted) cudaRuntimeExpanded = true

    Component.onCompleted: {
        cudaRuntimeDetailsExpanded = cudaRuntimeDetailsWanted;
        cudaRuntimeExpanded = cudaRuntimeWanted;
    }

    function openOutputDirDialog() {
        if (controller.outputDir !== "") {
            const folder = root.toFolderUrl(controller.outputDir);
            if (folder !== "")
                outputDirDialog.currentFolder = folder;
        }
        outputDirDialog.open();
    }

    function openCudaRemoveDialog() {
        cudaRuntimeRemoveDialog.open();
    }

    function openQwenSetup() {
        qwenSetupDialog.open();
    }

    // Tested seam for the folder dialog (native dialogs are unreliable
    // headless; the dialog's onAccepted just calls this).
    function setOutputDir(path) {
        controller.outputDir = path
    }

    function valueIndex(options, value) {
        for (let i = 0; i < options.length; i++)
            if (options[i].value === value)
                return i;
        return 0;
    }

    // QUrl → local path string (same helper idiom as the other tabs).
    function toLocalPath(url) {
        const s = url.toString();
        if (!s.startsWith("file://"))
            return s;
        let path = decodeURIComponent(s.substring(7));
        // Windows: toString() is file:///C:/... — drop the stray slash the
        // empty host slot leaves before the drive letter, or downstream
        // slots receive /C:/... and every filesystem call fails.
        if (/^\/[A-Za-z]:\//.test(path))
            path = path.substring(1);
        return path;
    }

    // Local path string → valid QUrl string for FolderDialog currentFolder
    function toFolderUrl(path) {
        if (!path || path.trim() === "")
            return "";
        if (typeof controller !== "undefined" && controller && typeof controller.pathToUrl === "function") {
            const u = controller.pathToUrl(path);
            if (u !== "")
                return u;
        }
        if (path.startsWith("file://"))
            return path;
        const clean = path.replace(/\\/g, "/");
        if (/^[A-Za-z]:\//.test(clean))
            return "file:///" + clean;
        if (clean.startsWith("//"))
            return "file:" + clean;
        if (clean.startsWith("/"))
            return "file://" + clean;
        return "file:///" + clean;
    }


    FolderDialog {
        id: outputDirDialog

        objectName: "outputDirDialog"
        title: qsTr("Chọn thư mục xuất âm thanh")
        onAccepted: root.setOutputDir(root.toLocalPath(outputDirDialog.selectedFolder))
    }

    RemoveConfirmDialog {
        id: cudaRuntimeRemoveDialog

        objectName: "cudaRuntimeRemoveDialog"
        title: qsTr("Gỡ runtime CUDA?")
        body: qsTr("Toàn bộ tệp đã tải sẽ bị xóa khỏi máy. Bạn sẽ cần tải lại để dùng lại.")
        confirmLabel: qsTr("Gỡ runtime")
        confirmObjectName: "cudaRuntimeRemoveConfirmButton"
        onConfirmed: controller.removeCudaRuntime()
    }

    QwenSetupDialog {
        id: qwenSetupDialog

        isCompact: root.isCompact
        onOpened: root.qwenSetupAutoPending = false
    }

    Connections {
        target: (typeof controller !== "undefined" && controller) ? controller : null

        function onProfileModelChanged() { root.maybeOpenQwenSetup(); }
        function onProfileRuntimeChanged() { root.maybeOpenQwenSetup(); }
        function onProfileReadyChanged() { root.maybeOpenQwenSetup(); }
    }

    // Sub-navigation entry: a full-width row in the side column, a chip in
    // the stacked (narrow) layout. `checked` marks the selected section.
    component SettingsNavButton: AbstractButton {
        id: navButton

        required property var modelData

        objectName: "settingsNavButton_" + modelData.id
        checked: !root.filterActive && root.currentSection === modelData.id
        text: modelData.label
        width: root.navStacked ? implicitWidth : (parent ? parent.width : implicitWidth)
        implicitWidth: navLabel.implicitWidth
        implicitHeight: Theme.controlHitTarget
        focusPolicy: Qt.StrongFocus
        hoverEnabled: true
        onClicked: root.jumpToSection(modelData.id)

        Accessible.role: Accessible.PageTab
        Accessible.name: text

        background: Rectangle {
            radius: root.navStacked ? Theme.radiusPill : Theme.radiusMd
            color: navButton.checked ? Theme.accentSubtle
                : (navButton.hovered ? Theme.surfaceHover : "transparent")
            border.width: navButton.visualFocus ? Theme.focusRingWidth
                : (root.navStacked ? 1 : 0)
            border.color: navButton.visualFocus || navButton.checked ? Theme.accent : Theme.borderSubtle
        }

        contentItem: Label {
            id: navLabel

            text: navButton.text
            color: navButton.checked ? Theme.accent : Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            font.weight: navButton.checked ? Theme.fontWeightBold : Theme.fontWeightMedium
            horizontalAlignment: root.navStacked ? Text.AlignHCenter : Text.AlignLeft
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            leftPadding: Theme.spacingMd
            rightPadding: Theme.spacingMd
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingLg

        PageHeader {
            Layout.fillWidth: true
            title: qsTr("Cài đặt")
            trailing: Rectangle {
                implicitWidth: Math.min(320, Math.max(200, root.availableWidth * 0.4))
                implicitHeight: Theme.controlHitTarget
                radius: Theme.radiusMd
                color: Theme.surfaceAlt
                border.width: filterField.activeFocus ? Theme.focusRingWidth : 1
                border.color: filterField.activeFocus ? Theme.accent : Theme.borderSubtle

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Theme.spacingMd
                    anchors.rightMargin: Theme.spacingXs
                    spacing: Theme.spacingSm

                    AppIcon {
                        width: 16
                        height: 16
                        kind: "search"
                        iconColor: filterField.activeFocus ? Theme.accent : Theme.textSubtle
                    }

                    TextField {
                        id: filterField

                        objectName: "settingsFilterField"
                        Layout.fillWidth: true
                        placeholderText: qsTr("Tìm cài đặt (CUDA, giọng mặc định…)")
                        placeholderTextColor: Theme.textSubtle
                        color: Theme.text
                        selectedTextColor: Theme.accentText
                        selectionColor: Theme.accent
                        selectByMouse: true
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        padding: 0
                        background: null
                        Accessible.name: qsTr("Tìm cài đặt")
                        onTextChanged: root.filterText = text
                    }

                    AppIconButton {
                        objectName: "settingsFilterClearButton"
                        visible: filterField.text !== ""
                        size: "sm"
                        iconKind: "close"
                        tooltipText: qsTr("Xóa tìm kiếm")
                        accessibleLabel: tooltipText
                        onClicked: filterField.text = ""
                    }
                }
            }
        }

        // Sub-nav + content. The nav never scrolls (six short rows); the
        // PageShell is the page's only scroller.
        GridLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: root.navStacked ? 1 : 2
            columnSpacing: Theme.spacingXl
            rowSpacing: Theme.spacingMd

            Flow {
                id: sectionNav

                objectName: "settingsSectionNav"
                // Exposed for the smoke tests (the buttons are Repeater
                // delegates).
                property string currentSection: root.currentSection
                Layout.fillWidth: root.navStacked
                Layout.preferredWidth: root.navStacked ? -1 : 200
                Layout.alignment: Qt.AlignTop
                spacing: root.navStacked ? Theme.spacingSm : Theme.spacingXxs

                Repeater {
                    model: root.sections

                    delegate: SettingsNavButton {}
                }
            }

            PageShell {
                id: settingsScroll

                Layout.fillWidth: true
                Layout.fillHeight: true
                maxWidth: 760

                Label {
                    objectName: "settingsFilterEmpty"
                    Layout.fillWidth: true
                    visible: root.filterEmpty
                    text: qsTr("Không có cài đặt nào khớp với “%1”.").arg(root.filterText.trim())
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    wrapMode: Text.Wrap
                }

                SettingsGeneral {
                    id: general

                    page: root
                }

                SettingsVoice {
                    id: voice

                    page: root
                }

                SettingsExport {
                    id: exportSection

                    page: root
                }

                SettingsEngine {
                    id: engine

                    page: root
                }

                SettingsModels {
                    id: models

                    page: root
                }

                SettingsUpdates {
                    id: updates

                    page: root
                }

                // Error notice banner
                AppNotice {
                    Layout.fillWidth: true
                    tone: "error"
                    title: qsTr("Không thể lưu cài đặt")
                    message: controller.errorText
                    messageObjectName: "errorLabel"
                    visible: controller.errorText !== ""
                }
            }
        }
    }
}
