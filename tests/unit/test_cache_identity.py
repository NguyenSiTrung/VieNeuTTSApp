"""Cache identity: backend/profile/revision/language/instruction/voice (Phase 2 Task 4)."""

import numpy as np
import pytest

from vienetts_app.core.audiobook import AudiobookLibrary, chapter_render_identity
from vienetts_app.core.backends import default_model_tag
from vienetts_app.core.epub import EpubBook, EpubChapter
from vienetts_app.core.models import TTSRequest


def base_request(**kw) -> TTSRequest:
    return TTSRequest(text="hello", **kw)


class TestRequestModelTag:
    def test_default_tag_is_none(self) -> None:
        assert base_request().model_tag is None

    def test_tag_must_be_str_or_none(self) -> None:
        with pytest.raises(ValueError, match="model_tag"):
            base_request(model_tag=123)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="model_tag"):
            base_request(model_tag="  ")

    def test_identity_covers_model_tag(self) -> None:
        assert base_request().cache_identity() != base_request(model_tag="rev-a").cache_identity()
        assert (
            base_request(model_tag="rev-a").cache_identity()
            == base_request(model_tag="rev-a").cache_identity()
        )


class TestDefaultModelTag:
    def test_official_vieneu_tag_stable(self) -> None:
        tag = default_model_tag("vieneu")
        assert tag.startswith("vieneu-official:")
        assert default_model_tag("vieneu") == tag

    def test_custom_repo_tag_differs(self) -> None:
        assert default_model_tag("vieneu", model_repo="someone/custom") != default_model_tag(
            "vieneu"
        )
        assert "someone/custom" in default_model_tag("vieneu", model_repo="someone/custom")

    def test_qwen_unmanaged_tags_distinct_per_profile(self) -> None:
        custom = default_model_tag("qwen_customvoice")
        base = default_model_tag("qwen_base")
        assert custom != base
        assert custom != default_model_tag("vieneu")

    def test_unknown_engine_rejected(self) -> None:
        from vienetts_app.core.backends import BackendCapabilityError

        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            default_model_tag("bogus")  # type: ignore[arg-type]


def make_library(tmp_path) -> tuple[AudiobookLibrary, str]:
    library = AudiobookLibrary(tmp_path / "audiobooks")
    book = EpubBook(
        title="T",
        author="A",
        chapters=[EpubChapter(index=0, title="C1", text="Chapter one text.")],
        source_path="/b.epub",
        content_hash="1" * 64,
    )
    record = library.add_book(book)
    return library, record.id


def sample() -> np.ndarray:
    return np.zeros(4800, dtype=np.float32)


class TestChapterRenderIdentity:
    def test_builder_is_canonical(self) -> None:
        first = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        second = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        assert first == second
        assert set(first) == {
            "engine",
            "model_tag",
            "language",
            "voice",
            "voice_source",
            "instruction",
            "speed",
            "silence_p",
            "text_sha256",
        }

    def test_each_field_prevents_collision(self) -> None:
        base = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        variants = [
            chapter_render_identity(engine="qwen_customvoice", language="en", voice="Ryan"),
            chapter_render_identity(engine="vieneu", language="vi", voice="Adam", model_tag="x"),
            chapter_render_identity(engine="vieneu", language="en", voice="Adam"),
            chapter_render_identity(engine="vieneu", language="vi", voice="Linh"),
            chapter_render_identity(
                engine="vieneu", language="vi", voice="Adam", voice_source="cloned"
            ),
            chapter_render_identity(
                engine="qwen_customvoice",
                language="en",
                voice="Ryan",
                instruction="cheerful",
            ),
            chapter_render_identity(engine="vieneu", language="vi", voice="Adam", speed=1.5),
        ]
        for variant in variants:
            assert variant != base


class TestChapterRenderFreshness:
    def test_missing_wav_is_stale(self, tmp_path) -> None:
        library, book_id = make_library(tmp_path)
        identity = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        assert library.chapter_render_fresh(book_id, 0, identity) is False

    def test_saved_identity_is_fresh(self, tmp_path) -> None:
        library, book_id = make_library(tmp_path)
        identity = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        library.save_chapter_audio(book_id, 0, sample())
        library.save_chapter_render_identity(book_id, 0, identity)
        assert library.chapter_render_fresh(book_id, 0, identity) is True

    def test_engine_switch_stales_cache(self, tmp_path) -> None:
        library, book_id = make_library(tmp_path)
        vieneu = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        library.save_chapter_audio(book_id, 0, sample())
        library.save_chapter_render_identity(book_id, 0, vieneu)
        qwen = chapter_render_identity(engine="qwen_customvoice", language="en", voice="Ryan")
        assert library.chapter_render_fresh(book_id, 0, qwen) is False
        assert library.chapter_render_fresh(book_id, 0, vieneu) is True

    def test_legacy_cache_without_sidecar_stays_fresh_for_vieneu(self, tmp_path) -> None:
        library, book_id = make_library(tmp_path)
        library.save_chapter_audio(book_id, 0, sample())
        legacy = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        assert library.chapter_render_fresh(book_id, 0, legacy) is True
        qwen = chapter_render_identity(engine="qwen_customvoice", language="en", voice="Ryan")
        assert library.chapter_render_fresh(book_id, 0, qwen) is False

    def test_corrupt_sidecar_is_stale(self, tmp_path) -> None:
        library, book_id = make_library(tmp_path)
        identity = chapter_render_identity(engine="vieneu", language="vi", voice="Adam")
        library.save_chapter_audio(book_id, 0, sample())
        sidecar = library.chapter_wav_path(book_id, 0).with_name("ch_0000.render.json")
        sidecar.write_text("{broken", encoding="utf-8")
        assert library.chapter_render_fresh(book_id, 0, identity) is False
