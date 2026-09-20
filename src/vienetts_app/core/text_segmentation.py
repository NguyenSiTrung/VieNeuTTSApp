"""Pure text segmentation for the synthesis engines (FR-4.6d, Task 3.4).

Long text is synthesized one bounded, sentence-aware segment at a time. The
cap exists for two reasons:

- **VieNeu** — ONNX Runtime's CPU arena grows with the largest single
  ``infer_stream`` workload and never shrinks (spike §18, bead
  VieNeuTTSApp-u5c), so bounding segments bounds RSS for arbitrarily long
  documents.
- **Qwen** — every segment crosses process IPC as one ``synthesize`` frame, so
  it must stay inside :data:`vienetts_app.core.qwen_protocol.MAX_TEXT_CHARS`;
  the host also synthesizes exactly one segment per job.

Everything here is pure string work: no Qt, no engine, no disk.

Two rules matter for quality, and both are script-aware:

1. **Boundaries.** Latin punctuation ends a sentence and is followed by a
   space, so ``[.!?]`` plus trailing whitespace is the boundary. CJK full
   stops (``。！？``) end a sentence with *nothing* after them, so a
   whitespace-requiring boundary rule leaves a whole Chinese or Japanese
   paragraph as one unit that then gets hard-split mid-sentence at the cap.
   CJK languages therefore use a boundary rule that does not need the space.
2. **Joins.** A segment is a *packed* run of units, so the packer inserts a
   separator between them. The separator is taken from the source text — the
   whitespace the boundary actually consumed — never invented. That keeps
   VieNeu/English output byte-identical to the pre-Task-3.4 rules (their
   boundaries always consume a space) and means a space is never inserted
   between two Chinese sentences that had none.
"""

from __future__ import annotations

import re

from vienetts_app.core.engine_profiles import EngineId, get_capabilities
from vienetts_app.core.qwen_protocol import MAX_TEXT_CHARS

# App-level segment cap for long-text STREAMING synthesis (FR-4.6d).
#
# Why 512: the SDK's own AR chunking inside ``infer_stream`` is capped at
# max_chars=256 (vieneu/v3turbo.py infer_stream signature +
# normalize_to_chunks_v3 in vieneu_utils/phonemize_text.py), so any app
# segment ≥256 chars adds no extra prefill work per character — the model
# workload per infer_stream call is set by the SDK's 256-char chunks either
# way. Doubling it to 512 halves the number of app-level dispatches while
# keeping the largest single infer_stream workload bounded at ~2 SDK chunks,
# so ONNX Runtime's arena grows with SEGMENT size, not document size (spike
# §18 measured a ~2.5 GB plateau when one infer_stream call covers a whole
# document; budget < 2 GB).
DEFAULT_MAX_CHARS = 512

# Qwen segments are dispatched as one IPC frame each; the same 512-char cap
# keeps progress granularity and per-segment memory in the same range as
# VieNeu, and ``segment_limit_for`` additionally clamps it to the protocol
# bound so a future cap change can never send an oversized frame.
QWEN_MAX_CHARS = 512

# Sentence-terminal punctuation that closes a segment unit: ASCII .!?,
# Unicode … (U+2026) and fullwidth ！？。; optional trailing closing
# quotes/brackets stay attached to the sentence; the match ends at the
# following whitespace (or end of text). Comma/semicolon are deliberately
# NOT boundaries (they do not reliably end an intonation unit); newlines are
# folded into the same terminator's trailing whitespace.
_SENTENCE_END_RE = re.compile(r"[.!?…！？。]+[\"'”’)\]]*(?:\s+|$)")

# CJK boundary rule: the same terminal punctuation (plus the fullwidth
# semicolon, which ends a clause) closes a unit even when the next character
# follows immediately — Chinese and Japanese write sentences with no space
# after the full stop. CJK closing brackets belong to the sentence they close,
# so they stay attached. Trailing whitespace is still consumed when present, so
# Korean (and CJK text laid out with spaces or newlines) keeps its own
# separator.
_CJK_SENTENCE_END_RE = re.compile(r"[.!?…！？。；]+[\"'”’)\]」』】〕》〉]*\s*")

# Languages whose writing system does not separate sentences with spaces.
_NO_SPACE_LANGUAGES = frozenset({"zh", "ja", "ko"})

# Scripts written without inter-word spaces, used to decide the boundary rule
# for the ambiguous ``auto`` language: CJK ideographs, kana, halfwidth katakana
# and Hangul syllables. Punctuation-only text is deliberately not detected
# here.
_CJK_SCRIPT_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\uac00-\ud7af]"
)


def uses_cjk_boundaries(language: str, text: str = "") -> bool:
    """Whether ``language`` needs CJK sentence boundaries (no trailing space).

    ``zh``/``ja``/``ko`` always do. An explicit space-using language (``vi``,
    ``en``, …) never does, so its segmentation is byte-identical to
    :func:`split_text_for_streaming`. ``auto`` (and an empty code) follows the
    text's own script: only a text that actually contains CJK/Hangul script
    switches rules.
    """
    code = (language or "").strip().lower()
    if code in _NO_SPACE_LANGUAGES:
        return True
    if code and code != "auto":
        return False
    return _CJK_SCRIPT_RE.search(text or "") is not None


