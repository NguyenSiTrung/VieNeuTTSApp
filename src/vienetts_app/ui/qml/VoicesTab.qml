// Giọng đọc destination (ui_shell_redesign FR-3.5 / AC-5): the voice library
// and, as its `clone` view, the existing cloning flow. `bridge.voicesView`
// picks the view ("library" | "clone"); both pages stay built, so every
// cloning objectName resolves whichever view is shown.
//
//   library   preset voices grouped by region (Miền Bắc/Trung/Nam) with
//             gender/style filters and an accent-insensitive search; each row
//             auditions (controller.auditionVoice) and can become the default
//             voice ("Đặt làm mặc định" writes controller.defaultVoice); the
//             cloned voices follow in their own section. "Tạo giọng mới"
//             opens the clone view.
//   clone     CloningTab (consent notice, enrollment, clone list) with its
//             header breadcrumb "‹ Giọng đọc" leading back to the library.
//
// Pick mode (`pickForCreate`, set by Main.qml when Tạo giọng đọc's "Đổi
// giọng…" opens this page, FR-3.3): a banner says what is being chosen and
// every row offers "Dùng giọng này" (voicesRowUse) instead of the default
// action; choosing raises `useVoiceRequested(id)` and the host selects it in
// the Create dock. Plain navigation (the sidebar) never shows it.
//
// The catalog is the ACTIVE profile's own, read from EngineState.voiceGroups
// (the capability table's voices_source — the same source VoicePicker and
// AppController._active_voice_rows() use): VieNeu's region-grouped presets
// plus its clones, Qwen CustomVoice's pinned speakers, Qwen Base's enrolled
// clones. The default voice is VieNeu's setting: under another profile the
// action is hidden and EngineState.defaultVoiceNote says why.
//
// One scroll surface per view (PageShell); groups are Repeaters, never nested
// ListViews, so the wheel never stalls in an inner flickable.
//
// objectNames (tested contract, tests/smoke/test_ui_tabs.py): voicesTab,
// voicesLibrary, voicesCreateButton,
// voicesDefaultSummary, voicesDefaultNote, voicesSearchField,
// voicesGenderFilter (segments voicesGenderFilter_<all|Nam|Nữ>),
// voicesStyleChip, voicesGroup, voicesRow, voicesRowName, voicesRowPersona,
// voicesRowAudition, voicesRowSetDefault, voicesClonedSection,
// voicesClonedEmpty, voicesNoResults, voicesClearFilters, voicesEmptyNotice,
// voicesPickBanner, voicesPickCancel, voicesRowUse; voicesBackButton lives in
// CloningTab's header.
//
// The group label "Đã sao chép" mirrors CLONED_GROUP in ui/controller.py —
// QML cannot import Python constants; keep the two in sync.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "components"
import "."

