"""Deterministic, public benchmark inputs identified by stable hashes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class CorpusEntry:
    scenario_id: str
    text: str
    language_class: str

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("scenario_id must not be blank")
        if not self.text.strip():
            raise ValueError("text must not be blank")
        if not self.language_class.strip():
            raise ValueError("language_class must not be blank")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def identity(self) -> dict[str, str | int]:
        return {
            "scenario_id": self.scenario_id,
            "text_sha256": self.sha256,
            "char_count": len(self.text),
            "language_class": self.language_class,
        }


def _fixed_length(base: str, length: int) -> str:
    if length < 1:
        raise ValueError("length must be positive")
    repeated = (base + " ") * ((length // (len(base) + 1)) + 2)
    return repeated[:length]


_VI_BASE = (
    "Đây là câu thử nghiệm cố định dùng để đo tốc độ tổng hợp tiếng nói trên thiết bị cục bộ."
)
_EN_BASE = "This is a fixed public benchmark sentence for local speech synthesis."


CORPUS: dict[str, CorpusEntry] = {
    "vi_20": CorpusEntry("vi_20", _fixed_length(_VI_BASE, 20), "vi"),
    "vi_50": CorpusEntry("vi_50", _fixed_length(_VI_BASE, 50), "vi"),
    "vi_256": CorpusEntry("vi_256", _fixed_length(_VI_BASE, 256), "vi"),
    "vi_512": CorpusEntry("vi_512", _fixed_length(_VI_BASE, 512), "vi"),
    "vi_2000": CorpusEntry("vi_2000", _fixed_length(_VI_BASE, 2000), "vi"),
    "vi_5000": CorpusEntry("vi_5000", _fixed_length(_VI_BASE, 5000), "vi"),
    "en_short": CorpusEntry("en_short", _fixed_length(_EN_BASE, 72), "en"),
    "code_switch": CorpusEntry(
        "code_switch",
        "Xin chào, this fixed sentence checks a Vietnamese and English code switch.",
        "code-switch",
    ),
    "numbers": CorpusEntry(
        "numbers",
        "Số thứ tự cố định: một, hai, ba, bốn, năm; 1, 2, 3, 4, 5.",
        "vi-numbers",
    ),
    "emotion": CorpusEntry(
        "emotion",
        "Niềm vui bình tĩnh lan tỏa, rồi sự ngạc nhiên trở thành hy vọng.",
        "vi-emotion",
    ),
    "multiline": CorpusEntry(
        "multiline",
        "Dòng đầu tiên của văn bản thử nghiệm.\nDòng thứ hai tiếp tục.\nDòng cuối cùng.",
        "vi-multiline",
    ),
    "punctuation_free": CorpusEntry(
        "punctuation_free",
        "van ban thu nghiem co dinh khong dau va khong dau cau",
        "vi-plain",
    ),
}


def get_corpus_entry(scenario_id: str) -> CorpusEntry:
    try:
        return CORPUS[scenario_id]
    except KeyError:
        raise KeyError(f"unknown benchmark scenario: {scenario_id}") from None