def segment_limit_for(profile: EngineId) -> int:
    """Largest segment ``profile`` may synthesize in one engine call.

    VieNeu is bounded by the ONNX arena cap; a Qwen segment additionally has to
    fit one ``synthesize`` frame, so the cap is clamped to the protocol bound
    (:data:`MAX_TEXT_CHARS`) rather than trusted to be smaller.
    """
    capabilities = get_capabilities(profile)
    if capabilities.runtime == "qwen_host":
        return min(QWEN_MAX_CHARS, MAX_TEXT_CHARS)
    return DEFAULT_MAX_CHARS


def _sentence_units(cleaned: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Cut ``cleaned`` into ``(unit, separator)`` pairs, keeping punctuation.

    A unit is everything up to (and including) a run of terminal punctuation
    plus its trailing whitespace; ``separator`` is that trailing whitespace
    collapsed to a single space (``""`` when the boundary had none — a CJK
    full stop written without a following space). Units are stripped and empty
    units are dropped; the tail after the last boundary is a unit with no
    separator.
    """
    units: list[tuple[str, str]] = []
    start = 0
    for match in pattern.finditer(cleaned):
        end = match.end()
        raw = cleaned[start:end]
        unit = raw.strip()
        if unit:
            units.append((unit, " " if raw != raw.rstrip() else ""))
        start = end
    tail = cleaned[start:].strip()
    if tail:
        units.append((tail, ""))
    return units


def _pack_units(units: list[tuple[str, str]], max_chars: int) -> list[str]:
    """Greedily pack ``(unit, separator)`` pairs into ≤ ``max_chars`` segments."""
    segments: list[str] = []
    current = ""
    separator = ""
    for unit, next_separator in units:
        if len(unit) > max_chars:
            if current:
                segments.append(current)
                current = ""
            remaining = unit
            while len(remaining) > max_chars:
                cut = remaining.rfind(" ", 0, max_chars + 1)
                if cut <= 0:
                    cut = max_chars
                segments.append(remaining[:cut].strip())
                remaining = remaining[cut:].strip()
            current = remaining
        elif not current:
            current = unit
        elif len(current) + len(separator) + len(unit) <= max_chars:
            current = f"{current}{separator}{unit}"
        else:
            segments.append(current)
            current = unit
        separator = next_separator
    if current:
        segments.append(current)
    return segments


def split_text_for_streaming(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split ``text`` into segments of ≤ ``max_chars`` at natural boundaries.

    Pure function used by chunked stream dispatch so ONE ``infer_stream``
    call never sees more than ``max_chars`` characters: ONNX Runtime's CPU
    arena grows with the largest single workload and never shrinks (spike
    §18, bead VieNeuTTSApp-u5c), so bounding segments bounds RSS for
    arbitrarily long documents.

    Rules:
    - Text is first cut into units at sentence terminators (``.!?!…`` etc.,
      optionally followed by closing quotes/brackets) and newlines; the
      terminal punctuation stays attached to its sentence. Sentences are
      NEVER broken mid-sentence while they fit within ``max_chars``;
      consecutive units are greedily packed into one segment until adding
      the next would exceed the cap.
    - A single unit longer than ``max_chars`` (a runaway run without
      terminal punctuation) is hard-split AT the cap, preferring the last
      space inside the window so words stay whole where possible; only a
      word longer than ``max_chars`` itself is split mid-word.
    - Empty and whitespace-only segments are dropped.
    - Deterministic; unicode/diacritics safe (pure str slicing, no NFC/NFD
      normalization that could decompose Vietnamese combining marks).

    This is the space-separated rule set. CJK languages use
    :func:`split_text_for_profile`, which also splits at full stops that have
    no following space.

    Returns ``[text]`` (stripped) when it already fits, so short texts keep
    byte-identical downstream behavior to today's non-chunked path.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    return _pack_units(_sentence_units(cleaned, _SENTENCE_END_RE), max_chars)


def split_text_into_sentences(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split ``text`` into sentence/paragraph units, each capped at ``max_chars``.

    Unlike ``split_text_for_streaming`` which greedily packs consecutive sentences
    into one segment up to ``max_chars``, this keeps individual sentence units separate
    so pauses / silence can be inserted between sentences.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    units = _sentence_units(cleaned, _SENTENCE_END_RE)
    if not units:
        return []
    segments: list[str] = []
    for unit, _separator in units:
        if len(unit) <= max_chars:
            segments.append(unit)
        else:
            segments.extend(split_text_for_streaming(unit, max_chars=max_chars))
    return segments


def split_text_for_profile(
    text: str, language: str = "", max_chars: int = DEFAULT_MAX_CHARS
) -> list[str]:
    """Split ``text`` into segments bounded for one engine profile.

    ``language`` is the app-level code from the job's engine context (``vi``,
    ``en``, ``zh``, … or ``auto``). Space-using languages produce exactly what
    :func:`split_text_for_streaming` produces — VieNeu and English behavior is
    unchanged. CJK languages (see :func:`uses_cjk_boundaries`) additionally
    treat a full stop as a sentence boundary when no space follows it, so a
    Chinese or Japanese paragraph is cut at its sentences instead of at an
    arbitrary character offset, and units are joined with the source's own
    separator: nothing is ever inserted between two CJK sentences.

    ``max_chars`` is the per-profile cap; callers use :func:`segment_limit_for`
    so a Qwen segment can never exceed the IPC frame bound.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    pattern = _CJK_SENTENCE_END_RE if uses_cjk_boundaries(language, cleaned) else _SENTENCE_END_RE
    return _pack_units(_sentence_units(cleaned, pattern), max_chars)
