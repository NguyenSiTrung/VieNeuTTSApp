#!/usr/bin/env bash
# Regenerate the English UI catalog (src/vienetts_app/ui/i18n/).
#
# Vietnamese is the qsTr source language and needs no catalog; English ships
# as vienetts_en.ts (source strings + translations, committed) compiled to
# vienetts_en.qm (committed too, so runtime needs no Qt tools).
#
# Workflow after adding/changing user-facing strings:
#   1. Run this script — lupdate merges new sources into the .ts (new
#      entries appear as unfinished).
#   2. Fill in English translations for the unfinished entries.
#   3. Run this script again — lrelease compiles the .qm.
#   4. The unit suite gates the result: tests/unit/test_i18n.py fails on any
#      unfinished entry or a missing .qm.
set -euo pipefail
cd "$(dirname "$0")/.."

LUPDATE=".venv/bin/pyside6-lupdate"
LRELEASE=".venv/bin/pyside6-lrelease"
command -v "$LUPDATE" >/dev/null 2>&1 || LUPDATE="pyside6-lupdate"
command -v "$LRELEASE" >/dev/null 2>&1 || LRELEASE="pyside6-lrelease"

TS="src/vienetts_app/ui/i18n/vienetts_en.ts"
QM="src/vienetts_app/ui/i18n/vienetts_en.qm"

"$LUPDATE" \
    src/vienetts_app/ui/qml \
    src/vienetts_app/ui/controller.py \
    src/vienetts_app/ui/audiobook_controller.py \
    src/vienetts_app/ui/bridge.py \
    -ts "$TS"

"$LRELEASE" "$TS" -qm "$QM"

if grep -q 'type="unfinished"' "$TS"; then
    echo "NOTE: $TS has unfinished entries — translate them, then re-run to recompile."
fi
