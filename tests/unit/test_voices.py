"""Generic voice descriptors and capability-aware catalogs (Phase 4 Task 1)."""

import pytest

from vienetts_app.core.backends import BackendCapabilityError
from vienetts_app.core.voices import (
    QWEN_SPEAKERS,
    VoiceDescriptor,
    descriptors_for,
    filter_by_language,
    qwen_descriptors,
    sort_native_first,
    vieneu_descriptors,
)


def catalog_entry(name: str, description: str, gender: str = "", style: str = "") -> dict:
    return {"name": name, "description": description, "gender": gender, "style": style}


class TestVieneuDescriptors:
    def test_region_grouping_preserved(self) -> None:
        entries = [
            catalog_entry("A", "Nam · Bắc · Tin tức", "male", "tin_tuc"),
            catalog_entry("B", "Nữ · Trung · Kể chuyện", "female", "ke_chuyen"),
            catalog_entry("C", "Nam · Nam · Hội thoại", "male", "hoi_thoai"),
            catalog_entry("D", "mystery voice"),
        ]
        descs = vieneu_descriptors(entries)
        by_id = {d.voice_id: d for d in descs}
        assert by_id["A"].region == "Bắc"
        assert by_id["B"].region == "Trung"
        assert by_id["C"].region == "Nam"
        assert by_id["D"].region is None
        assert all(d.engine == "vieneu" and d.kind == "preset" for d in descs)
        assert all(set(d.languages) == {"vi", "en"} for d in descs)

    def test_labels_and_metadata(self) -> None:
        (desc,) = vieneu_descriptors([catalog_entry("Adam", "Nam · Bắc · Tin tức")])
        assert desc.label == "Adam — Nam · Bắc · Tin tức"
        assert isinstance(desc, VoiceDescriptor)

    def test_descriptors_frozen(self) -> None:
        (desc,) = vieneu_descriptors([catalog_entry("A", "Nam · Bắc · X")])
        with pytest.raises(AttributeError):
            desc.region = "Nam"  # type: ignore[misc]


class TestQwenDescriptors:
    def test_nine_documented_speakers(self) -> None:
        assert len(QWEN_SPEAKERS) == 9
        names = [s.name for s in QWEN_SPEAKERS]
        for expected in (
            "Vivian",
            "Serena",
            "Uncle_Fu",
            "Dylan",
            "Eric",
            "Ryan",
            "Aiden",
            "Ono_Anna",
            "Sohee",
        ):
            assert expected in names

    def test_customvoice_covers_en_zh_ko(self) -> None:
        descs = qwen_descriptors("qwen_customvoice")
        assert len(descs) == 9
        assert all({"en", "zh", "ko"} <= set(d.languages) for d in descs)
        assert all(d.engine == "qwen_customvoice" and d.kind == "preset" for d in descs)

    def test_native_languages_recorded(self) -> None:
        by_id = {d.voice_id: d for d in qwen_descriptors("qwen_customvoice")}
        assert by_id["Vivian"].native_language == "Chinese"
        assert by_id["Ryan"].native_language == "English"
        assert by_id["Ono_Anna"].native_language == "Japanese"
        assert by_id["Sohee"].native_language == "Korean"

    def test_base_has_no_presets(self) -> None:
        assert qwen_descriptors("qwen_base") == []

    def test_unknown_engine_rejected(self) -> None:
        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            qwen_descriptors("bogus")  # type: ignore[arg-type]


class TestDispatchAndFilter:
    def test_dispatch_matches_engine(self) -> None:
        entries = [catalog_entry("A", "Nam · Bắc · X")]
        for desc in descriptors_for("vieneu", catalog=entries):
            assert desc.engine == "vieneu"
        for desc in descriptors_for("qwen_customvoice"):
            assert desc.engine == "qwen_customvoice"
        assert descriptors_for("qwen_base") == []

    def test_filter_by_language(self) -> None:
        entries = [catalog_entry("A", "Nam · Bắc · X")]
        combined = descriptors_for("vieneu", catalog=entries) + descriptors_for("qwen_customvoice")
        assert filter_by_language(combined, "") == combined
        assert filter_by_language(combined, "xx") == []
        en = filter_by_language(combined, "en")
        assert {d.voice_id for d in en} >= {"A", "Ryan"}
        vi = filter_by_language(combined, "vi")
        assert {d.voice_id for d in vi} == {"A"}

    def test_sort_native_first(self) -> None:
        descs = descriptors_for("qwen_customvoice")
        ordered = sort_native_first(descs, "zh")
        natives = [d.voice_id for d in ordered[:5]]
        assert set(natives) == {"Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric"}
        # Stable for the rest, and a no-op for empty/unknown languages.
        assert [d.voice_id for d in sort_native_first(descs, "")] == [d.voice_id for d in descs]
