"""CJK-aware text segmentation (Phase 2 Task 3)."""

from vienetts_app.core.engine import split_text_for_streaming, split_text_into_sentences


def zh_sentence(n: int) -> str:
    bodies = ["今天天气很好", "我们一起去公园散步", "晚上回家吃饭休息", "明天继续努力工作"]
    return bodies[n % len(bodies)] + "。"


def test_cjk_no_spaces_invented_when_packing() -> None:
    text = "".join(zh_sentence(i) for i in range(80))
    assert " " not in text
    segments = split_text_for_streaming(text)
    assert segments
    assert all(len(s) <= 512 for s in segments)
    assert "".join(segments) == text


def test_cjk_strong_punctuation_splits() -> None:
    text = "你好世界！今天好吗？很好。"
    assert split_text_for_streaming(text) == [text]
    long_text = "".join(f"第{i}章内容概要。" for i in range(60))
    segments = split_text_for_streaming(long_text)
    assert len(segments) > 1
    assert "".join(segments) == long_text


def test_korean_spaced_boundaries_keep_spaces() -> None:
    text = " ".join(["안녕하세요. 반갑습니다. 오늘 날씨가 좋네요."] * 12)
    segments = split_text_for_streaming(text)
    assert all(len(s) <= 512 for s in segments)


def test_cjk_weak_clause_boundaries() -> None:
    clauses = ["春", "夏", "秋", "冬"]
    text = "、".join(clauses * 200) + "。"
    assert " " not in text
    segments = split_text_for_streaming(text)
    assert "".join(segments) == text
    assert any(s.endswith("、") for s in segments)


def test_hard_split_prefers_cjk_clause_punctuation() -> None:
    unit = "甲" * 300 + "，" + "乙" * 300
    segments = split_text_for_streaming(unit, max_chars=500)
    assert all(len(s) <= 500 for s in segments)
    assert segments[0].endswith("，")
    assert "".join(segments) == unit


def test_fullwidth_full_stop_splits() -> None:
    text = "おはよう．こんにちは．" * 60
    segments = split_text_for_streaming(text)
    assert "".join(segments) == text
    assert len(segments) > 1


def test_japanese_mixed_scripts_no_spaces() -> None:
    text = "こんにちは、世界！今日はいい天気ですね。" * 15
    assert " " not in text
    segments = split_text_for_streaming(text)
    assert "".join(segments) == text
    assert all(len(s) <= 512 for s in segments)


def test_sentences_keep_clause_units_separate() -> None:
    units = split_text_into_sentences("春、夏、秋、冬。")
    assert units == ["春、", "夏、", "秋、", "冬。"]


def test_vi_en_behavior_unchanged() -> None:
    text = "Xin chào Việt Nam! Hôm nay trời đẹp. Bạn khỏe không?"
    assert split_text_for_streaming(text) == [text]
