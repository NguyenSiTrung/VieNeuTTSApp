"""Generic voice descriptors and capability-aware catalogs (Phase 4 Task 1).

One descriptor type for every engine: VieNeu presets (with North/Central/
South regional grouping parsed from the SDK description format), Qwen
CustomVoice fixed speakers (with native languages from the official model
card), and Qwen Base reference voices (no presets — enrollment only).
Cloned-voice persistence stays engine-isolated (Phase 4 Task 3); this
module only describes and filters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from vienetts_app.core.backends import (
    QWEN_BASE,
    QWEN_CUSTOMVOICE,
    VIENEU,
    BackendCapabilityError,
    EngineId,
    get_capabilities,
)

VoiceKind = Literal["preset", "cloned"]

# Catalog groups, fixed order (FR-3.1: North/Central/South + fallback +
# cloned). Display labels are Vietnamese per the UI language.
_REGION_GROUPS: tuple[tuple[str, str], ...] = (
    ("Bắc", "Bắc"),
    ("Trung", "Trung"),
    ("Nam", "Nam"),
)
FALLBACK_GROUP = "Khác"
CLONED_GROUP = "Đã sao chép"
_REGION_IDS = frozenset(region for region, _ in _REGION_GROUPS)


def _parse_region(description: str) -> str | None:
    """Extract the region token from ``"Nam · Bắc · Phong cách ..."``.

    The middle ``·``-separated token is the region (Bắc/Trung/Nam). Returns
    None when the description does not match the pattern.
    """
    parts = [p.strip() for p in description.split("·")]
    if len(parts) != 3:
        return None
    return parts[1] if parts[1] in {"Bắc", "Trung", "Nam"} else None


def _display_label(entry: dict[str, str], region: str | None) -> str:
    """Human label for a preset: prefer the full description, else the name."""
    if region is not None:
        return f"{entry['name']} — {entry['description']}"
    return entry.get("description") or entry["name"]


@dataclass(frozen=True)
class VoiceDescriptor:
    """One selectable voice, backend-neutral."""

    voice_id: str
    engine: str
    kind: VoiceKind
    languages: tuple[str, ...]
    label: str = ""
    description: str = ""
    gender: str = ""
    style: str = ""
    region: str | None = None  # VieNeu N/C/S grouping; None = fallback
    native_language: str = ""  # Qwen fixed speaker's native language


@dataclass(frozen=True)
class QwenSpeaker:
    """Fixed CustomVoice speaker (official model-card table)."""

    name: str
    description: str
    native_language: str


QWEN_SPEAKERS: tuple[QwenSpeaker, ...] = (
    QwenSpeaker("Vivian", "Bright young female voice.", "Chinese"),
    QwenSpeaker("Serena", "Warm, gentle young female voice.", "Chinese"),
    QwenSpeaker("Uncle_Fu", "Seasoned male voice, mellow timbre.", "Chinese"),
    QwenSpeaker("Dylan", "Youthful Beijing male voice.", "Chinese (Beijing)"),
    QwenSpeaker("Eric", "Lively Chengdu male voice.", "Chinese (Sichuan)"),
    QwenSpeaker("Ryan", "Dynamic male voice with rhythm.", "English"),
    QwenSpeaker("Aiden", "Sunny American male voice.", "English"),
    QwenSpeaker("Ono_Anna", "Playful Japanese female voice.", "Japanese"),
    QwenSpeaker("Sohee", "Warm Korean female voice.", "Korean"),
)


def vieneu_descriptors(
    catalog: list[dict[str, str]] | None = None,
) -> list[VoiceDescriptor]:
    """VieNeu presets as descriptors (model-free; catalog injectable)."""
    if catalog is None:
        from vienetts_app.core.engine import preset_voices  # noqa: PLC0415 - lazy, SDK asset

        catalog = preset_voices()
    caps = get_capabilities(VIENEU)
    out: list[VoiceDescriptor] = []
    for entry in catalog:
        region = _parse_region(entry.get("description", ""))
        out.append(
            VoiceDescriptor(
                voice_id=entry["name"],
                engine=VIENEU,
                kind="preset",
                languages=caps.languages,
                label=_display_label(entry, region),
                description=entry.get("description", ""),
                gender=entry.get("gender", ""),
                style=entry.get("style", ""),
                region=region,
            )
        )
    return out


def qwen_descriptors(engine: EngineId) -> list[VoiceDescriptor]:
    """Fixed speakers for a Qwen profile (Base has none — enrollment only)."""
    caps = get_capabilities(engine)
    if engine == QWEN_BASE:
        return []
    if engine == QWEN_CUSTOMVOICE:
        return [
            VoiceDescriptor(
                voice_id=speaker.name,
                engine=engine,
                kind="preset",
                languages=caps.languages,
                label=f"{speaker.name} — {speaker.description}",
                description=speaker.description,
                native_language=speaker.native_language,
            )
            for speaker in QWEN_SPEAKERS
        ]
    raise BackendCapabilityError(f"{caps.label} is not a voice-catalog profile")


def descriptors_for(engine: EngineId, **kwargs: Any) -> list[VoiceDescriptor]:
    """All preset descriptors for one engine (cloned voices: Phase 4 Task 3)."""
    if engine == VIENEU:
        return vieneu_descriptors(**kwargs)
    return qwen_descriptors(engine)


def filter_by_language(descriptors: list[VoiceDescriptor], language: str) -> list[VoiceDescriptor]:
    """Keep voices supporting ``language`` ("" keeps everything)."""
    if not language:
        return list(descriptors)
    return [d for d in descriptors if language in d.languages]


def sort_native_first(descriptors: list[VoiceDescriptor], language: str) -> list[VoiceDescriptor]:
    """Stable sort: voices native in ``language`` first (recommendation aid)."""
    if not language:
        return list(descriptors)
    from vienetts_app.core.qwen_backend import LANGUAGE_NAMES

    want = LANGUAGE_NAMES.get(language, "")
    natives = [d for d in descriptors if d.native_language.split(" (")[0] == want]
    rest = [d for d in descriptors if d not in natives]
    return natives + rest
