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
        # Phase 5 Task 5.4: a re-synthesis refused because the clip's audio came
        # from another engine names the profile to switch to.
        (
            "Đoạn này được tạo bằng {profile}. Hãy chuyển sang hồ sơ đó để tạo lại."
        ): "This clip was produced with {profile}. Switch to that profile to re-synthesize it.",
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


def test_translator_for_en_has_engine_profile_copy() -> None:
    # Engine profile switching + the profile-scoped synthesis language
    # (Phase 5): an English UI must not fall back to Vietnamese for the
    # refusals a user hits while choosing a profile/language.
    translator = translator_for("en")
    assert translator is not None
    expected_controller = {
        "Hồ sơ engine không hợp lệ: {}": "Invalid engine profile: {}",
        "Không thể đổi engine khi đang xử lý: {}": "Cannot switch engines while busy: {}",
        "{} không hỗ trợ ngôn ngữ {} — chọn một trong: {}": (
            "{} does not support language {} — choose one of: {}"
        ),
        "Không thể lưu cài đặt: {}": "Could not save settings: {}",
    }
    for source, translation in expected_controller.items():
        assert translator.translate("AppController", source) == translation
    assert (
        translator.translate(
            "BatchFileController", "Không thể tạo tác vụ tổng hợp cho cấu hình engine hiện tại."
        )
        == "Could not create a synthesis job for the current engine configuration."
    )


def test_translator_for_en_has_qwen_engine_install_copy() -> None:
    # Phase 6 Task 6.1: the model-family/language pickers and the Qwen
    # runtime + model install surfaces. Every action a user takes here
    # (install, cancel, repair, remove, import an offline bundle) and every
    # way it can refuse (unsupported hardware, platform without a runtime,
    # CPU-only guidance, corruption) must read in English.
    translator = translator_for("en")
    assert translator is not None
    expected_settings = {
        "Họ mô hình (engine)": "Model family (engine)",
        "Chọn engine tổng hợp và ngôn ngữ mà engine đó nhận.": (
            "Choose the synthesis engine and the language it accepts."
        ),
        "Ngôn ngữ tổng hợp": "Synthesis language",
        "Thiết bị tính toán cho Qwen": "Compute device for Qwen",
        "Máy này không có runtime Qwen cho thiết bị nào.": (
            "This machine has no Qwen runtime for any device."
        ),
        "Sẽ chạy trên: %1": "Will run on: %1",
        "%1 không khả dụng: %2": "%1 is unavailable: %2",
        "Runtime Qwen được quản lý": "Managed Qwen runtime",
        "Runtime Qwen được quản lý chỉ hỗ trợ Windows/Linux x64 và Apple Silicon.": (
            "The managed Qwen runtime supports only Windows/Linux x64 and Apple Silicon."
        ),
        "Không hỗ trợ runtime Qwen": "Qwen runtime unsupported",
        "Cài đặt runtime Qwen thất bại": "Qwen runtime installation failed",
        "Đang xác thực các tệp runtime Qwen…": "Verifying the Qwen runtime files…",
        "Cài đặt runtime Qwen": "Install Qwen runtime",
        "Hủy tải runtime Qwen": "Cancel the Qwen runtime download",
        "Sửa chữa runtime Qwen": "Repair Qwen runtime",
        "Gỡ runtime Qwen": "Remove Qwen runtime",
        "Nhập gói runtime Qwen ngoại tuyến": "Import an offline Qwen runtime bundle",
        "Chọn thư mục gói runtime Qwen": "Choose the Qwen runtime bundle folder",
        "Mô hình Qwen": "Qwen model",
        "%1/%2 đã cài": "%1/%2 installed",
        "Đang dùng": "In use",
        "Cần tải %1": "%1 to download",
        "Cài đặt %1": "Install %1",
        "Sửa chữa %1": "Repair %1",
        "Gỡ %1": "Remove %1",
        "Nhập gói ngoại tuyến cho %1": "Import an offline bundle for %1",
        "Chạy Qwen trên CPU rất chậm": "Running Qwen on CPU is very slow",
        "Chọn thư mục gói mô hình Qwen": "Choose the Qwen model bundle folder",
    }
    for source, translation in expected_settings.items():
        assert translator.translate("SettingsTab", source) == translation
    # Extracted pickers own their strings in their own lupdate contexts.
    expected_pickers = {
        ("EngineProfilePicker", "Chưa sẵn sàng"): "Not ready",
        ("LanguagePicker", "Engine này không nhận tham số ngôn ngữ."): (
            "This engine takes no language parameter."
        ),
        ("LanguagePicker", "%1 sẽ tự nhận diện ngôn ngữ của văn bản."): (
            "%1 detects the text language on its own."
        ),
    }
    for (context, source), translation in expected_pickers.items():
        assert translator.translate(context, source) == translation
    # Phase 6 Task 6.2: the capability state every synthesis surface shares —
    # the picker's catalog group names, the reason a profile has nothing to
    # offer, the readiness sentences (which moved here from the profile
    # picker, so a control and the picker can never disagree) and the device
    # readout.
    expected_engine_state = {
        "Người nói cố định": "Fixed speakers",
        "Giọng đã sao chép": "Cloned voices",
        ("Hồ sơ này chỉ tổng hợp bằng giọng đã sao chép — hãy tạo một giọng trong tab Sao chép."): (
            "This profile synthesizes only with cloned voices"
            " — create one in the Voice Cloning tab."
        ),
        "Mô hình và runtime đã sẵn sàng cho engine này.": (
            "Model and runtime are ready for this engine."
        ),
        "Đang chuẩn bị mô hình/runtime cho engine này…": (
            "Preparing the model and runtime for this engine…"
        ),
        "Không thể chuẩn bị engine này. Mở Cài đặt để sửa hoặc cài lại.": (
            "This engine could not be prepared. Open Settings to repair or reinstall it."
        ),
        "Máy này không có runtime cho engine đã chọn.": (
            "This machine has no runtime for the selected engine."
        ),
        "Cần cài mô hình và runtime trong Cài đặt trước khi dùng engine này.": (
            "Install the model and runtime in Settings before using this engine."
        ),
        "Mô hình đã sẵn sàng — hãy cài runtime Qwen trong Cài đặt.": (
            "The model is ready — install the Qwen runtime in Settings."
        ),
        "Cần cài mô hình trong Cài đặt trước khi dùng engine này.": (
            "Install the model in Settings before using this engine."
        ),
        "đang kiểm tra…": "checking…",
        "Thiết bị: %1": "Device: %1",
    }
    for source, translation in expected_engine_state.items():
        assert translator.translate("EngineState", source) == translation
    # Phase 6 Task 6.3: the Cloning tab's capability gate (a fixed-speaker
    # profile offers no enrollment), the reference transcript Base needs, and
    # the Studio's provenance / engine-switch copy.
    expected_cloning = {
        "Hồ sơ này không hỗ trợ sao chép giọng": "This profile cannot clone voices",
        (
            "Mở Cài đặt → Họ mô hình (engine) để chọn engine có thể sao chép giọng."
        ): "Open Settings → Model family (engine) to pick an engine that can clone voices.",
        "Văn bản trong đoạn tham chiếu": "Text in the reference clip",
        "Nhập văn bản của đoạn tham chiếu trước khi tạo giọng.": (
            "Enter the reference transcript before creating the voice."
        ),
        "Hồ sơ: %1": "Profile: %1",
    }
    for source, translation in expected_cloning.items():
        assert translator.translate("CloningTab", source) == translation
    assert (
        translator.translate(
            "EngineState",
            "%1 dùng giọng cố định nên không thể sao chép giọng"
            " — hãy chuyển sang hồ sơ hỗ trợ sao chép.",
        )
        == "%1 uses fixed speakers and cannot clone voices"
        " — switch to a profile that supports cloning."
    )
    expected_studio = {
        "Chuyển sang %1": "Switch to %1",
        "Đoạn này được tạo bằng %1. Chuyển sang hồ sơ đó để tạo lại.": (
            "This clip was produced with %1. Switch to that profile to re-synthesize it."
        ),
    }
    for source, translation in expected_studio.items():
        assert translator.translate("StudioTab", source) == translation
    # The clip row owns its own provenance line (its own lupdate context).
    expected_clip_row = {
        "Hồ sơ: %1": "Profile: %1",
        "Ngôn ngữ: %1": "Language: %1",
        "VieNeu-TTS (bản cũ)": "VieNeu-TTS (legacy)",
    }
    for source, translation in expected_clip_row.items():
        assert translator.translate("StudioClipRow", source) == translation
    # The device/runtime/model refusals are raised in AppController.
    expected_controller = {
        "Thiết bị Qwen không hợp lệ: {}": "Invalid Qwen device: {}",
        "Nền tảng này không có runtime Qwen được hỗ trợ.": (
            "This platform has no supported Qwen runtime."
        ),
        "Không phát hiện GPU NVIDIA trên máy này.": ("No NVIDIA GPU was detected on this machine."),
        "Hồ sơ Qwen không hợp lệ: {}": "Invalid Qwen profile: {}",
        "Chọn thư mục chứa các tệp wheel của runtime Qwen.": (
            "Choose the folder holding the Qwen runtime wheel files."
        ),
    }
    for source, translation in expected_controller.items():
        assert translator.translate("AppController", source) == translation


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


