import hashlib

from scripts.benchmarks.corpus import CORPUS, get_corpus_entry

EXPECTED_IDS = {
    "vi_20",
    "vi_50",
    "vi_256",
    "vi_512",
    "vi_2000",
    "vi_5000",
    "en_short",
    "code_switch",
    "numbers",
    "emotion",
    "multiline",
    "punctuation_free",
}


def test_corpus_contains_expected_ids() -> None:
    assert set(CORPUS) == EXPECTED_IDS


def test_corpus_entries_are_nonblank_and_hash_stable() -> None:
    for scenario_id, entry in CORPUS.items():
        assert entry.scenario_id == scenario_id
        assert entry.text.strip()
        assert isinstance(entry.text.encode("utf-8"), bytes)
        assert entry.sha256 == hashlib.sha256(entry.text.encode("utf-8")).hexdigest()
        assert entry.identity()["char_count"] == len(entry.text)


def test_get_corpus_entry_rejects_unknown_id() -> None:
    try:
        get_corpus_entry("missing")
    except KeyError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("unknown corpus ID should raise KeyError")
