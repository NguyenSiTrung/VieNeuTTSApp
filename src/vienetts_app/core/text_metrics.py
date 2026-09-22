"""Script-aware word counts and duration estimates for editor metric chips.

The Text tab and the Paragraph tab show live ``"%1 từ · %2 ký tự · ~%3s"``
chips while the user types. A whitespace split (``str.split()``) is only a
word count for scripts that *write* words with spaces. The Qwen profiles
synthesize Chinese, Japanese and Korean, where it collapses:

- **Chinese/Japanese** — sentences have no inter-word spaces, so a whole
  paragraph is ONE ``\\S+`` token and the duration estimate reads ``~1s``.
- **Korean** — eojeol (space-separated units) are agglutinative, 2-3
  syllables each, so a flat 150 wpm underestimates roughly 2x.

The fix mirrors :mod:`vienetts_app.core.text_segmentation`'s script-aware
boundary rule: let the text's own script decide the unit, not the language
setting. Each Han/kana character counts as one word (the ``字数``/``かな数``
convention), Hangul keeps its space-delimited eojeol (the ``어절`` convention),
and every other script keeps the whitespace-token count — so Vietnamese,
English and the rest stay byte-identical to the old numbers.

Duration is estimated from per-script speech rates (all approximations for a
``~`` chip, chosen from typical TTS/audiobook pace):

- 150 words/min for space-delimited scripts (the rate the old code pinned);
- 240 Han characters/min (Mandarin news/TTS read pace);
- 360 kana characters/min (Japanese runs ~6 morae/sec);
- 270 Hangul syllables/min (Korean standard reading pace).

Mixed-script text sums each script's contribution, so a Vietnamese chapter
with embedded Chinese quotes estimates sensibly. Everything here is pure
string work: no Qt, no engine, no settings — same contract as
``text_segmentation``.
"""

from __future__ import annotations

import re

# Han ideographs (CJK Unified + Ext A + compatibility). Punctuation lives in
# other blocks and is deliberately excluded: 。！？ are pauses, not words.
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Hiragana + katakana letters (incl. the prolonged sound mark ー and the
# halfwidth forms), excluding CJK punctuation blocks (\u3000-\u303f,
# \uff61-\uff65) which are pauses, not words.
_KANA_RE = re.compile(r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f]")

# Precomposed Hangul syllables. Composing jamo blocks are not counted
# individually — two jamo are one syllable, not two.
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")

# CJK punctuation: CJK Symbols and Punctuation (、。「」《》… incl. U+3002 。)
# plus the fullwidth/halfwidth punctuation slots of U+FF00–FF65. Fullwidth
# letters/digits are deliberately NOT here — they are word content. Without
# this class a Chinese full stop would survive the Han strip and count as a
# "word", one per sentence.
_CJK_PUNCT_RE = re.compile(
    r"[\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]"
)

# Space-delimited scripts (Latin, Cyrillic, Vietnamese, …) keep the old
# whitespace-token word count.
_WORDS_PER_MINUTE = 150.0
_HAN_CHARS_PER_MINUTE = 240.0
_KANA_CHARS_PER_MINUTE = 360.0
_HANGUL_SYLLABLES_PER_MINUTE = 270.0


def _word_token_count(remainder: str) -> int:
    """Whitespace tokens that contain at least one alphanumeric character.

    Punctuation-only tokens are dropped: after CJK stripping, the ASCII
    periods of Korean eojeol or stray ellipses would otherwise each count
    as a word (``"안녕하세요."`` → token ``"."``) and inflate the estimate.
    """
    return sum(1 for token in remainder.split() if any(ch.isalnum() for ch in token))


def _strip_cjk(text: str, hangul: bool) -> str:
    """Replace Han/kana/punctuation (and optionally Hangul) with spaces.

    Spaces, not ``""``: without them ``"hello你好world"`` would re-join into
    one token and the Latin words would vanish from the count. CJK
    punctuation goes first so its replacement cannot fuse two word scripts
    into one token either.
    """
    remainder = _CJK_PUNCT_RE.sub(" ", text)
    remainder = _KANA_RE.sub(" ", _HAN_RE.sub(" ", remainder))
    if hangul:
        remainder = _HANGUL_RE.sub(" ", remainder)
    return remainder


def count_words(text: str) -> int:
    """Word-equivalent count for the metric chip.

    Han and kana characters count one each (no-space scripts); every other
    script — Hangul included — counts whitespace tokens. For text without
    Han/kana this equals ``len(text.split())`` exactly, so space-delimited
    languages keep the numbers the old chip showed.
    """
    if not text or not text.strip():
        return 0
    han = len(_HAN_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    tokens = _word_token_count(_strip_cjk(text, hangul=False))
    return han + kana + tokens


def estimate_duration_seconds(text: str) -> int:
    """Estimated spoken duration in whole seconds (``0`` for empty text).

    Each script contributes at its own speech rate, so the same string
    without Han/kana/Hangul reproduces the old ``words / 2.5`` estimate.
    """
    if not text or not text.strip():
        return 0
    han = len(_HAN_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    hangul = len(_HANGUL_RE.findall(text))
    words = _word_token_count(_strip_cjk(text, hangul=True))
    seconds = (
        han / _HAN_CHARS_PER_MINUTE * 60.0
        + kana / _KANA_CHARS_PER_MINUTE * 60.0
        + hangul / _HANGUL_SYLLABLES_PER_MINUTE * 60.0
        + words / _WORDS_PER_MINUTE * 60.0
    )
    return round(seconds)
