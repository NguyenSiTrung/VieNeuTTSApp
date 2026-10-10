"""UI language resolution + English catalog contracts.

``resolve_language`` is the pure preference→concrete-language mapping the
bootstrap and the controller share; the catalog tests are the quality gate
that keeps ``vienetts_en.ts``/``.qm`` from drifting out of sync with the
``qsTr``/``tr`` sources (unfinished entries or a stale/missing ``.qm`` fail).
"""

import pytest

pytest.importorskip("PySide6")

from vienetts_app.ui.i18n import (  # noqa: E402
    TS_PATH,
    resolve_language,
    translator_for,
)


def test_resolve_language_contract() -> None:
    cases = [
        # "system" → English only for en_* locales; everything else falls back to Vietnamese.
        ("system", "en_US", "en"),
        ("system", "en_GB", "en"),
        ("system", "vi_VN", "vi"),
        ("system", "C", "vi"),
        ("system", "", "vi"),
        ("system", "fr_FR", "vi"),
        # Explicit choices ignore the system locale.
        ("vi", "en_US", "vi"),
        ("en", "vi_VN", "en"),
    ]
    for preference, system_locale, expected in cases:
        assert resolve_language(preference, system_locale) == expected

    # Settings validation keeps preferences in SUPPORTED_LANGUAGES; resolve
    # stays total by treating anything else as "system".
    assert resolve_language("fr", "en_US") == "en"
    assert resolve_language("fr", "vi_VN") == "vi"


def test_translator_loading() -> None:
    # Vietnamese is the qsTr source language — no catalog, no translator.
    assert translator_for("vi") is None

    translator = translator_for("en")
    assert translator is not None
    # Context + source must mirror a real entry in vienetts_en.ts (lupdate
    # names QML contexts after the file, minus the .qml suffix). A drift
    # here means the catalog no longer matches the sources.
    translated = translator.translate("SettingsTab", "Chế độ màu sắc")
    assert translated, "catalog entry missing for SettingsTab color-mode label"
    assert translated == "Color mode"
    # The five nav destinations (ShellBridge.TABS, FR-3.1).
    for source, english in (
        ("Tạo giọng đọc", "Create voice"),
        ("Sách nói", "Audiobooks"),
        ("Giọng đọc", "Voices"),
        ("Studio", "Studio"),
        ("Cài đặt", "Settings"),
    ):
        assert translator.translate("ShellBridge", source) == english
    # The create page's header and its mode switch (CreateTab, FR-3.2).
    for source, english in (
        ("Tạo giọng đọc", "Create voice"),
        ("Soạn thảo", "Compose"),
        ("Tài liệu", "Document"),
        ("Nhiều tệp", "Many files"),
        ("Phụ đề", "Subtitles"),
        ("Nhập tệp…", "Import file…"),
    ):
        assert translator.translate("CreateTab", source) == english
    # The compose toolbar's emotion chips (components/ComposeEditorCard.qml).
    assert translator.translate("ComposeEditorCard", "Biểu cảm") == "Emotions"
    # The window status bar is its own context (components/StatusBar.qml).
    assert translator.translate("StatusBar", "Kiểm tra lại") == "Check again"
    assert translator.translate("StatusBar", "Có bản cập nhật") == "Update available"
    # The transport dock is its own context (components/TransportDock.qml);
    # the compact voice chip's name stays in VoicePicker's.
    for source, english in (
        ("Tạo âm thanh (Ctrl+Enter)", "Generate audio (Ctrl+Enter)"),
        ("Dừng", "Stop"),
        ("Xuất WAV", "Export WAV"),
        ("Xuất MP3", "Export MP3"),
        ("Lưu nhanh", "Quick save"),
        ("Lưu thành…", "Save as…"),
        ("Mở trong Studio", "Open in Studio"),
        ("Tùy chọn khác", "More options"),
    ):
        assert translator.translate("TransportDock", source) == english
    assert translator.translate("VoicePicker", "Đổi giọng đọc: %1") == "Change voice: %1"
    # Sách nói's player (dock skin, FR-2.4): prev/next icon buttons' names.
    for source, english in (
        ("Chương trước", "Previous chapter"),
        ("Chương tiếp theo", "Next chapter"),
        ("Trình phát", "Player"),
    ):
        assert translator.translate("AudiobookTab", source) == english


