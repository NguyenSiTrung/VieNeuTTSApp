"""Text segmentation: VieNeu parity, CJK boundaries/joins, and the IPC bound."""

from __future__ import annotations

import pytest

from vienetts_app.core.engine_profiles import QWEN_BASE, QWEN_CUSTOM, VIENEU
from vienetts_app.core.qwen_protocol import MAX_TEXT_CHARS
from vienetts_app.core.text_segmentation import (
    DEFAULT_MAX_CHARS,
    QWEN_MAX_CHARS,
    segment_limit_for,
    split_text_for_profile,
    split_text_for_streaming,
    uses_cjk_boundaries,
)

VI_TEXT = (
    "Xin chào, đây là một câu tiếng Việt có dấu. "
    "Câu thứ hai kiểm tra dấu câu! "
    "Và câu thứ ba kết thúc bằng dấu chấm hỏi? "
    "Cuối cùng là một câu dài hơn để chắc chắn việc gộp câu vẫn giữ nguyên hành vi cũ."
)
EN_TEXT = (
    "The first sentence is short. The second one is a little longer, "
    "and it keeps going so that packing has something to do! Does it work? Yes."
)
ZH_TEXT = "你好。世界。今天天气很好。我们一起去公园散步吧。谢谢你。"
JA_TEXT = "こんにちは。今日はいい天気ですね。公園へ行きましょう。"
KO_TEXT = "안녕하세요. 오늘 날씨가 좋네요. 공원에 갑시다."


class TestParity:
    """VieNeu/English output must be byte-identical to the pre-Task-3.4 rules."""

    @pytest.mark.parametrize("text", [VI_TEXT, EN_TEXT, "short text", "", "   \n\t "])
    def test_profile_split_matches_streaming_split_for_space_languages(self, text: str) -> None:
        for language in ("vi", "en", "de", "auto", ""):
            assert split_text_for_profile(text, language) == split_text_for_streaming(text)

    @pytest.mark.parametrize("max_chars", [1, 7, 40, 60, 512])
    def test_explicit_max_chars_is_honored(self, max_chars: int) -> None:
        assert split_text_for_profile(
            VI_TEXT, "vi", max_chars=max_chars
        ) == split_text_for_streaming(VI_TEXT, max_chars=max_chars)

    def test_engine_reexports_the_same_functions(self) -> None:
        from vienetts_app.core import engine

        assert engine.split_text_for_streaming is split_text_for_streaming
        assert engine.split_text_for_profile is split_text_for_profile
        assert engine.segment_limit_for is segment_limit_for
        assert engine.DEFAULT_MAX_CHARS == DEFAULT_MAX_CHARS

    def test_short_text_stays_one_segment(self) -> None:
        assert split_text_for_profile("Xin chào bạn.", "vi") == ["Xin chào bạn."]
        assert split_text_for_profile("你好。", "zh") == ["你好。"]

    def test_empty_and_whitespace_only_text_yields_nothing(self) -> None:
        assert split_text_for_profile("", "zh") == []
        assert split_text_for_profile("   \n ", "ja") == []

    def test_units_are_still_split_at_sentence_terminators(self) -> None:
        segments = split_text_for_profile(VI_TEXT, "vi", max_chars=60)
        assert len(segments) > 1
        assert all(len(segment) <= 60 for segment in segments)
        # No sentence content is lost or reordered by packing.
        assert " ".join(segments).split() == VI_TEXT.split()

    def test_unknown_language_code_keeps_the_space_rules(self) -> None:
        assert split_text_for_profile(EN_TEXT, "xx") == split_text_for_streaming(EN_TEXT)
        assert uses_cjk_boundaries("xx", ZH_TEXT) is False


