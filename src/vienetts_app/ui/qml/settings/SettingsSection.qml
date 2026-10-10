import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// One Cài đặt section (FR-4.2): a heading over the section's cards. The page
// shows the section the sub-navigation selected — or, while the filter field
// holds text, every section with at least one matching row (`hasMatch`,
// which each section derives from its own rows via anyMatch()).
//
// objectName is `settingsSection_<sectionId>` (tested contract).
ColumnLayout {
    id: section

    property var page: null
    property string sectionId: ""
    property string title: ""
    property bool hasMatch: true
    readonly property string filter: section.page ? section.page.filterText : ""
    readonly property bool compact: section.page ? section.page.isCompact : false

    default property alias content: body.data

    // Rows hidden by their host (wrong engine family, …) never count.
    function anyMatch(rows) {
        for (let i = 0; i < rows.length; i++)
            if (rows[i] && rows[i].shown && rows[i].matches)
                return true;
        return false;
    }

    objectName: "settingsSection_" + section.sectionId
    Layout.fillWidth: true
    spacing: Theme.spacingLg
    visible: section.page
        ? (section.page.filterActive ? section.hasMatch
                                     : section.page.currentSection === section.sectionId)
        : false

    // The side nav names the selected section; the stacked chip row (narrow
    // windows) already shows it checked right above, so the heading only
    // returns there while filtering (results span several sections).
    Label {
        objectName: "settingsSectionTitle"
        Layout.fillWidth: true
        visible: section.page ? (section.page.filterActive || !section.page.navStacked) : true
        text: section.title
        color: Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLg
        font.weight: Theme.fontWeightBold
        wrapMode: Text.Wrap
    }

    ColumnLayout {
        id: body

        Layout.fillWidth: true
        spacing: Theme.spacingLg
    }
}
