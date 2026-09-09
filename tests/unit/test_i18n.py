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


def test_resolve_language() -> None:
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


def test_resolve_language_unknown_preference_behaves_like_system() -> None:
    # Settings validation keeps preferences in SUPPORTED_LANGUAGES; resolve
    # stays total by treating anything else as "system".
    assert resolve_language("fr", "en_US") == "en"
    assert resolve_language("fr", "vi_VN") == "vi"


def test_translator_for_vi_is_none() -> None:
    # Vietnamese is the qsTr source language — no catalog, no translator.
    assert translator_for("vi") is None


def test_translator_for_en_loads_and_translates() -> None:
    translator = translator_for("en")
    assert translator is not None
    # Context + source must mirror a real entry in vienetts_en.ts (lupdate
    # names QML contexts after the file, minus the .qml suffix). A drift
    # here means the catalog no longer matches the sources.
    translated = translator.translate("SettingsTab", "Chế độ màu sắc")
    assert translated, "catalog entry missing for SettingsTab color-mode label"
    assert translated == "Color mode"


def test_translator_for_en_has_cuda_runtime_settings_copy() -> None:
    translator = translator_for("en")
    assert translator is not None
    expected = {
        "Runtime CUDA được quản lý": "Managed CUDA runtime",
        "Cài đặt runtime CUDA": "Install CUDA runtime",
        "Hủy tải runtime CUDA": "Cancel CUDA runtime download",
        "Thử lại cài đặt runtime CUDA": "Retry CUDA runtime installation",
        "Gỡ runtime CUDA": "Remove CUDA runtime",
        "Kiểm tra runtime CUDA cục bộ": "Check local CUDA runtimes",
        "Đã phát hiện %1 runtime CUDA cục bộ.": "Detected %1 local CUDA runtimes.",
        "Mở trang tải driver NVIDIA": "Open the NVIDIA driver download page",
        ("Máy không có GPU NVIDIA thì không dùng được runtime CUDA — dùng backend ONNX (CPU)."): (
            "Machines without an NVIDIA GPU cannot use the CUDA runtime"
            " — use the ONNX (CPU) backend."
        ),
    }
    for source, translation in expected.items():
        assert translator.translate("SettingsTab", source) == translation


def test_translator_for_en_has_studio_copy() -> None:
    translator = translator_for("en")
    assert translator is not None
    expected = {
        "Studio Âm thanh": "Audio Studio",
        "Dự án Studio": "Studio Project",
        "Tinh chỉnh âm thanh": "Audio Refinements",
        "Đoạn âm thanh": "Audio Clips",
        "Xuất âm thanh": "Export audio",
        "Nghe thử": "Preview",
    }
    for source, translation in expected.items():
        assert translator.translate("StudioTab", source) == translation


def test_english_ts_has_no_unfinished_translations() -> None:
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


def test_english_catalog_files_exist() -> None:
    assert TS_PATH.is_file()
    assert (TS_PATH.with_suffix(".qm")).is_file()