Item {
    id: root

    objectName: "voicesTab"

    readonly property bool hasController: typeof controller !== "undefined" && controller !== null
    readonly property bool hasBridge: typeof bridge !== "undefined" && bridge !== null
    readonly property string view: hasBridge ? bridge.voicesView : "library"

    // ── pick mode (host-driven) ───────────────────────────────────────────
    property bool pickForCreate: false
    // The voice the Create dock uses now (its row reads "Đang dùng").
    property string createVoiceId: ""
    signal useVoiceRequested(string voiceId)
    signal pickCancelled()

    // ── filter state ──────────────────────────────────────────────────────
    property string genderFilter: "all"
    property string styleFilter: ""
    property string searchText: ""
    readonly property string searchKey: normalize(searchText.trim())
    readonly property bool filtering: genderFilter !== "all" || styleFilter !== ""
        || searchKey !== ""

    // ── catalog ───────────────────────────────────────────────────────────
    readonly property var presetGroups: {
        const groups = EngineState.voiceGroups;
        const result = [];
        for (let i = 0; i < groups.length; i++) {
            if (isClonedGroup(groups[i]))
                continue;
            const key = String(groups[i].id || groups[i].label || "");
            const rows = [];
            for (let j = 0; j < groups[i].voices.length; j++)
                rows.push(rowInfo(groups[i].voices[j], false));
            result.push({ "key": key, "title": groupTitle(key, groups[i].label), "rows": rows });
        }
        return result;
    }
    readonly property var cloneRows: {
        const groups = EngineState.voiceGroups;
        const rows = [];
        for (let i = 0; i < groups.length; i++) {
            if (!isClonedGroup(groups[i]))
                continue;
            for (let j = 0; j < groups[i].voices.length; j++)
                rows.push(rowInfo(groups[i].voices[j], true));
        }
        return rows;
    }
    readonly property var allPresetRows: {
        const rows = [];
        for (let i = 0; i < presetGroups.length; i++)
            for (let j = 0; j < presetGroups[i].rows.length; j++)
                rows.push(presetGroups[i].rows[j]);
        return rows;
    }
    readonly property bool hasGenders: allPresetRows.some(row => row.gender !== "")
    // The catalog's own styles, in first-seen order ("" = every style).
    readonly property var styleKeys: {
        const keys = [""];
        for (let i = 0; i < allPresetRows.length; i++) {
            const style = allPresetRows[i].style;
            if (style !== "" && keys.indexOf(style) < 0)
                keys.push(style);
        }
        return keys;
    }

    readonly property var shownGroups: {
        const result = [];
        for (let i = 0; i < presetGroups.length; i++) {
            const rows = presetGroups[i].rows.filter(row => presetMatches(row));
            if (rows.length > 0)
                result.push({ "key": presetGroups[i].key, "title": presetGroups[i].title,
                              "rows": rows });
        }
        return result;
    }
    // Clones carry no persona tokens: they follow the search only.
    readonly property var shownClones: cloneRows.filter(row => searchMatches(row))
    readonly property int shownCount: {
        let count = shownClones.length;
        for (let i = 0; i < shownGroups.length; i++)
            count += shownGroups[i].rows.length;
        return count;
    }

    readonly property string defaultVoiceId: hasController ? controller.defaultVoice : ""
    readonly property string defaultVoiceName: {
        const rows = allPresetRows.concat(cloneRows);
        for (let i = 0; i < rows.length; i++)
            if (rows[i].id === defaultVoiceId)
                return rows[i].name;
        return defaultVoiceId;
    }

    // ── helpers ───────────────────────────────────────────────────────────
    function isClonedGroup(group) {
        return group.id === "cloned" || group.label === "Đã sao chép";
    }

    // Lower-case, diacritics stripped ("Ngọc" and "ngoc" both match).
    function normalize(text) {
        return String(text || "").toLocaleLowerCase().normalize("NFD")
            .replace(/[̀-ͯ]/g, "").replace(/đ/g, "d");
    }

    function regionLabel(token) {
        if (token === "Bắc")
            return qsTr("Miền Bắc");
        if (token === "Trung")
            return qsTr("Miền Trung");
        if (token === "Nam")
            return qsTr("Miền Nam");
        return token || "";
    }

    function genderLabel(token) {
        if (token === "Nam")
            return qsTr("Nam", "voice gender: male");
        if (token === "Nữ")
            return qsTr("Nữ", "voice gender: female");
        return token || "";
    }

    function styleLabel(token) {
        switch (token) {
        case "kể chuyện":
            return qsTr("Kể chuyện", "voice style: storytelling");
        case "tin tức":
            return qsTr("Tin tức", "voice style: news");
        case "tự nhiên":
            return qsTr("Tự nhiên", "voice style: natural");
        case "đọc truyện":
            return qsTr("Đọc truyện", "voice style: reading");
        }
        const text = String(token || "");
        return text !== "" ? text.charAt(0).toLocaleUpperCase() + text.substring(1) : "";
    }

    function groupTitle(key, label) {
        const region = regionLabel(key);
        return region !== key ? region : String(label || key);
    }

    // Catalog row → {id, name, gender, region, style, persona, cloned, search}.
    // Capability rows carry persona fields explicitly (a pinned speaker's
    // native language rides in `region`); VieNeu labels are parsed like
    // VoicePicker.parseVoiceInfo ("Name — gender · region · style").
    function rowInfo(voice, cloned) {
        const label = String(voice.label || voice.id || "");
        let name = label;
        let gender = voice.gender !== undefined ? String(voice.gender) : "";
        let region = voice.region !== undefined ? String(voice.region) : "";
        let style = voice.style !== undefined ? String(voice.style) : "";
        if (label.indexOf(" — ") >= 0) {
            const parts = label.split(" — ");
            name = parts[0].trim();
            if (gender === "" && region === "" && style === "") {
                const tokens = parts.slice(1).join(" — ").split(" · ").map(s => s.trim());
                gender = tokens.length >= 1 ? tokens[0] : "";
                region = tokens.length >= 2 ? tokens[1] : "";
                style = tokens.length >= 3 ? tokens.slice(2).join(" · ")
                    .replace(/^(Phong cách|Giọng đọc)\s*/i, "").trim() : "";
            }
        }
        style = style.toLocaleLowerCase();
        const persona = [genderLabel(gender), regionLabel(region), styleLabel(style)]
            .filter(part => String(part || "").trim() !== "").join(" · ");
        return {
            "id": String(voice.id),
            "name": name,
            "gender": gender,
            "region": region,
            "style": style,
            "persona": persona,
            "cloned": cloned,
            "search": normalize(name + " " + voice.id + " " + persona)
        };
    }

    function searchMatches(row) {
        return searchKey === "" || row.search.indexOf(searchKey) >= 0;
    }

    function presetMatches(row) {
        if (genderFilter !== "all" && row.gender !== genderFilter)
            return false;
        if (styleFilter !== "" && row.style !== styleFilter)
            return false;
        return searchMatches(row);
    }

    function clearFilters() {
        genderFilter = "all";
        styleFilter = "";
        searchField.text = "";
    }

    function initials(name) {
        const clean = String(name || "").trim();
        if (clean === "")
            return "";
        const words = clean.split(/\s+/);
        if (words.length >= 2)
            return (words[0].charAt(0) + words[words.length - 1].charAt(0)).toLocaleUpperCase();
        return clean.substring(0, 2).toLocaleUpperCase();
    }

    function isAuditioning(id) {
        return hasController && id !== "" && controller.auditionVoiceId === id
            && controller.auditionState !== "idle";
    }

    function openClone() {
        if (hasBridge)
            bridge.setVoicesView("clone");
    }

    // ── one voice row (presets and clones alike) ──────────────────────────
    component VoiceRow: Rectangle {
        id: voiceRow

        required property var modelData
        required property int index
        readonly property string voiceId: modelData.id
        readonly property string gender: modelData.gender
        readonly property string styleKey: modelData.style
        readonly property bool isDefault: EngineState.defaultVoiceApplies
            && root.defaultVoiceId === voiceId

        objectName: "voicesRow"
        Layout.fillWidth: true
        implicitHeight: Math.max(56, rowLayout.implicitHeight + Theme.spacingSm * 2)
        radius: Theme.radiusMd
        color: rowHover.hovered ? Theme.surfaceHover : "transparent"
        border.width: isDefault ? 1 : 0
        border.color: Theme.accent

        HoverHandler {
            id: rowHover
        }

        RowLayout {
            id: rowLayout

            anchors.fill: parent
            anchors.leftMargin: Theme.spacingSm
            anchors.rightMargin: Theme.spacingXs
            spacing: Theme.spacingSm

            Rectangle {
                Layout.preferredWidth: 36
                Layout.preferredHeight: 36
                radius: 18
                color: voiceRow.isDefault ? Theme.accentSubtle : Theme.surfaceAlt

                Label {
                    anchors.centerIn: parent
                    text: root.initials(voiceRow.modelData.name)
                    color: voiceRow.isDefault ? Theme.accent : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    font.weight: Theme.fontWeightHeading
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Label {
                    objectName: "voicesRowName"
                    Layout.fillWidth: true
                    text: voiceRow.modelData.name
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    font.weight: Theme.fontWeightHeading
                    elide: Text.ElideRight
                }

                Label {
                    objectName: "voicesRowPersona"
                    Layout.fillWidth: true
                    text: voiceRow.modelData.persona
                    visible: text !== ""
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    elide: Text.ElideRight
                }
            }

            // Pick mode: the one row action is choosing the voice for the
            // Create run. Selected look: chip + checked (never primary).
            AppButton {
                objectName: "voicesRowUse"
                visible: root.pickForCreate
                variant: "chip"
                size: "sm"
                readonly property bool inUse: root.createVoiceId === voiceRow.voiceId
                checked: inUse
                iconKind: inUse ? "check" : ""
                text: inUse ? qsTr("Đang dùng") : qsTr("Dùng giọng này")
                accessibleLabel: qsTr("Dùng %1 cho Tạo giọng đọc").arg(voiceRow.modelData.name)
                onClicked: root.useVoiceRequested(voiceRow.voiceId)
            }

            AppButton {
                objectName: "voicesRowSetDefault"
                visible: EngineState.defaultVoiceApplies && !root.pickForCreate
                variant: "chip"
                size: "sm"
                checked: voiceRow.isDefault
                iconKind: voiceRow.isDefault ? "check" : ""
                text: voiceRow.isDefault ? qsTr("Mặc định") : qsTr("Đặt làm mặc định")
                accessibleLabel: voiceRow.isDefault
                    ? qsTr("%1 là giọng mặc định").arg(voiceRow.modelData.name)
                    : qsTr("Đặt %1 làm giọng mặc định").arg(voiceRow.modelData.name)
                enabled: root.hasController
                onClicked: {
                    if (!voiceRow.isDefault && EngineState.defaultVoiceApplies)
                        controller.defaultVoice = voiceRow.voiceId;
                }
            }

            AppIconButton {
                objectName: "voicesRowAudition"
                iconKind: root.isAuditioning(voiceRow.voiceId) ? "stop" : "play"
                accessibleLabel: root.isAuditioning(voiceRow.voiceId)
                    ? qsTr("Dừng nghe thử")
                    : qsTr("Nghe thử %1").arg(voiceRow.modelData.name)
                tooltipText: accessibleLabel
                busy: root.hasController && controller.auditionVoiceId === voiceRow.voiceId
                    && controller.auditionState === "loading"
                enabled: root.hasController && !controller.busy
                disabledReason: qsTr("Không thể nghe thử khi đang tạo âm thanh.")
                onClicked: controller.auditionVoice(voiceRow.voiceId)
            }
        }
    }

    StackLayout {
        anchors.fill: parent
        currentIndex: root.view === "clone" ? 1 : 0

        // ── library view ──────────────────────────────────────────────────
        Pane {
            id: libraryPage

            objectName: "voicesLibrary"
            padding: Theme.spacingLg

            background: Rectangle {
                color: Theme.bg
            }

            PageShell {
                anchors.fill: parent
                maxWidth: 960

                PageHeader {
                    Layout.fillWidth: true
                    iconKind: "voices"
                    title: qsTr("Giọng đọc")
                    trailing: AppButton {
                        objectName: "voicesCreateButton"
                        variant: "primary"
                        iconKind: "cloning"
                        text: qsTr("Tạo giọng mới")
                        enabled: EngineState.supportsCloning
                        disabledReason: EngineState.cloningBlockedReason
                        onClicked: root.openClone()
                    }
                }

                // Pick mode banner: what this visit is for, and the way out.
                Rectangle {
                    objectName: "voicesPickBanner"
                    Layout.fillWidth: true
                    visible: root.pickForCreate
                    implicitHeight: pickRow.implicitHeight + Theme.spacingSm * 2
                    radius: Theme.radiusMd
                    color: Theme.accentSubtle
                    border.width: 1
                    border.color: Theme.accent

                    RowLayout {
                        id: pickRow

                        anchors.fill: parent
                        anchors.leftMargin: Theme.spacingMd
                        anchors.rightMargin: Theme.spacingXs
                        spacing: Theme.spacingSm

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("Chọn giọng cho Tạo giọng đọc — bấm \"Dùng giọng này\" trên một giọng.")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            wrapMode: Text.Wrap
                        }

                        AppButton {
                            objectName: "voicesPickCancel"
                            variant: "quiet"
                            text: qsTr("Hủy")
                            accessibleLabel: qsTr("Hủy chọn giọng, quay lại Tạo giọng đọc")
                            onClicked: root.pickCancelled()
                        }
                    }
                }

                // Default voice: what new work starts with (VieNeu's setting).
                // Short windows (≈640×420) drop this line for the list — the
                // default row's checked "Mặc định" chip still says it.
                Label {
                    objectName: "voicesDefaultSummary"
                    Layout.fillWidth: true
                    visible: EngineState.defaultVoiceApplies && root.height >= 480
                    text: root.defaultVoiceName !== ""
                        ? qsTr("Giọng mặc định: %1").arg(root.defaultVoiceName)
                        : qsTr("Chưa đặt giọng mặc định.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    wrapMode: Text.Wrap
                }

                Label {
                    objectName: "voicesDefaultNote"
                    Layout.fillWidth: true
                    visible: !EngineState.defaultVoiceApplies
                    text: EngineState.defaultVoiceNote
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                // ── filters ───────────────────────────────────────────────
                ColumnLayout {
                    Layout.fillWidth: true
                    visible: !EngineState.hasNoVoices
                    spacing: Theme.spacingSm

                    // Search + gender share one row; the style chips get a
                    // row of their own, which a 640 px window still fits on
                    // one line (one Flow of everything wrapped to two rows
                    // and left less than one voice row on a 640×420 window).
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: Theme.controlHitTarget
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.width: searchField.activeFocus ? Theme.focusRingWidth : 1
                            border.color: searchField.activeFocus ? Theme.accent : Theme.borderSubtle

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: Theme.spacingMd
                                anchors.rightMargin: Theme.spacingXs
                                spacing: Theme.spacingSm

                                AppIcon {
                                    width: 16
                                    height: 16
                                    kind: "search"
                                    iconColor: searchField.activeFocus ? Theme.accent : Theme.textSubtle
                                }

                                TextField {
                                    id: searchField

                                    objectName: "voicesSearchField"
                                    Layout.fillWidth: true
                                    placeholderText: qsTr("Tìm theo tên, giới tính, vùng miền…")
                                    placeholderTextColor: Theme.textSubtle
                                    color: Theme.text
                                    selectedTextColor: Theme.accentText
                                    selectionColor: Theme.accent
                                    selectByMouse: true
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    padding: 0
                                    background: null
                                    Accessible.name: qsTr("Tìm giọng đọc")
                                    onTextChanged: root.searchText = text
                                }

                                AppIconButton {
                                    visible: searchField.text !== ""
                                    size: "sm"
                                    iconKind: "close"
                                    tooltipText: qsTr("Xóa tìm kiếm")
                                    accessibleLabel: tooltipText
                                    onClicked: searchField.text = ""
                                }
                            }
                        }

                        AppSegmented {
                            objectName: "voicesGenderFilter"
                            visible: root.hasGenders
                            accessibleLabel: qsTr("Giới tính")
                            model: [
                                { "value": "all", "label": qsTr("Tất cả") },
                                { "value": "Nam", "label": qsTr("Nam", "voice gender: male") },
                                { "value": "Nữ", "label": qsTr("Nữ", "voice gender: female") }
                            ]
                            currentValue: root.genderFilter
                            onActivated: function (value) {
                                root.genderFilter = value;
                            }
                        }
                    }

                    Flow {
                        Layout.fillWidth: true
                        visible: root.styleKeys.length > 1
                        spacing: Theme.spacingSm

                        Repeater {
                            model: root.styleKeys.length > 1 ? root.styleKeys : []

                            delegate: AppButton {
                                required property var modelData
                                required property int index
                                readonly property string styleKey: modelData

                                objectName: "voicesStyleChip"
                                variant: "chip"
                                checked: root.styleFilter === styleKey
                                text: styleKey === "" ? qsTr("Mọi phong cách")
                                    : root.styleLabel(styleKey)
                                onClicked: root.styleFilter = styleKey
                            }
                        }
                    }
                }

                // A profile with nothing to offer yet (Qwen Base before its
                // first enrollment): the reason, and the way out.
                AppNotice {
                    objectName: "voicesEmptyNotice"
                    Layout.fillWidth: true
                    visible: EngineState.hasNoVoices
                    message: EngineState.noVoicesReason
                    actionText: EngineState.supportsCloning ? qsTr("Tạo giọng mới") : ""
                    onActionTriggered: root.openClone()
                }

                // Filters matched nothing.
                RowLayout {
                    objectName: "voicesNoResults"
                    Layout.fillWidth: true
                    visible: !EngineState.hasNoVoices && root.filtering && root.shownCount === 0
                    spacing: Theme.spacingMd

                    Label {
                        Layout.fillWidth: true
                        text: qsTr("Không có giọng nào khớp bộ lọc.")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        wrapMode: Text.Wrap
                    }

                    AppButton {
                        objectName: "voicesClearFilters"
                        variant: "quiet"
                        text: qsTr("Xóa bộ lọc")
                        onClicked: root.clearFilters()
                    }
                }

                // ── preset groups (by region) ─────────────────────────────
                Repeater {
                    model: root.shownGroups

                    delegate: AppCard {
                        id: groupCard

                        required property var modelData
                        required property int index
                        readonly property string groupKey: modelData.key

                        objectName: "voicesGroup"
                        Layout.fillWidth: true
                        title: modelData.title
                        badgeText: String(modelData.rows.length)
                        cardPadding: Theme.spacingMd

                        GridLayout {
                            Layout.fillWidth: true
                            columns: Math.max(1, Math.floor((width + columnSpacing) / 420))
                            columnSpacing: Theme.spacingSm
                            rowSpacing: 2

                            Repeater {
                                model: groupCard.modelData.rows
                                delegate: VoiceRow {}
                            }
                        }
                    }
                }

                // ── cloned voices ─────────────────────────────────────────
                AppCard {
                    objectName: "voicesClonedSection"
                    Layout.fillWidth: true
                    visible: root.shownClones.length > 0
                        || (root.cloneRows.length === 0 && !root.filtering
                            && !EngineState.hasNoVoices && EngineState.supportsCloning)
                    title: qsTr("Giọng đã sao chép")
                    badgeText: root.cloneRows.length > 0 ? String(root.cloneRows.length) : ""
                    cardPadding: Theme.spacingMd

                    Label {
                        objectName: "voicesClonedEmpty"
                        Layout.fillWidth: true
                        visible: root.cloneRows.length === 0
                        text: qsTr("Chưa có giọng sao chép nào — chọn \"Tạo giọng mới\" để tạo từ một đoạn âm thanh mẫu 3–8 giây.")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        wrapMode: Text.Wrap
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        visible: root.shownClones.length > 0
                        columns: Math.max(1, Math.floor((width + columnSpacing) / 420))
                        columnSpacing: Theme.spacingSm
                        rowSpacing: 2

                        Repeater {
                            model: root.shownClones
                            delegate: VoiceRow {}
                        }
                    }
                }
            }
        }

        // ── clone view: the cloning flow, breadcrumbed under Giọng đọc ────
        CloningTab {
            showBack: true
            onBackRequested: if (root.hasBridge) bridge.setVoicesView("library")
        }
    }
}
