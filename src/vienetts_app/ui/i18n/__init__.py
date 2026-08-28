"""UI language resolution + Qt translation catalogs (qsTr source: Vietnamese).

The QML sources wrap every user-facing string in ``qsTr```` with Vietnamese
text, so Vietnamese needs no catalog — it is the untranslated source. English
ships as ``vienetts_en.ts``/``.qm`` beside this module; both files are
committed so runtime never needs pyside6-lupdate/lrelease
(``scripts/update_i18n.sh`` regenerates them). The language applies at
startup (restart-to-apply; see SettingsTab's language banner).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTranslator

SUPPORTED_LANGUAGES = ("system", "vi", "en")

CATALOG_DIR = Path(__file__).parent
TS_PATH = CATALOG_DIR / "vienetts_en.ts"
QM_PATH = CATALOG_DIR / "vienetts_en.qm"


def resolve_language(preference: str, system_locale: str) -> str:
    """Map a language preference to a concrete UI language.

    ``"system"`` resolves to English for ``en_*`` locales and to Vietnamese
    (the source language) for everything else, including empty/``C`` locales;
    explicit ``"vi"``/``"en"`` pass through; unknown values behave like
    ``"system"`` (settings validation should keep them out regardless).
    """
    if preference == "en":
        return "en"
    if preference == "vi":
        return "vi"
    return "en" if system_locale.startswith("en") else "vi"


def translator_for(language: str) -> QTranslator | None:
    """Return the translator for ``language``, or None when none is needed.

    Only English has a catalog. A missing/unloadable ``.qm`` also returns
    None — the UI then shows the Vietnamese source instead of failing.
    """
    if language != "en":
        return None
    translator = QTranslator()
    if not translator.load(str(QM_PATH)):
        return None
    return translator
