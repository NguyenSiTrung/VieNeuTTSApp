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
        # Redesign copy: the dock's audition target, the range toolbar and the
        # rack's pending-edit hint must not fall back to Vietnamese.
        "Toàn bộ dự án": "Whole project",
        "Đoạn #%1": "Clip #%1",
        "Giữ vùng chọn": "Keep selection",
        "Xoá vùng chọn": "Delete selection",
        "Xuất nhanh": "Quick export",
        # Visible transport stop/seek (were keyboard-only) and the reset
        # confirmation copy.
        "Dừng": "Stop",
        "Lùi 5 giây": "Back 5 seconds",
        "Tiến 5 giây": "Forward 5 seconds",
        "Đặt lại về bản gốc?": "Reset to original?",
        "Hủy": "Cancel",
    }
    for source, translation in expected.items():
        assert translator.translate("StudioTab", source) == translation
    # Extracted rack/clip components own their strings in their own lupdate
    # contexts — same sources, new context names.
    expected_components = {
        ("StudioParamRow", "Chưa áp dụng"): "Not applied",
        ("StudioParamRow", "Áp dụng"): "Apply",
        ("StudioClipRow", "Xoá đoạn %1"): "Delete clip %1",
        ("StudioClipRow", "Tạo lại…"): "Regenerate…",
    }
    for (context, source), translation in expected_components.items():
        assert translator.translate(context, source) == translation
    # The range-op labels are built in AppController (studioOps), so they live
    # in that context rather than in the QML catalog.
    expected_controller = {
        "Giữ đoạn": "Keep range",
        "Bỏ đoạn": "Remove range",
    }
    for source, translation in expected_controller.items():
        assert translator.translate("AppController", source) == translation


def test_translator_for_en_has_subtitle_copy() -> None:
    # Paragraph tab's SRT studio (SubtitleCard.qml + SubtitleController):
    # the whole mode rendered in Vietnamese under the English locale until
    # the catalog was regenerated for it — mode tab, card title and actions
    # must stay uniform with the translated "One document"/"Many files" modes.
    translator = translator_for("en")
    assert translator is not None
    expected_qml = {
        "Phụ đề (SRT)": "Subtitles (SRT)",
        "Nhập tệp .srt, chọn cách khớp thời gian, rồi tạo âm thanh và phụ đề đã căn chỉnh.": (
            "Import an .srt file, pick how the timing is matched,"
            " then render audio and realigned subtitles."
        ),
        "Chọn tệp phụ đề": "Choose a subtitle file",
        "Nhập .srt…": "Import .srt…",
        "Chưa có phụ đề nào. Nhập một tệp .srt để bắt đầu.": (
            "No subtitles yet. Import an .srt file to get started."
        ),
        "Lồng tiếng (theo SRT)": "Dub (follow SRT)",
        "Bản thoại tự nhiên": "Natural dialogue",
        "Tạo và phát": "Render & play",
        "Xuất WAV": "Export WAV",
        "Xuất SRT": "Export SRT",
        "Đã xuất: %1": "Exported: %1",
    }
    for source, translation in expected_qml.items():
        assert translator.translate("SubtitleCard", source) == translation
    assert translator.translate("ParagraphTab", "Phụ đề (SRT)") == "Subtitles (SRT)"
    expected_py = {
        "Tổng hợp thất bại.": "Synthesis failed.",
        "Không thể tạo tác vụ tổng hợp.": "Could not create a synthesis job.",
        "Không thể tạo tác vụ tổng hợp: {error}": "Could not create a synthesis job: {error}",
        "Chưa có tệp âm thanh để xuất. Hãy tạo trước.": (
            "Nothing to export yet — generate it first."
        ),
        "Không thể xuất tệp phụ đề: {error}": "Could not export the subtitle file: {error}",
    }
    for source, translation in expected_py.items():
        assert translator.translate("SubtitleController", source) == translation


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