def test_english_ts_has_no_empty_translations() -> None:
    # A translation can be missing without being marked "unfinished" (that is
    # how lupdate's same-text heuristic leaves a fresh entry). An empty entry
    # would silently fall back to Vietnamese at runtime.
    empty = [
        f"{context}: {source!r}"
        for context, source, _comment, forms in _english_ts_messages()
        if not all(form.strip() for form in forms)
    ]
    assert not empty, f"empty translations: {empty[:5]}"


def test_english_ts_has_no_obsolete_entries() -> None:
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


def test_english_catalog_compiles_every_entry() -> None:
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


def test_identical_sources_share_one_translation() -> None:
    # The same Vietnamese sentence must read the same in English wherever it
    # means the same thing — a shared sentence translated twice drifts (the
    # playback-unavailable notice and the invalid-audio refusal were each
    # written two ways before Phase 6 Task 6.4). Only these two sources are
    # genuinely context-dependent: "Xóa" is Delete (a cloned voice) vs Clear
    # (an editor), "Văn bản" is Transcript (the audiobook's reference text)
    # vs Text (the tab's own name).
    ambiguous = {"Xóa", "Văn bản"}
    by_source: dict[str, set[str]] = {}
    for _context, source, _comment, forms in _english_ts_messages():
        by_source.setdefault(source, set()).add(forms[0])
    divergent = {
        source: sorted(translations)
        for source, translations in by_source.items()
        if len(translations) > 1 and source not in ambiguous
    }
    assert not divergent, f"identical sources translated differently: {divergent}"


def test_english_catalog_files_exist() -> None:
    assert TS_PATH.is_file()
    assert (TS_PATH.with_suffix(".qm")).is_file()
