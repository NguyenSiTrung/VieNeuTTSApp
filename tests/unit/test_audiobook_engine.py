"""Audiobook engine-aware chapter cache (Phase 5 Task 2)."""

import pytest

pytest.importorskip("PySide6")

from tests.unit.test_audiobook_controller import Harness  # noqa: E402


@pytest.fixture()
def harness(qcoreapp, tmp_path):
    return Harness(tmp_path)


class TestEngineAwareChapterCache:
    def test_render_saves_identity_sidecar(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        lib = harness.audiobook_lib
        book = harness.audiobook.currentBookId
        identity = lib.load_chapter_render_identity(book, 0)
        assert identity is not None
        assert identity["engine"] == "vieneu"
        assert identity["voice"] == harness.app.defaultVoice

    def test_engine_switch_rerenders_stale_chapter(self, harness: Harness) -> None:
        harness.open_sample()
        chapter_zero = harness.audiobook._state.chapters[0].text  # noqa: SLF001

        def chapter_zero_renders() -> int:
            return sum(1 for job in harness.worker.submitted if job.request.text == chapter_zero)

        harness.render(0)
        assert chapter_zero_renders() == 1
        # Same engine: plays straight from the cache (only the chapter-1 pipeline submits).
        harness.audiobook.playChapter(0)
        assert chapter_zero_renders() == 1
        # Switched engine: the VieNeu render is stale → re-render.
        harness.app.ttsEngine = "qwen_customvoice"
        harness.app.ttsLanguage = "en"
        harness.audiobook.playChapter(0)
        assert chapter_zero_renders() == 2
        request = harness.worker.submitted[-1].request
        assert request.engine == "qwen_customvoice"

    def test_legacy_vieneu_cache_without_sidecar_stays_fresh(self, harness: Harness) -> None:
        harness.open_sample()
        chapter_zero = harness.audiobook._state.chapters[0].text  # noqa: SLF001
        harness.render(0)
        lib = harness.audiobook_lib
        book = harness.audiobook.currentBookId
        lib.chapter_render_path(book, 0).unlink()
        harness.audiobook.playChapter(0)
        # Pre-multiengine VieNeu render reused for chapter 0 (pipeline may render chapter 1).
        assert all(job.request.text != chapter_zero for job in harness.worker.submitted[1:])

    def test_missing_sidecar_under_qwen_rerenders(self, harness: Harness) -> None:
        harness.open_sample()
        chapter_zero = harness.audiobook._state.chapters[0].text  # noqa: SLF001
        harness.render(0)
        lib = harness.audiobook_lib
        book = harness.audiobook.currentBookId
        lib.chapter_render_path(book, 0).unlink()
        harness.app.ttsEngine = "qwen_customvoice"
        harness.app.ttsLanguage = "en"
        harness.audiobook.playChapter(0)
        assert sum(1 for job in harness.worker.submitted if job.request.text == chapter_zero) == 2
