"""Script-aware word counts / duration estimates (editor metric chips).

Pins the CJK fix (bead VieNeuTTSApp-m9mr): a whitespace split collapses a
whole Chinese paragraph to one "word", so the Text/Paragraph tab chips
showed nonsense (~1s) for zh/ja and ~2x-underestimates for ko. Expected
values below are hand-derived from per-script speech rates, not from the
implementation.
"""

from __future__ import annotations

import pytest

from vienetts_app.core.text_metrics import count_words, estimate_duration_seconds

# 23 Han characters + 5 full stops (punctuation must not count as words).
ZH_TEXT = "你好。世界。今天天气很好。我们一起去公园散步吧。谢谢你。"
# 6 space-separated eojeol, 19 Hangul syllables.
KO_TEXT = "안녕하세요. 오늘 날씨가 좋네요. 공원에 갑시다."
# 7 Han characters (今日天気公園行) + 17 kana.
JA_TEXT = "こんにちは。今日はいい天気ですね。公園へ行きましょう。"

VI_TEXT = (
    "Xin chào, đây là một câu tiếng Việt có dấu. "
    "Câu thứ hai kiểm tra dấu câu! "
    "Và câu thứ ba kết thúc bằng dấu chấm hỏi? "
    "Cuối cùng là một câu dài hơn để chắc chắn việc gộp câu vẫn giữ nguyên hành vi cũ."
)
EN_TEXT = "The first sentence is short. The second one is a little longer."


class TestSpaceScriptParity:
    """Text without Han/kana keeps the old whitespace-split numbers exactly."""

    @pytest.mark.parametrize("text", [VI_TEXT, EN_TEXT, "ok", "  multiple   spaces  "])
    def test_count_matches_whitespace_split(self, text: str) -> None:
        assert count_words(text) == len(text.split())

    @pytest.mark.parametrize("text", [VI_TEXT, EN_TEXT, "ok"])
    def test_estimate_matches_old_words_per_2_5s(self, text: str) -> None:
        assert estimate_duration_seconds(text) == round(len(text.split()) / 2.5)

    @pytest.mark.parametrize("text", ["", "   \n\t "])
    def test_empty_text_is_zero(self, text: str) -> None:
        assert count_words(text) == 0
        assert estimate_duration_seconds(text) == 0


class TestChinese:
    def test_each_han_character_counts_as_one_word(self) -> None:
        # 23 characters — the old whitespace split saw exactly 1 "word".
        assert count_words(ZH_TEXT) == 23
        assert len(ZH_TEXT.split()) == 1  # the bug this module fixes

    def test_duration_uses_han_speech_rate(self) -> None:
        # 23 chars at 240 chars/min = 5.75s -> ~6s (old formula: 1 word -> ~1s).
        assert estimate_duration_seconds(ZH_TEXT) == 6

    def test_cjk_punctuation_is_not_a_word(self) -> None:
        # 。！？ are pauses, not words — the Han strip must not leave them
        # behind as junk tokens (one per sentence in ZH_TEXT).
        assert count_words("。。。！？") == 0


class TestJapanese:
    def test_kana_and_kanji_both_count(self) -> None:
        # 7 Han + 17 kana characters; no spaces anywhere.
        assert count_words(JA_TEXT) == 24
        assert len(JA_TEXT.split()) == 1

    def test_duration_mixes_both_rates(self) -> None:
        # 7/4s + 17/6s = 4.58s -> ~5s.
        assert estimate_duration_seconds(JA_TEXT) == 5


class TestKorean:
    def test_eojeol_still_counted_by_spaces(self) -> None:
        # Korean writes words with spaces (어절 convention): 6 eojeol.
        assert count_words(KO_TEXT) == 6

    def test_duration_uses_syllable_rate_not_wpm(self) -> None:
        # 19 syllables at 270/min = 4.22s -> ~4s. The old 150-wpm formula
        # gave round(6/2.5) = 2s — roughly half.
        assert estimate_duration_seconds(KO_TEXT) == 4
        assert round(len(KO_TEXT.split()) / 2.5) == 2  # the underestimation fixed

    def test_bare_syllables_without_spaces(self) -> None:
        assert count_words("안녕하세요") == 1
        assert estimate_duration_seconds("안녕하세요") == 1  # 5/4.5 = 1.11


class TestMixedScript:
    def test_latin_and_han_sum(self) -> None:
        assert count_words("Xin chào 你好") == 4  # 2 tokens + 2 Han
        assert estimate_duration_seconds("Xin chào 你好") == 1  # 0.8 + 0.5

    def test_han_removal_does_not_join_latin_tokens(self) -> None:
        # Without a space on substitution, "hello你好world" would be one
        # token and both Latin words would vanish.
        assert count_words("hello你好world") == 4
