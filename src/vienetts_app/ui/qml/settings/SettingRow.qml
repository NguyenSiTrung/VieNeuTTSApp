import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."
import "SettingsFilter.js" as Filter

// One settings row (FR-4.2): label + one-line description on the left, the
// control on the right; below the compact breakpoint the control stacks under
// the label at full width so no combo text is truncated.
//
// The Cài đặt filter narrows rows by label: `matches` is false unless every
// word of the page's filter text (bound in by the section as `filter`;
// diacritic- and case-insensitive, settings/SettingsFilter.js) occurs in the
// label or the keywords, and a non-matching row hides. Descriptions are not
// searched (their "mặc định …" asides matched everything). Hosts that need
// their own visibility condition set `shown`, never `visible`.
//
// API
//   label, description      row copy (description may be empty)
//   descriptionObjectName   tested name for the description Label
//   descriptionColor        e.g. Theme.warningText for a capability note
//   keywords                extra filter terms that are not on screen
//   filter, compact         bound in by the section
//   divider                 a hairline above the row (not on the first row)
//   default property        the control(s), laid out in a RowLayout
ColumnLayout {
    id: row

    property string label: ""
    property string description: ""
    property string descriptionObjectName: ""
    property color descriptionColor: Theme.textMuted
    property string keywords: ""
    property string filter: ""
    property bool compact: false
    property bool divider: false
    property bool shown: true
    readonly property bool matches: Filter.matches(row.filter, [row.label, row.keywords])

    default property alias control: controlSlot.data

    Layout.fillWidth: true
    spacing: Theme.spacingLg
    visible: row.shown && row.matches

    Rectangle {
        Layout.fillWidth: true
        // Hidden while filtering: the first surviving row must not open
        // with a rule.
        visible: row.divider && row.filter.trim() === ""
        implicitHeight: 1
        color: Theme.borderSubtle
        opacity: 0.7
    }

    GridLayout {
        Layout.fillWidth: true
        columns: row.compact ? 1 : 2
        columnSpacing: Theme.spacingLg
        rowSpacing: Theme.spacingSm

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: row.compact ? Qt.AlignLeft : Qt.AlignVCenter
            spacing: 3

            Label {
                Layout.fillWidth: true
                text: row.label
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
                wrapMode: Text.Wrap
            }

            Label {
                objectName: row.descriptionObjectName
                Layout.fillWidth: true
                visible: row.description !== ""
                text: row.description
                color: row.descriptionColor
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.Wrap
                lineHeight: 1.2
            }
        }

        RowLayout {
            id: controlSlot

            Layout.fillWidth: row.compact
            Layout.alignment: row.compact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
            spacing: Theme.spacingMd
        }
    }
}