def test_i18n_update_script_covers_all_controllers() -> None:
    # scripts/update_i18n.sh is the regeneration entry point: every UI
    # controller with tr() sources must be listed or lupdate silently drops
    # its strings (how the SRT studio shipped untranslated).
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "update_i18n.sh"
    text = script.read_text(encoding="utf-8")
    controllers = [
        p.name
        for p in (Path(__file__).resolve().parents[2] / "src" / "vienetts_app" / "ui").glob(
            "*_controller.py"
        )
    ]
    assert controllers, "no UI controllers found"
    missing = [name for name in controllers if name not in text]
    assert not missing, f"update_i18n.sh does not scan: {missing}"


def _english_ts_messages() -> list[tuple[str, str, str | None, list[str]]]:
    """(context, source, comment, translations) for every .ts message.

    ``translations`` holds one entry for a plain message, or one per numerus
    form for a plural source — the shape the compiled catalog serves through
    ``QTranslator.translate(context, source, comment, n)``.
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(TS_PATH)
    messages: list[tuple[str, str, str | None, list[str]]] = []
    for context in tree.findall(".//context"):
        name = context.findtext("name") or ""
        for message in context.findall("message"):
            translation = message.find("translation")
            if translation is None:
                continue
            forms = [form.text or "" for form in translation.findall("numerusform")]
            if not forms:
                forms = [translation.text or ""]
            messages.append((name, message.findtext("source") or "", message.get("comment"), forms))
    return messages


def test_english_catalog_integrity() -> None:
    import xml.etree.ElementTree as ET

    tree = ET.parse(TS_PATH)
    messages = tree.findall(".//message")
    assert messages, "English catalog is empty"
    unfinished = [
        m.find("source").text or ""
        for m in messages
        if (t := m.find("translation")) is not None and t.get("type") == "unfinished"
    ]
    assert not unfinished, f"unfinished translations: {unfinished[:5]}"

    # A translation can be missing without being marked "unfinished" (that is
    # how lupdate's same-text heuristic leaves a fresh entry). An empty entry
    # would silently fall back to Vietnamese at runtime.
    empty = [
        f"{context}: {source!r}"
        for context, source, _comment, forms in _english_ts_messages()
        if not all(form.strip() for form in forms)
    ]
    assert not empty, f"empty translations: {empty[:5]}"

    # scripts/update_i18n.sh runs lupdate with -noobsolete: a source string
    # that no longer exists in the UI must not linger as a stale entry, which
    # would silently revive (with an outdated translation) if the same text
    # came back later.
    import xml.etree.ElementTree as ET

    tree = ET.parse(TS_PATH)
    stale = []
    for context in tree.findall(".//context"):
        name = context.findtext("name") or ""
        for message in context.findall("message"):
            translation = message.find("translation")
            kind = translation.get("type") if translation is not None else None
            if kind in ("obsolete", "vanished") or message.get("type") == "vanished":
                stale.append(f"{name}: {message.findtext('source')!r} ({kind})")
    assert not stale, f"stale catalog entries: {stale[:5]}"

    # The .qm is committed and built by scripts/update_i18n.sh; a stale or
    # truncated one leaves an entry unserved (QTranslator returns "" for a
    # miss, the Vietnamese source otherwise). Round-trip every entry — plurals
    # through each numerus form — so the compiled catalog provably holds what
    # the .ts says.
    translator = translator_for("en")
    assert translator is not None
    misses = []
    for context, source, comment, forms in _english_ts_messages():
        for n, expected in enumerate(forms, start=1):
            if comment is None:
                actual = (
                    translator.translate(context, source)
                    if len(forms) == 1
                    else translator.translate(context, source, None, n)
                )
            else:
                actual = translator.translate(context, source, comment)
            if actual != expected:
                misses.append(f"{context}: {source!r} -> {actual!r} (want {expected!r})")
    assert not misses, f"catalog entries not compiled: {misses[:5]}"

    # The same Vietnamese sentence must read the same in English wherever it
    # means the same thing — a shared sentence translated twice drifts (the
    # playback-unavailable notice and the invalid-audio refusal were each
    # written two ways before Phase 6 Task 6.4). Only these two sources are
    # genuinely context-dependent: "Xóa" is Delete (a cloned voice) vs Clear
    # (an editor), "Giọng đọc" is Voice (the picker's current voice) vs Voices
    # (the voice-library destination in the nav).
    ambiguous = {"Xóa", "Giọng đọc"}
    by_source: dict[str, set[str]] = {}
    for _context, source, _comment, forms in _english_ts_messages():
        by_source.setdefault(source, set()).add(forms[0])
    divergent = {
        source: sorted(translations)
        for source, translations in by_source.items()
        if len(translations) > 1 and source not in ambiguous
    }
    assert not divergent, f"identical sources translated differently: {divergent}"

    assert TS_PATH.is_file()
    assert (TS_PATH.with_suffix(".qm")).is_file()