class TestCjkBoundaries:
    def test_chinese_is_cut_at_full_stops_not_at_the_cap(self) -> None:
        # The space rule sees ONE unit here (no whitespace after 。), so it has
        # to cut wherever the cap lands; the CJK rule cuts at the full stops.
        limit = 14
        space_rule = split_text_for_streaming(ZH_TEXT, max_chars=limit)
        cjk_rule = split_text_for_profile(ZH_TEXT, "zh", max_chars=limit)
        assert all(segment.endswith("。") for segment in cjk_rule)
        assert not all(segment.endswith("。") for segment in space_rule)
        assert all(len(segment) <= limit for segment in cjk_rule)
        assert "".join(cjk_rule) == ZH_TEXT

    def test_no_space_is_inserted_between_chinese_sentences(self) -> None:
        segments = split_text_for_profile(ZH_TEXT, "zh", max_chars=8)
        assert " " not in "".join(segments)
        assert "".join(segments) == ZH_TEXT
        assert segments[0] == "你好。世界。"  # packed to the cap, no separator
        assert all(len(segment) <= 8 for segment in segments)

    def test_japanese_is_cut_at_full_stops(self) -> None:
        assert split_text_for_profile(JA_TEXT, "ja", max_chars=200) == [JA_TEXT]
        segments = split_text_for_profile(JA_TEXT, "ja", max_chars=12)
        assert segments == ["こんにちは。", "今日はいい天気ですね。", "公園へ行きましょう。"]
        assert "".join(segments) == JA_TEXT

    def test_korean_keeps_its_own_sentence_spaces(self) -> None:
        # Korean writes a space after the full stop: the source separator is
        # preserved instead of being dropped or duplicated.
        assert split_text_for_profile(KO_TEXT, "ko", max_chars=200) == [KO_TEXT]
        segments = split_text_for_profile(KO_TEXT, "ko", max_chars=12)
        assert segments == ["안녕하세요.", "오늘 날씨가 좋네요.", "공원에 갑시다."]
        assert " ".join(segments) == KO_TEXT

    def test_chinese_with_author_spaces_keeps_them(self) -> None:
        spaced = "你好。 世界。"
        assert split_text_for_profile(spaced, "zh", max_chars=100) == [spaced]

    def test_auto_follows_the_text_script(self) -> None:
        assert split_text_for_profile(ZH_TEXT, "auto", max_chars=14) == split_text_for_profile(
            ZH_TEXT, "zh", max_chars=14
        )
        assert split_text_for_profile(EN_TEXT, "auto") == split_text_for_streaming(EN_TEXT)
        assert uses_cjk_boundaries("auto", ZH_TEXT) is True
        assert uses_cjk_boundaries("auto", EN_TEXT) is False

    def test_cjk_hard_split_never_inserts_a_space(self) -> None:
        long_zh = "这是一句没有任何标点符号的很长的中文句子" * 6
        segments = split_text_for_profile(long_zh, "zh", max_chars=20)
        assert len(segments) > 1
        assert all(len(segment) <= 20 for segment in segments)
        assert " " not in "".join(segments)
        assert "".join(segments) == long_zh

    def test_mixed_cjk_and_latin_keeps_word_boundaries(self) -> None:
        text = "你好 world 你好 world"
        segments = split_text_for_profile(text, "zh", max_chars=12)
        assert all(len(segment) <= 12 for segment in segments)
        assert " ".join(segments) == text  # the source spaces are preserved

    def test_uses_cjk_boundaries_reads_the_language_code(self) -> None:
        assert uses_cjk_boundaries("zh") is True
        assert uses_cjk_boundaries("ZH") is True
        assert uses_cjk_boundaries(" ja ") is True
        assert uses_cjk_boundaries("ko") is True
        assert uses_cjk_boundaries("vi") is False
        assert uses_cjk_boundaries("en") is False
        assert uses_cjk_boundaries("yue") is False  # not a Qwen profile language
        assert uses_cjk_boundaries("", "你好") is True

    def test_quoted_and_bracketed_sentences_keep_their_closers(self) -> None:
        text = "「你好。」他说。「再见。」"
        assert split_text_for_profile(text, "zh", max_chars=100) == [text]
        segments = split_text_for_profile(text, "zh", max_chars=6)
        assert segments == ["「你好。」", "他说。", "「再见。」"]
        assert "".join(segments) == text


class TestSegmentLimits:
    def test_limit_per_profile(self) -> None:
        assert segment_limit_for(VIENEU) == DEFAULT_MAX_CHARS
        assert segment_limit_for(QWEN_CUSTOM) == QWEN_MAX_CHARS
        assert segment_limit_for(QWEN_BASE) == QWEN_MAX_CHARS

    def test_every_qwen_segment_fits_the_ipc_bound(self) -> None:
        # A pathological document: no sentence terminators, no spaces, longer
        # than the protocol bound. Every segment must still fit one frame.
        text = "字" * (MAX_TEXT_CHARS * 3 + 7)
        for profile in (QWEN_CUSTOM, QWEN_BASE):
            limit = segment_limit_for(profile)
            segments = split_text_for_profile(text, "zh", max_chars=limit)
            assert segments
            assert max(len(segment) for segment in segments) <= MAX_TEXT_CHARS
            assert "".join(segments) == text

    def test_limit_never_exceeds_the_protocol_bound(self) -> None:
        for profile in (VIENEU, QWEN_CUSTOM, QWEN_BASE):
            assert segment_limit_for(profile) <= MAX_TEXT_CHARS


class TestArgumentValidation:
    def test_max_chars_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_chars must be >= 1"):
            split_text_for_profile("hello", "en", max_chars=0)
        with pytest.raises(ValueError, match="max_chars must be >= 1"):
            split_text_for_streaming("hello", max_chars=-1)
